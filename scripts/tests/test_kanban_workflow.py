"""Tests for the kanban-workflow skill (hermes/skills/kanban-workflow).

Unit tests drive the script with a fake `hermes` runner. The integration test
runs the real `hermes kanban` CLI against a throwaway HERMES_HOME (never the
live board) and skips when Hermes is not installed.

Run: python3 -m unittest discover -s scripts/tests -p 'test_kanban_workflow.py'
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "hermes" / "skills" / "kanban-workflow" / "scripts" / "workflow.py"

sys.dont_write_bytecode = True  # keep __pycache__ out of the installed skill dir
spec = importlib.util.spec_from_file_location("kanban_workflow", SCRIPT)
wf = importlib.util.module_from_spec(spec)
sys.modules["kanban_workflow"] = wf
spec.loader.exec_module(wf)

DISCORD_ENV = {
    "HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_ID": "111",
    "HERMES_SESSION_THREAD_ID": "222", "HERMES_SESSION_MESSAGE_ID": "333",
    "HERMES_SESSION_USER_ID": "444", "HERMES_SESSION_CHAT_TYPE": "thread",
}


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeHermes:
    """Records argv; answers `config get` from a backend map and mints card ids."""

    def __init__(self, backends=None):
        self.calls = []
        self.backends = backends if backends is not None else {
            "mac-worker": "ssh", "windows-worker": "ssh", "reviewer": "local"}
        self.cards = {}

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "-p":
            backend = self.backends.get(argv[1])
            return done(1, err="no such profile") if backend is None else done(0, backend + "\n")
        if argv[:2] == ["kanban", "create"]:
            key = argv[argv.index("--idempotency-key") + 1]
            card = self.cards.setdefault(key, {"id": f"t_{len(self.cards) + 1}", "title": argv[2]})
            return done(0, json.dumps(card))
        if argv[:2] == ["kanban", "notify-subscribe"]:
            return done(0, "Subscribed")
        raise AssertionError(f"unexpected call {argv}")

    def of(self, verb):
        return [c for c in self.calls if c[:2] == ["kanban", verb]]


def intake_args(*extra):
    return wf.parser().parse_args(["intake", "--title", "Build the iPad app", *extra])


class IntakeTest(unittest.TestCase):
    def test_builds_lanes_then_request_and_subscribes_discord_origin(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--body", "full ask"), fake)

        creates = fake.of("create")
        self.assertEqual([c[c.index("--assignee") + 1] for c in creates],
                         ["mac-worker", "windows-worker", "default"])
        request = creates[-1]
        self.assertEqual([request[i + 1] for i, a in enumerate(request) if a == "--parent"], ["t_1", "t_2"])
        self.assertEqual(plan["key"], "discord:111:333")
        self.assertEqual(request[request.index("--idempotency-key") + 1], "discord:111:333:request")
        lane_body = creates[0][creates[0].index("--body") + 1]
        self.assertIn("full ask", lane_body)
        self.assertIn('kanban_request_review(reviewer="reviewer"', lane_body)
        self.assertIn("Never merge, deploy, release", lane_body)
        self.assertIn(f"{wf.SCRIPT} gate", request[request.index("--body") + 1])
        self.assertEqual(request[2], "Report: Build the iPad app")
        self.assertIn('"Report:" card that\n   waits on this one is the final report, not a review phase', lane_body)

        subs = {c[2]: c for c in fake.of("notify-subscribe")}
        self.assertEqual(subs["t_3"][subs["t_3"].index("--delivery-mode") + 1], "notify+wake")
        self.assertEqual(subs["t_1"][subs["t_1"].index("--delivery-mode") + 1], "notify")
        self.assertEqual(subs["t_3"][subs["t_3"].index("--thread-id") + 1], "222")
        self.assertTrue(plan["subscribed"])
        self.assertEqual(plan["request"]["id"], "t_3")

    def test_rerun_is_idempotent(self):
        fake = FakeHermes()
        args = intake_args("--lane", "mac", "--key", "k")
        first, second = wf.intake(args, fake), wf.intake(args, fake)
        self.assertEqual(first["request"]["id"], second["request"]["id"])
        self.assertEqual(len(fake.cards), 2)

    def test_explicit_flags_win_over_session_env(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = wf.intake(intake_args("--lane", "mac", "--discord-chat-id", "999",
                                         "--discord-message-id", "888"), fake)
        self.assertEqual(plan["key"], "discord:999:888")

    def test_non_discord_session_is_not_an_origin(self):
        fake = FakeHermes()
        env = {**DISCORD_ENV, "HERMES_SESSION_PLATFORM": "telegram"}
        with mock.patch.dict(os.environ, env, clear=False):
            plan = wf.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        self.assertFalse(plan["subscribed"])
        self.assertEqual(fake.of("notify-subscribe"), [])

    def test_requires_an_idempotency_key(self):
        with mock.patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": ""}, clear=False):
            with self.assertRaisesRegex(wf.WorkflowError, "idempotency key"):
                wf.intake(intake_args("--lane", "mac"), FakeHermes())

    def test_dry_run_creates_nothing(self):
        fake = FakeHermes()
        plan = wf.intake(intake_args("--lane", "mac", "--key", "k", "--dry-run"), fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(plan["lanes"][0]["idempotency_key"], "k:mac-worker")

    def test_refuses_lane_that_would_run_on_this_host(self):
        fake = FakeHermes({"mac-worker": "local", "reviewer": "local"})
        with self.assertRaisesRegex(wf.WorkflowError, "not 'ssh'"):
            wf.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        self.assertEqual(fake.of("create"), [])
        wf.intake(intake_args("--lane", "mac", "--key", "k", "--allow-local-lane"), fake)
        self.assertEqual(len(fake.of("create")), 2)

    def test_refuses_missing_profiles_and_self_review(self):
        with self.assertRaisesRegex(wf.WorkflowError, "'reviewer' does not exist"):
            wf.intake(intake_args("--lane", "mac", "--key", "k"), FakeHermes({"mac-worker": "ssh"}))
        with self.assertRaisesRegex(wf.WorkflowError, "review must be independent"):
            wf.intake(intake_args("--lane", "mac", "--key", "k", "--reviewer", "mac-worker"), FakeHermes())

    def test_any_profile_name_is_a_lane(self):
        fake = FakeHermes({"coder": "local", "reviewer": "local"})
        plan = wf.intake(intake_args("--lane", "coder", "--key", "k"), fake)
        self.assertEqual(plan["lanes"][0]["assignee"], "coder")


def show(status="done", events=(), runs=()):
    return {"task": {"status": status}, "events": [{"kind": k, "payload": p} for k, p in events],
            "runs": [{"profile": p, "outcome": o} for p, o in runs]}


ASKED = ("review_requested", {"implementer": "mac-worker", "reviewer": "reviewer"})


class ReviewVerdictTest(unittest.TestCase):
    def test_independent_approval_passes(self):
        ok, reason = wf.review_verdict(show(events=[ASKED, ("completed", {})],
                                            runs=[("mac-worker", "review_requested"), ("reviewer", "completed")]))
        self.assertTrue(ok, reason)

    def test_failures(self):
        cases = {
            "status is review": show("review", [ASKED], [("mac-worker", "review_requested")]),
            "without a review request": show(events=[("completed", {})], runs=[("mac-worker", "completed")]),
            "self-approved": show(events=[ASKED, ("completed", {})], runs=[("mac-worker", "completed")]),
            "not re-reviewed": show(events=[ASKED, ("changes_requested", {}), ("completed", {})],
                                    runs=[("reviewer", "completed")]),
        }
        for expected, card in cases.items():
            ok, reason = wf.review_verdict(card)
            self.assertFalse(ok, expected)
            self.assertIn(expected, reason)

    def test_second_review_after_changes_passes(self):
        card = show(events=[ASKED, ("changes_requested", {}), ASKED, ("completed", {})],
                    runs=[("mac-worker", "review_requested"), ("mac-worker", "review_requested"),
                          ("reviewer", "completed")])
        self.assertTrue(wf.review_verdict(card)[0])

    def test_gate_exit_codes(self):
        cards = {"t_req": {"task": {"status": "todo"}, "parents": ["t_a"]},
                 "t_a": show(events=[ASKED, ("completed", {})], runs=[("reviewer", "completed")])}
        run = lambda argv: done(0, json.dumps(cards[argv[2]]))  # noqa: E731
        gate = lambda: wf.main(["gate", "t_req"], run)  # noqa: E731
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate(), 0)
            cards["t_a"] = show("blocked")
            self.assertEqual(gate(), 1)
            cards["t_req"]["parents"] = []
            self.assertEqual(gate(), 1)
        self.assertIn("review gate: PASS", out.getvalue())
        self.assertIn("status is blocked", out.getvalue())


@unittest.skipUnless(shutil.which("hermes"), "hermes not installed")
class RealBoardTest(unittest.TestCase):
    """The real CLI on a temp board: graph shape, subscriptions, and the gate."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kanban-workflow-")
        self.env = {**os.environ, "HERMES_HOME": self.home}
        for k in DISCORD_ENV:
            self.env.pop(k, None)
        self.hermes(["kanban", "init"])

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def hermes(self, argv):
        return subprocess.run(["hermes", *argv], capture_output=True, text=True, env=self.env, timeout=120)

    def run_cli(self, argv):
        # Profiles do not exist in the temp home: answer `config get` like an SSH lane.
        if argv[0] == "-p":
            return done(0, "ssh\n")
        return self.hermes(argv)

    def show(self, tid):
        return json.loads(self.hermes(["kanban", "show", tid, "--json"]).stdout)

    def test_full_lifecycle(self):
        args = intake_args("--lane", "mac", "--lane", "windows", "--key", "it",
                           "--discord-chat-id", "111", "--discord-thread-id", "222", "--discord-chat-type", "thread")
        plan = wf.intake(args, self.run_cli)
        req, (mac, win) = plan["request"]["id"], [lane["id"] for lane in plan["lanes"]]

        request = self.show(req)
        self.assertEqual(request["task"]["status"], "todo")
        self.assertEqual(sorted(request["parents"]), sorted([mac, win]))
        self.assertEqual(self.show(mac)["task"]["status"], "ready")
        subs = json.loads(self.hermes(["kanban", "notify-list", req, "--json"]).stdout)
        self.assertEqual([(s["chat_id"], s["thread_id"], s["delivery_mode"]) for s in subs],
                         [("111", "222", "notify+wake")])
        self.assertEqual(wf.intake(args, self.run_cli)["request"]["id"], req)  # idempotent

        # mac: implement -> review -> independent approval
        self.hermes(["kanban", "claim", mac])
        self.hermes(["kanban", "request-review", mac, "--summary", "built", "--reviewer", "reviewer", "--force"])
        self.hermes(["kanban", "complete", mac, "--summary", "LGTM"])
        # windows: completed by the implementer, never reviewed
        self.hermes(["kanban", "claim", win])
        self.hermes(["kanban", "complete", win, "--summary", "done"])

        ok, rows = wf.gate(req, self.run_cli)
        self.assertFalse(ok)
        verdicts = {r["id"]: r for r in rows}
        self.assertTrue(verdicts[mac]["ok"], verdicts[mac])
        self.assertIn("without a review request", verdicts[win]["reason"])
        self.assertEqual(self.show(req)["task"]["status"], "ready")  # both lanes done -> report card ready


if __name__ == "__main__":
    unittest.main()
