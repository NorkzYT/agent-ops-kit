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
    def __init__(self, regs=(), usage=None, targets=(BLANK, UI), cookies=()):
        self.regs, self.usage, self.targets = list(regs), usage or {}, list(targets)
        self.cookies = [dict(c) for c in cookies]
        self.events, self.calls, self.deleted = [], [], []

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
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.cookies]}
        if method == "Network.deleteCookies":
            self.deleted.append(dict(params))
            self.cookies = [c for c in self.cookies if not (
                c["name"] == params["name"] and c["domain"] == params["domain"]
                and (c.get("path") or "/") == params["path"] and c.get("partitionKey") == params.get("partitionKey"))]
            return {}
        raise AssertionError(f"unexpected CDP call {method}")

    def collect(self, seconds, until=None):
        return until is None or until(self.events)


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


# --- cookies (review P1: Domain= cookie tossed from a sibling subdomain) ---------
TOSSED = {"name": "sid", "domain": ".example.com", "path": "/", "value": "attacker"}
HOSTONLY_SIBLING = {"name": "w", "domain": "www.example.com", "path": "/"}
HOSTONLY_SIGNIN = {"name": "h", "domain": "accounts.example.com", "path": "/x"}
PARTITIONED = {"name": "p", "domain": "tracker.test", "path": "/",
               "partitionKey": {"topLevelSite": "https://example.com", "hasCrossSiteAncestor": False}}
SUFFIX_NOT_PARENT = {"name": "s", "domain": ".ample.com", "path": "/"}


def t_verify_fails_on_parent_domain_cookie():
    # Storage.getUsageAndQuota has no cookie entry: the tossed cookie is only
    # visible to a cookie listing. Pre-fix verify never listed cookies.
    fields, ok = run_verify(FakeCDP(cookies=[TOSSED]))
    assert not ok, fields
    assert fields.get("cookies_left") == ["sid@.example.com/"], fields


def t_verify_fails_on_partitioned_cookie_under_signin_site():
    fields, ok = run_verify(FakeCDP(cookies=[PARTITIONED]))
    assert not ok and fields.get("cookies_left"), fields


def t_cookie_match_is_scoped():
    assert hasattr(h, "cookie_hits"), "no cookie_hits() seam"
    got = h.cookie_hits([TOSSED, HOSTONLY_SIBLING, HOSTONLY_SIGNIN, PARTITIONED, SUFFIX_NOT_PARENT],
                        ["https://accounts.example.com"])
    assert [c["name"] for c in got] == ["sid", "h", "p"], got
    fields, ok = run_verify(FakeCDP(cookies=[HOSTONLY_SIBLING, SUFFIX_NOT_PARENT]))
    assert ok, fields


def t_delete_cookies_exact_and_verified():
    assert hasattr(h, "delete_cookies"), "no delete_cookies() seam"
    c = FakeCDP(cookies=[TOSSED, HOSTONLY_SIBLING, PARTITIONED])
    labels = h.delete_cookies(c, "S", ["https://accounts.example.com"])
    assert labels == ["p@tracker.test/ [partition example.com]", "sid@.example.com/"], labels
    assert {"name": "sid", "domain": ".example.com", "path": "/"} in c.deleted, c.deleted
    assert any(d.get("partitionKey") == PARTITIONED["partitionKey"] for d in c.deleted), c.deleted
    assert [x["name"] for x in c.cookies] == ["w"], c.cookies
    fields, ok = run_verify(c)
    assert ok and fields["cookies_left"] == [], fields


def t_delete_that_does_not_take_effect_fails_verify():
    c = FakeCDP(cookies=[TOSSED])
    c.call = (lambda orig: (lambda m, p=None, session=None: {} if m == "Network.deleteCookies" else orig(m, p, session)))(c.call)
    h.delete_cookies(c, "S", ["https://accounts.example.com"])
    fields, ok = run_verify(c)
    assert not ok and fields["cookies_left"], fields


def t_parse_args_rejects_userinfo_and_bad_port():
    for bad in ("https://u:p@accounts.example.com", "https://u@accounts.example.com",
                "https://accounts.example.com:99999", "https://accounts.example.com:0"):
        try:
            h.parse_args(["--clear-origin", bad])
        except (h.Fail, ValueError):
            continue
        raise AssertionError(f"accepted {bad}")
    assert h.parse_args(["--clear-origin", "https://a.test:8443"]) == ["https://a.test:8443"]


