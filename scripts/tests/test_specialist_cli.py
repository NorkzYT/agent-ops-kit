"""Tests for the specialist-router operator targets: ``make specialist-route``
(dry-run CLI, usage without a task, readable summary) and ``make
specialist-logs`` (content-free live decisions from the Hermes agent log).

Offline: temp HERMES_HOME and profiles root, fixture agent.log, no Hermes.

Run: python3 -m unittest discover -s scripts/tests -p 'test_specialist_cli.py'
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _loader import REPO  # noqa: E402

CLI = REPO / "scripts" / "specialist-route.py"
MAKE = ["make", "-C", str(REPO), "--no-print-directory"]

PREFIX = "INFO hermes.plugin.model_router: model_router:"
LOG_LINES = [
    f"2026-10-07 08:20:54,986 {PREFIX} specialists task=95c684dd privacy=public tier=elevated "
    "routes=- dropped=- classifier=off",
    f"2026-10-07 08:21:00,001 {PREFIX} tier decision tier=medium reasons=default",
    "2026-10-07 08:21:01,002 INFO gateway.run: inbound [Norkz] model_router: specialists task=SECRET "
    "Write the Zorblax plan",
    f"2026-10-07 08:22:10,111 {PREFIX} specialists task=1a2b3c4d privacy=public tier=medium "
    "source=attachment routes=marketing:short:ready dropped=- classifier=off reason=-",
    f"2026-10-07 08:22:11,222 WARNING hermes.plugin.model_router: model_router: specialist routing "
    "skipped (RuntimeError)",
]


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TASK", "PLAN", "REQUIRE_READY", "LINES", "MAKEFLAGS", "MFLAGS")}
    env.update(extra)
    return env


class SpecialistRouteCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def run_cli(self, *args, stdin="", **env):
        return subprocess.run([sys.executable, str(CLI), "--profiles-root", self.root, *args],
                              input=stdin, capture_output=True, text=True, timeout=60,
                              env=_env(**env))

    def assert_usage(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("error:", proc.stdout + proc.stderr)
        self.assertIn("no task given", proc.stdout)
        self.assertIn('make specialist-route TASK="', proc.stdout)
        self.assertIn("TASK=- < prompt.txt", proc.stdout)
        self.assertIn("make specialist-logs", proc.stdout)
        self.assertIn("make specialist-status", proc.stdout)

    def test_no_task_prints_usage_and_exits_zero(self):
        self.assert_usage(self.run_cli())
        self.assert_usage(self.run_cli(TASK="   "))
        self.assert_usage(self.run_cli("-", stdin="\n"))

    def test_make_target_without_task_prints_usage(self):
        proc = subprocess.run([*MAKE, "specialist-route"], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, env=_env())
        self.assert_usage(proc)

    def test_summary_lists_profiles_readiness_reasons_and_flags(self):
        proc = self.run_cli("Run a Zorblax security review of the login flow and write a marketing campaign.")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Zorblax", proc.stdout)
        trace, end = json.JSONDecoder().raw_decode(proc.stdout)
        summary = proc.stdout[end:]
        self.assertIn("summary", summary)
        self.assertIn("source typed", summary)
        self.assertIn("readiness:not-installed", summary)  # temp root: nothing installed
        self.assertIn("security-reviewer", summary)
        installed = Path(self.root)
        for name in ("security-reviewer", "marketing"):
            (installed / name / "knowledge").mkdir(parents=True)
        proc = self.run_cli("Run a security review of the login flow and write a marketing campaign.")
        trace, end = json.JSONDecoder().raw_decode(proc.stdout)
        summary = proc.stdout[end:]
        self.assertEqual({r["profile"] for r in trace["routes"]}, {"marketing", "security-reviewer"})
        line = next(ln for ln in summary.splitlines() if "security-reviewer" in ln)
        for want in ("short", "knowledge no-knowledge", "kw:review", "HIGH-RISK", "advisory"):
            self.assertIn(want, line)
        self.assertIn("advisory", next(ln for ln in summary.splitlines() if " marketing " in ln))

    def test_summary_explains_no_route(self):
        proc = self.run_cli("Rename the doc to final please.")
        _trace, end = json.JSONDecoder().raw_decode(proc.stdout)
        self.assertIn("no specialist selected (no-match)", proc.stdout[end:])

    def test_summary_marks_attachment_derived(self):
        home = Path(self.root) / "home"
        cache = home / "cache" / "documents"
        cache.mkdir(parents=True)
        body = "Write a Zorblax marketing campaign for the launch.\n"
        path = cache / "doc_000000000001_prompt.txt"
        path.write_text(body)
        wire = (f"[The user sent a text document: 'prompt.txt'. Its content has been included below. "
                f"The file is also saved at: {path}]\n\n[Norkz] [Content of prompt.txt]:\n{body}")
        (Path(self.root) / "marketing").mkdir()
        proc = self.run_cli("-", "--plan", stdin=wire, HERMES_HOME=str(home))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Zorblax", proc.stdout)
        trace, end = json.JSONDecoder().raw_decode(proc.stdout)
        self.assertEqual(trace["task_source"], "attachment")
        self.assertIn("source attachment-derived", proc.stdout[end:])
        self.assertIn("attachment-derived", proc.stdout[end:].split("<specialist-routing>")[1])


class SpecialistLogsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "hermes home"
        (self.home / "logs").mkdir(parents=True)
        self.log = self.home / "logs" / "agent.log"
        self.log.write_text("\n".join(LOG_LINES) + "\n")

    def make(self, *args, **env):
        return subprocess.run([*MAKE, *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=_env(HERMES_HOME=str(self.home), **env))

    def test_help_lists_targets(self):
        out = subprocess.run([*MAKE, "help"], capture_output=True, text=True, timeout=60).stdout
        for target in ("specialist-logs ", "specialist-logs-follow", "specialist-route"):
            self.assertIn(target, out)

    def test_snapshot_shows_only_plugin_specialist_lines(self):
        proc = self.make("specialist-logs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines, [LOG_LINES[0], LOG_LINES[3], LOG_LINES[4]])
        self.assertNotIn("Zorblax", proc.stdout + proc.stderr)
        self.assertNotIn("tier decision", proc.stdout)

    def test_lines_override_and_validation(self):
        proc = self.make("specialist-logs", "LINES=1")
        self.assertEqual(proc.stdout.splitlines(), [LOG_LINES[4]])
        for bad in ("abc", "0", "1; touch x"):
            proc = self.make("specialist-logs", LINES=bad)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("positive integer", proc.stderr)

    def test_no_match_and_missing_log_are_notices(self):
        self.log.write_text(LOG_LINES[1] + "\n" + LOG_LINES[2] + "\n")
        proc = self.make("specialist-logs")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("No specialist routing lines", proc.stderr)
        self.log.unlink()
        proc = self.make("specialist-logs")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("No Hermes agent log at", proc.stderr)

    def test_router_logs_unchanged(self):
        proc = self.make("router-logs")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(proc.stdout.splitlines()), 5)

    def test_follow_filters_live_lines(self):
        out = Path(self._tmp.name) / "follow.out"
        with out.open("w") as fh:
            proc = subprocess.Popen([*MAKE, "specialist-logs-follow"], stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=_env(HERMES_HOME=str(self.home)),
                                    start_new_session=True)
        try:
            time.sleep(1)
            with self.log.open("a") as fh:
                fh.write(LOG_LINES[2].replace("SECRET", "LIVESECRET") + "\n")
                fh.write(LOG_LINES[0].replace("95c684dd", "live0001") + "\n")
            time.sleep(1.5)
        finally:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        text = out.read_text()
        self.assertIn("task=live0001", text)
        self.assertNotIn("LIVESECRET", text)


if __name__ == "__main__":
    unittest.main()
