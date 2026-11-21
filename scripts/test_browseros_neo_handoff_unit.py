"""Pure-mock checks for docker/browseros-neo/neo-handoff.py verification logic.

No browser, Docker, network or filesystem side effects: a fake CDP object
answers registrations, usage and target queries. Run:

  python3 -I scripts/test_browseros_neo_handoff_unit.py [path/to/neo-handoff.py]
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "docker", "browseros-neo", "neo-handoff.py")
spec = importlib.util.spec_from_file_location("neo_handoff", PATH)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)  # __main__ guard: nothing runs

KEEP = "K1"
BLANK = {"targetId": KEEP, "type": "page", "url": "about:blank"}
UI = {"targetId": "U1", "type": "browser_ui", "url": "chrome://omnibox-popup.top-chrome/"}


class FakeCDP:
    def __init__(self, regs=(), usage=None, targets=(BLANK, UI)):
        self.regs, self.usage, self.targets = list(regs), usage or {}, list(targets)
        self.events, self.calls = [], []

    def call(self, method, params=None, session=None):
        self.calls.append(method)
        if method == "ServiceWorker.enable":
            self.events.append({"method": "ServiceWorker.workerRegistrationUpdated",
                                "params": {"registrations": self.regs}})
            return {}
        if method == "Storage.getUsageAndQuota":
            return {"usageBreakdown": [{"storageType": k, "usage": v}
                                       for k, v in self.usage.get(params["origin"], {}).items()]}
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        raise AssertionError(f"unexpected CDP call {method}")

    def collect(self, seconds):
        pass


passed = failed = 0


def check(label, fn):
    global passed, failed
    try:
        fn()
        print(f"  ok   {label}")
        passed += 1
    except Exception as e:  # noqa: BLE001 - every exception is a test failure
        print(f"  FAIL {label}: {type(e).__name__}: {e}")
        failed += 1


def run_verify(c, sw=("https://sw.test",), clear=("https://accounts.example.com",)):
    if hasattr(h, "verify"):
        return h.verify(c, "S", KEEP, list(sw), list(clear))
    # Pre-fix source has no seam: run its exact step-3 lines from main()
    # against the fake (RED evidence for the shadowed `left`).
    import inspect, textwrap
    src = inspect.getsource(h.main)
    a = src.index("    left = registrations(c, sid)")
    b = src.index('        raise Fail("scrub did not verify")')
    b = src.index("\n", b) + 1
    body = textwrap.dedent(src[a:b]).replace("c.close()", "pass").replace(
        'raise Fail("scrub did not verify")', "pass")
    body += "fields = report\nok = not (left or caches or bad)\n"
    env = dict(vars(h), c=c, sid="S", keep=KEEP, sw_origins=list(sw), clear_origins=list(clear), report={})
    exec(body, env)
    return env["fields"], env["ok"]


SW = {"registrationId": "1", "scopeURL": "https://sw.test/", "isDeleted": False}


def t_clean():
    fields, ok = run_verify(FakeCDP())
    assert ok and fields["service_workers_left"] == [] and fields["storage_left"] == {}, fields


def t_surviving_sw_with_storage():
    # A surviving registration AND nonzero storage: both must be reported, no TypeError.
    c = FakeCDP(regs=[SW], usage={"https://sw.test": {"cache_storage": 10},
                                  "https://accounts.example.com": {"cookies": 3}})
    fields, ok = run_verify(c)
    assert not ok, fields
    assert fields["service_workers_left"] == ["https://sw.test/"], fields
    assert fields["storage_left"] == {"https://sw.test": {"cache_storage": 10},
                                      "https://accounts.example.com": {"cookies": 3}}, fields


def t_surviving_sw_last_origin_clean():
    # Registration survives; the last scanned origin has zero usage. Before the fix
    # `left` was overwritten by {} and the registration vanished from the verdict.
    c = FakeCDP(regs=[SW], usage={})
    fields, ok = run_verify(c)
    assert not ok and fields["service_workers_left"] == ["https://sw.test/"], fields


def t_storage_only():
    c = FakeCDP(usage={"https://accounts.example.com": {"indexeddb": 5}})
    fields, ok = run_verify(c)
    assert not ok and fields["storage_left"] == {"https://accounts.example.com": {"indexeddb": 5}}, fields


def t_sw_origin_ignores_other_types():
    c = FakeCDP(usage={"https://sw.test": {"cookies": 7}})
    fields, ok = run_verify(c)
    assert ok, fields


def t_keep_missing():
    assert any("missing" in b for b in h.target_problems(FakeCDP(targets=[UI]), KEEP))


def t_keep_moved():
    moved = dict(BLANK, url="https://evil.test/")
    assert h.target_problems(FakeCDP(targets=[moved, UI]), KEEP)


def t_keep_not_page():
    assert h.target_problems(FakeCDP(targets=[dict(BLANK, type="iframe"), UI]), KEEP)


def t_keep_ok():
    assert h.target_problems(FakeCDP(), KEEP) == []


def t_extra_tab():
    extra = {"targetId": "X", "type": "page", "url": "https://other.test/"}
    assert h.target_problems(FakeCDP(targets=[BLANK, UI, extra]), KEEP)


def t_verify_fails_on_moved_keep():
    fields, ok = run_verify(FakeCDP(targets=[dict(BLANK, url="https://evil.test/"), UI]))
    assert not ok and fields["unexpected_targets"], fields


def t_requires_clear_origin():
    assert hasattr(h, "parse_args"), "no parse_args() seam"
    try:
        h.parse_args([])
    except h.Fail as e:
        assert "--clear-origin" in str(e)
    else:
        raise AssertionError("empty argv accepted")
    assert h.parse_args(["--clear-origin", "https://accounts.example.com"]) == ["https://accounts.example.com"]
    for bad in (["--clear-origin", "http://a.test"], ["--clear-origin", "https://a.test/x"], ["--other"]):
        try:
            h.parse_args(bad)
        except h.Fail:
            continue
        raise AssertionError(f"accepted {bad}")


def t_main_refuses_before_touching():
    assert hasattr(h, "parse_args")
    touched = []
    h.one_browser_pid = lambda: touched.append("pid") or 1
    h.subprocess = None  # any lifecycle step would crash differently
    try:
        h.main([])
    except h.Fail:
        pass
    assert touched == [], touched


def t_limits_honest():
    text = " ".join(getattr(h, "LIMITS", []))
    assert "same persistent profile" in text and "no tab was resumed" in text, text
    assert "not prove the whole profile" in text and "NO-GO" in text, text


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        check(name[2:].replace("_", " "), fn)
print(f"neo-handoff unit: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