# --- neo-quit stdout (review P1: would corrupt the JSON report) ----------------
def t_neo_quit_stdout_not_inherited():
    assert hasattr(h, "quit_browser"), "no quit_browser() seam (stdout inherited by subprocess.run)"
    seen = {}

    class R:
        returncode = 0

    class FakeSub:
        DEVNULL = -3

        @staticmethod
        def run(argv, **kw):
            seen.update(kw, argv=argv)
            return R()
    real = h.subprocess
    h.subprocess = FakeSub
    try:
        assert h.quit_browser() == 0
    finally:
        h.subprocess = real
    assert seen.get("stdout") not in (None, sys.stdout), seen


# --- real CDP framing over a scripted socket (review P1: SW listing, partial frames)
import socket as _socket
import json as _json


class Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class ScriptSock:
    """recv() returns the next scripted chunk; "T" (or an empty script) is a 0.25 s timeout."""
    def __init__(self, clock, steps, on_send=None):
        self.clock, self.steps, self.on_send, self.sent = clock, list(steps), on_send, []

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent.append(data)
        if self.on_send:
            self.on_send(self)

    def recv(self, n):
        if not self.steps or self.steps[0] == "T":
            if self.steps:
                self.steps.pop(0)
            self.clock.t += 0.25
            raise _socket.timeout("timed out")
        return self.steps.pop(0)


def frame(obj, fin=True, op=1):
    data = obj if isinstance(obj, bytes) else _json.dumps(obj).encode()
    b1 = (0x80 if fin else 0) | op
    if len(data) < 126:
        return bytes([b1, len(data)]) + data
    if len(data) < 65536:
        return bytes([b1, 126]) + len(data).to_bytes(2, "big") + data
    return bytes([b1, 127]) + len(data).to_bytes(8, "big") + data


def with_cdp(steps, fn, on_send=None):
    clock = Clock()
    real_time = h.time
    h.time = clock
    try:
        c = h.CDP.__new__(h.CDP)
        c.s, c.buf, c.n, c.events, c.partial = ScriptSock(clock, steps, on_send), b"", 0, [], b""
        return fn(c, clock)
    finally:
        h.time = real_time


EV = {"method": "ServiceWorker.workerRegistrationUpdated",
      "params": {"registrations": [{"registrationId": "7", "scopeURL": "https://planted.test/", "isDeleted": False}]}}
EMPTY = {"method": "ServiceWorker.workerRegistrationUpdated", "params": {"registrations": []}}


def t_late_listing_is_waited_for():
    # The listing arrives 3 s after enable. Pre-fix: 2 s of silence = zero registrations.
    steps = [frame({"id": 1, "result": {}})] + ["T"] * 12 + [frame(EV)]
    regs = with_cdp(steps, lambda c, _: h.registrations(c, None))
    assert [r["scopeURL"] for r in regs] == ["https://planted.test/"], regs


def t_no_listing_fails_closed():
    steps = [frame({"id": 1, "result": {}})]
    try:
        with_cdp(steps, lambda c, _: h.registrations(c, None))
    except h.Fail as e:
        assert "listing" in str(e), e
    else:
        raise AssertionError("silence after ServiceWorker.enable was accepted as zero registrations")


def t_empty_listing_is_zero():
    steps = [frame({"id": 1, "result": {}}), "T", frame(EMPTY)]
    assert with_cdp(steps, lambda c, _: h.registrations(c, None)) == []


def t_partial_frame_across_timeout_is_kept():
    big = dict(EV, pad="x" * 300)          # 126-form length header
    f = frame(big)
    steps = [f[:3], "T", f[3:100], "T", f[100:]]
    def run(c, _):
        c.collect(5.0)
        return c.events
    ev = with_cdp(steps, run)
    assert ev == [big], ev


def t_fragmented_message_across_timeout_is_kept():
    data = _json.dumps(EV).encode()
    steps = [frame(data[:20], fin=False, op=1), "T", "T", frame(data[20:], fin=True, op=0)]
    def run(c, _):
        c.collect(5.0)
        return c.events
    assert with_cdp(steps, run) == [EV]


def t_oversized_message_fails():
    if not hasattr(h, "MAX_MESSAGE"):
        raise AssertionError("no message size bound")
    hdr = bytes([0x81, 127]) + (h.MAX_MESSAGE + 1).to_bytes(8, "big")
    try:
        with_cdp([hdr], lambda c, _: c.recv())
    except h.Fail:
        return
    raise AssertionError("oversized frame accepted")


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        check(name[2:].replace("_", " "), fn)
print(f"neo-handoff unit: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
