"""Offline tests for run_pilot.py with stub rtk binaries (no real rtk, no network)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
RUNNER = HERE / "run_pilot.py"

STUBS = {
    # echoes its input: every marker survives
    "keep": '#!/bin/sh\nif [ "$1" = summary ]; then shift; exec "$@"; fi\nexec cat\n',
    # drops every line naming a test: failure IDs are lost
    "lossy": '#!/bin/sh\nif [ "$1" = summary ]; then shift; "$@" | grep -v test_; exit 0; fi\ngrep -v -e test_ -e billing\nexit 0\n',
    # dies like the release binary on a CPU without the needed instructions
    "sigill": '#!/bin/sh\nkill -ILL $$\n',
    # prints which env it got, to prove isolation
    "env": '#!/bin/sh\nenv\n',
}


def make_stub(d: Path, kind: str) -> Path:
    p = d / "rtk"
    p.write_text(STUBS[kind])
    p.chmod(0o755)
    return p


def lock_for(d: Path, rtk: Path) -> Path:
    lock = json.loads((HERE / "rtk.lock.json").read_text())
    lock["binary_sha256"] = hashlib.sha256(rtk.read_bytes()).hexdigest()
    p = d / "lock.json"
    p.write_text(json.dumps(lock))
    return p


def run(rtk, out, lock=None, cases=None, extra_env=None):
    args = [sys.executable, "-I", str(RUNNER), "--rtk", str(rtk), "--out", str(out)]
    if lock:
        args += ["--lock", str(lock)]
    if cases:
        args += ["--cases", str(cases)]
    env = dict(os.environ, **(extra_env or {}))
    return subprocess.run(args, capture_output=True, text=True, env=env)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kept_markers_exit_0_and_raw_logs_preserved(self):
        rtk = make_stub(self.d, "keep")
        r = run(rtk, self.d / "out", lock_for(self.d, rtk))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = json.loads((self.d / "out/summary.json").read_text())
        self.assertEqual(s["lossy"], [])
        self.assertTrue(all(c["verdict"] == "kept" for c in s["cases"]))
        case = self.d / "out/failing-tests"
        for f in ("argv.json", "raw.input", "rtk.stdout", "rtk.stderr", "result.json"):
            self.assertTrue((case / f).is_file(), f)
        timeout = json.loads((self.d / "out/timeout/result.json").read_text())
        self.assertEqual(timeout["producer_exit"], 124)  # producer status kept separately

    def test_lost_failure_ids_flagged_exit_1(self):
        rtk = make_stub(self.d, "lossy")
        r = run(rtk, self.d / "out", lock_for(self.d, rtk))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        s = json.loads((self.d / "out/summary.json").read_text())
        self.assertIn("failing-tests", s["lossy"])
        fail = next(c for c in s["cases"] if c["id"] == "failing-tests")
        self.assertIn("test_queue_drains_fifo", fail["lost_markers"])

    def test_crash_is_unmeasured_not_zero(self):
        rtk = make_stub(self.d, "sigill")
        r = run(rtk, self.d / "out", lock_for(self.d, rtk))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        for c in json.loads((self.d / "out/summary.json").read_text())["cases"]:
            self.assertEqual(c["verdict"], "unmeasured")
            self.assertEqual(c["status"], "crashed")
            self.assertEqual(c["rtk_signal"], "SIGILL")
            self.assertIsNone(c["reduction"])
            self.assertIsNone(c["rtk_bytes"])

    def test_digest_mismatch_refused_before_running(self):
        rtk = make_stub(self.d, "keep")
        r = run(rtk, self.d / "out")  # shipped lock pins the real binary
        self.assertEqual(r.returncode, 2)
        self.assertIn("!= pinned", r.stderr)
        self.assertFalse((self.d / "out").exists())

    def test_non_allowlisted_mode_or_fixture_refused(self):
        rtk = make_stub(self.d, "keep")
        lock = lock_for(self.d, rtk)
        for case in ({"id": "x", "fixture": "fixtures/timeout.log", "mode": "shell", "producer_exit": 0, "must_keep": []},
                     {"id": "y", "fixture": "../run_pilot.py", "mode": "log-stdin", "producer_exit": 0, "must_keep": []}):
            cases = self.d / "cases.json"
            cases.write_text(json.dumps({"cases": [case]}))
            r = run(rtk, self.d / "out", lock, cases)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertFalse((self.d / "out").exists())

    def test_rtk_env_is_private_and_credential_free(self):
        rtk = make_stub(self.d, "env")
        r = run(rtk, self.d / "out", lock_for(self.d, rtk), extra_env={"GITHUB_TOKEN": "canary", "ANTHROPIC_BASE_URL": "http://x"})
        env = (self.d / "out/timeout/rtk.stdout").read_text()
        self.assertNotIn("canary", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertIn("RTK_TELEMETRY_DISABLED=1", env)
        self.assertIn(f"HOME={self.d / 'out/.rtk-home'}", env)

    GOOD = {"id": "ok", "fixture": "fixtures/timeout.log", "mode": "log-stdin", "producer_exit": 0, "must_keep": ["x"]}

    def _refused(self, cases_doc, raw=False):
        rtk = make_stub(self.d, "keep")
        lock = lock_for(self.d, rtk)
        cases = self.d / "cases.json"
        cases.write_text(cases_doc if raw else json.dumps(cases_doc))
        out = self.d / "nest" / "out"
        r = run(rtk, out, lock, cases)
        self.assertEqual(r.returncode, 2, f"{cases_doc!r}: {r.stdout}{r.stderr}")
        self.assertIn("refused:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.d / "nest").exists(), "nothing may be written")
        return r

    def test_case_ids_validated_before_any_write(self):
        for bad_id in ("../escape", "a/b", "", ".", "..", "x" * 65, 7, None, ".hidden", ".rtk-home", "summary.json"):
            self._refused({"cases": [dict(self.GOOD, id=bad_id)]})
        self.assertFalse((self.d / "escape").exists())
        # duplicate ids would overwrite each other's evidence
        self._refused({"cases": [self.GOOD, dict(self.GOOD)]})

    def test_case_keys_and_types_validated_before_any_write(self):
        g = self.GOOD
        for case in (
            {k: v for k, v in g.items() if k != "must_keep"},
            {k: v for k, v in g.items() if k != "producer_exit"},
            {k: v for k, v in g.items() if k != "id"},
            dict(g, extra="x"),
            dict(g, must_keep="x"),
            dict(g, must_keep=[1]),
            dict(g, must_keep=[]),
            dict(g, producer_exit="1"),
            dict(g, producer_exit=True),
            dict(g, fixture=5),
            "not-an-object",
        ):
            self._refused({"cases": [case]})
        for doc in ({"cases": []}, {"cases": {}}, [], {"cases": [g], "surprise": 1}):
            self._refused(doc)
        self._refused("{not json", raw=True)

    def test_bad_lock_refused_not_crashed(self):
        rtk = make_stub(self.d, "keep")
        lock = self.d / "lock.json"
        lock.write_text(json.dumps({"release": "v1"}))
        r = run(rtk, self.d / "out", lock)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.d / "out").exists())

    def test_exec_failure_is_unmeasured_not_a_crash_of_the_runner(self):
        rtk = self.d / "rtk"
        rtk.write_bytes(b"\x00\x01not an executable format\n")
        rtk.chmod(0o755)
        r = run(rtk, self.d / "out", lock_for(self.d, rtk))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        for c in json.loads((self.d / "out/summary.json").read_text())["cases"]:
            self.assertEqual(c["status"], "exec-error")
            self.assertEqual(c["verdict"], "unmeasured")

    def test_unexpected_runner_error_has_its_own_exit_code(self):
        rtk = make_stub(self.d, "keep")
        out = self.d / "out"
        out.write_text("a file where the out dir should be")
        r = run(rtk, out, lock_for(self.d, rtk))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("internal error", r.stderr)

    def test_producer_exit_parity_is_reported_as_not_tested(self):
        rtk = make_stub(self.d, "keep")
        run(rtk, self.d / "out", lock_for(self.d, rtk))
        s = json.loads((self.d / "out/summary.json").read_text())
        self.assertEqual(s["producer_exit_parity"], "not-tested")
        for c in s["cases"]:
            self.assertEqual(c["producer_exit_parity"], "not-tested")


if __name__ == "__main__":
    unittest.main()
