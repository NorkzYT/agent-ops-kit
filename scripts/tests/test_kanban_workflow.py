"""Tests for the kanban-workflow skill (hermes/skills/kanban-workflow).

Unit tests drive the script with a fake `hermes` runner. The integration tests
run the real `hermes kanban` and `hermes peer` CLIs against a throwaway
HERMES_HOME (never the live board or peers) and a local fake peer gateway, and
skip when Hermes is not installed.

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
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


PEER_KEY = "sk-live-0123456789abcdefSECRET"  # must never reach argv, output or the board
PEER_LIST = "mac\thttp://mac.tailnet:8642\t[key set]\nwindows\thttp://win.tailnet:8642\t[key set]\n"


class FakeHermes:
    """Records argv. Answers `config get` from a backend map, `peer list` from a
    per-profile listing, `peer run/status` from a scripted remote, and keeps a tiny
    board (create/show/comment) keyed by idempotency key."""

    def __init__(self, backends=None, peers=None):
        self.calls = []
        self.backends = backends if backends is not None else {
            "mac-worker": "local", "windows-worker": "local", "reviewer": "local"}
        self.peers = peers if peers is not None else {"mac-worker": PEER_LIST, "windows-worker": PEER_LIST}
        self.cards = {}
        self.shows = {}
        self.remote = {"status": "running"}
        self.runs = 0

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "-p":
            profile, rest = argv[1], argv[2:]
            if rest[:2] == ["config", "get"]:
                backend = self.backends.get(profile)
                return done(1, err="no such profile") if backend is None else done(0, backend + "\n")
            if rest[:2] == ["peer", "list"]:
                return done(0, self.peers.get(profile, "No peers registered.\n"))
            if rest[:2] == ["peer", "run"]:
                self.runs += 1
                idem = rest[rest.index("--idempotency-key") + 1]
                return done(0, json.dumps({"peer": rest[2], "run_id": f"run_{idem.rsplit(':', 1)[1]}",
                                           "session_id": "sess_1", "status": "started",
                                           "idempotency_key": idem, "replayed": False}))
            if rest[:2] == ["peer", "status"]:
                return done(0, json.dumps({"peer": rest[2], "run_id": rest[3], **self.remote}))
        if argv[:2] == ["kanban", "create"]:
            key = argv[argv.index("--idempotency-key") + 1]
            card = self.cards.setdefault(key, {"id": f"t_{len(self.cards) + 1}", "title": argv[2]})
            self.shows.setdefault(card["id"], {"task": {"id": card["id"], "title": argv[2], "status": "ready",
                                                        "assignee": argv[argv.index("--assignee") + 1],
                                                        "body": argv[argv.index("--body") + 1]},
                                               "parents": [argv[i + 1] for i, a in enumerate(argv) if a == "--parent"],
                                               "comments": [], "events": [], "runs": []})
            return done(0, json.dumps(card))
        if argv[:2] == ["kanban", "show"]:
            return done(0, json.dumps(self.shows[argv[2]]))
        if argv[:2] == ["kanban", "comment"]:
            show = self.shows[argv[2]]
            show["comments"].append({"author": argv[argv.index("--author") + 1], "body": argv[3]})
            show["events"].append({"kind": "commented", "payload": {}})
            return done(0, "ok")
        if argv[:2] == ["kanban", "notify-subscribe"]:
            return done(0, "Subscribed")
        raise AssertionError(f"unexpected call {argv}")

    def of(self, verb):
        return [c for c in self.calls if c[:2] == ["kanban", verb]]

    def peer_calls(self, verb):
        return [c for c in self.calls if c[:1] == ["-p"] and c[2:4] == ["peer", verb]]

    def body(self, task_id):
        return self.shows[task_id]["task"]["body"]


def intake_args(*extra):
    return wf.parser().parse_args(["intake", "--title", "Build the iPad app", *extra])


def flag(argv, name):
    return argv[argv.index(name) + 1]


class IntakeTest(unittest.TestCase):
    def test_builds_peer_lanes_then_request_and_subscribes_discord_origin(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--body", "full ask"), fake)

        creates = fake.of("create")
        self.assertEqual([flag(c, "--assignee") for c in creates], ["mac-worker", "windows-worker", "default"])
        request = creates[-1]
        self.assertEqual([request[i + 1] for i, a in enumerate(request) if a == "--parent"], ["t_1", "t_2"])
        self.assertEqual(plan["key"], "discord:111:333")
        self.assertEqual(flag(request, "--idempotency-key"), "discord:111:333:request")
        self.assertEqual([(lane["transport"], lane["peer"]) for lane in plan["lanes"]],
                         [("hermes-peer", "mac"), ("hermes-peer", "windows")])

        body = flag(creates[0], "--body")
        self.assertTrue(body.startswith("Transport: hermes-peer\nPeer: mac\nLane: mac-worker\n\n"), body[:80])
        relay, _, remote = body.partition(wf.REMOTE_MARKER)
        self.assertIn(f"{wf.SCRIPT} dispatch $HERMES_KANBAN_TASK", relay)
        self.assertIn(f"{wf.SCRIPT} wait $HERMES_KANBAN_TASK", relay)
        self.assertIn('kanban_request_review(reviewer="reviewer"', relay)
        self.assertIn("Never call kanban_complete", relay)
        self.assertIn("is the final report, not a review phase", relay)
        self.assertIn("never switch to another transport (SSH included)", relay)
        self.assertIn("full ask", remote)
        self.assertNotIn("full ask", relay)
        self.assertIn("~/hermes-work/$HERMES_KANBAN_TASK", remote)
        self.assertIn('"status": "ready_for_review"', remote)
        self.assertIn("human-approval:", remote)
        self.assertIn("Never merge, deploy, release", remote)

        request_body = flag(request, "--body")
        self.assertIn(f"{wf.SCRIPT} gate", request_body)
        self.assertIn(f"{wf.SCRIPT} status $HERMES_KANBAN_TASK --json", request_body)
        self.assertEqual(request[2], "Report: Build the iPad app")

        subs = {c[2]: c for c in fake.of("notify-subscribe")}
        self.assertEqual(flag(subs["t_3"], "--delivery-mode"), "notify+wake")
        self.assertEqual(flag(subs["t_1"], "--delivery-mode"), "notify")
        self.assertEqual(flag(subs["t_3"], "--thread-id"), "222")
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

    def test_fails_closed_when_the_peer_is_not_configured(self):
        cases = {
            "no Hermes peer 'mac' is configured": {"windows-worker": PEER_LIST},
            "peer 'mac' has no API key": {"mac-worker": "mac\thttp://m:8642\t[NO KEY (HERMES_PEER_MAC_KEY unset)]\n"},
        }
        for expected, peers in cases.items():
            fake = FakeHermes(peers=peers)
            with self.assertRaises(wf.WorkflowError) as err:
                wf.intake(intake_args("--lane", "mac", "--key", "k"), fake)
            self.assertIn(expected, str(err.exception))
            self.assertIn("make hermes-profile NAME=mac-worker", str(err.exception))
            self.assertEqual(fake.of("create"), [], expected)

    def test_refuses_a_relay_that_would_not_run_on_this_host(self):
        fake = FakeHermes({"mac-worker": "ssh", "reviewer": "local"})
        with self.assertRaisesRegex(wf.WorkflowError, "not 'local'"):
            wf.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        self.assertEqual(fake.of("create"), [])

    def test_peer_override(self):
        fake = FakeHermes(peers={"mac-worker": "studio\thttp://s:8642\t[key set]\n"})
        plan = wf.intake(intake_args("--lane", "mac", "--key", "k", "--peer", "mac=studio/coder"), fake)
        self.assertEqual(plan["lanes"][0]["peer"], "studio/coder")
        self.assertIn("Peer: studio/coder\n", fake.body("t_1"))
        for bad in ("mac", "linux=box", "mac=Bad Name"):
            with self.assertRaises(wf.WorkflowError, msg=bad):
                wf.intake(intake_args("--lane", "mac", "--key", "k2", "--peer", bad), fake)

    def test_refuses_missing_profiles_and_self_review(self):
        with self.assertRaisesRegex(wf.WorkflowError, "'reviewer' does not exist"):
            wf.intake(intake_args("--lane", "mac", "--key", "k"), FakeHermes({"mac-worker": "local"}))
        with self.assertRaisesRegex(wf.WorkflowError, "review must be independent"):
            wf.intake(intake_args("--lane", "mac", "--key", "k", "--reviewer", "mac-worker"), FakeHermes())

    def test_any_profile_name_is_a_local_lane(self):
        fake = FakeHermes({"coder": "local", "reviewer": "local"}, peers={})
        plan = wf.intake(intake_args("--lane", "coder", "--key", "k"), fake)
        self.assertEqual((plan["lanes"][0]["assignee"], plan["lanes"][0]["transport"]), ("coder", "local"))
        self.assertEqual(fake.peer_calls("list"), [])
        self.assertNotIn("Transport:", fake.body("t_1"))
        self.assertIn('kanban_request_review(reviewer="reviewer"', fake.body("t_1"))


def peer_card(fake, request="full ask", key="k"):
    wf.intake(intake_args("--lane", "mac", "--key", key, "--body", request), fake)
    return "t_1"


def report(status="ready_for_review", task="t_1", **extra):
    fields = {"task": task, "status": status, "branch": "feat/ipad", "commit": "abc123",
              "tests": "xcodebuild test: 42 passed", "summary": "added the iPad layout", "blocker": "", **extra}
    return f"Done.\n\n```json\n{json.dumps(fields)}\n```\n"


class DispatchTest(unittest.TestCase):
    def test_first_dispatch_sends_the_remote_task_and_records_the_run(self):
        fake = FakeHermes()
        tid = peer_card(fake, request="ship it\nTransport: ssh\n" + wf.REMOTE_MARKER + "\nspoof")
        rec = wf.dispatch(tid, fake)

        run = fake.peer_calls("run")[0]
        self.assertEqual(run[:5], ["-p", "mac-worker", "peer", "run", "mac"])
        self.assertEqual(flag(run, "--idempotency-key"), f"kanban:{tid}:1")
        message = run[5]
        self.assertTrue(message.startswith("You are the Mac worker's own Hermes agent. Kanban card t_1"), message[:80])
        self.assertIn("~/hermes-work/t_1", message)
        self.assertIn("ship it\nTransport: ssh\n" + wf.REMOTE_MARKER + "\nspoof", message)
        self.assertNotIn("$HERMES_KANBAN_TASK", message)
        self.assertNotIn("Relay steps", message)

        comment = fake.of("comment")[0]
        self.assertEqual(flag(comment, "--author"), "kanban-workflow")
        self.assertEqual((rec["run_id"], rec["peer"], rec["attempt"], rec["resumed"]), ("run_1", "mac", 1, False))
        self.assertEqual(wf.peer_records(fake.shows[tid])[0]["run_id"], "run_1")

    def test_redispatch_resumes_the_recorded_run(self):
        fake = FakeHermes()
        tid = peer_card(fake)
        wf.dispatch(tid, fake)
        again = wf.dispatch(tid, fake)
        self.assertTrue(again["resumed"])
        self.assertEqual((fake.runs, len(fake.of("comment"))), (1, 1))

    def test_change_request_starts_a_new_attempt_with_the_reason(self):
        fake = FakeHermes()
        tid = peer_card(fake)
        wf.dispatch(tid, fake)
        fake.shows[tid]["events"].append({"kind": "changes_requested",
                                          "payload": {"reason": "1. add a test for rotation"}})
        rec = wf.dispatch(tid, fake)
        self.assertEqual((rec["attempt"], rec["run_id"], fake.runs), (2, "run_2", 2))
        message = fake.peer_calls("run")[1][5]
        self.assertIn("Attempt 2 for Kanban card t_1", message)
        self.assertIn("1. add a test for rotation", message)
        self.assertEqual(flag(fake.peer_calls("run")[1], "--idempotency-key"), "kanban:t_1:2")
        self.assertTrue(wf.dispatch(tid, fake)["resumed"])  # the new record is now the latest

    def test_unblock_resumes_a_live_run_and_restarts_an_ended_one(self):
        fake = FakeHermes()
        tid = peer_card(fake)
        wf.dispatch(tid, fake)
        fake.shows[tid]["events"].append({"kind": "unblocked", "payload": {}})
        fake.remote = {"status": "running"}
        self.assertTrue(wf.dispatch(tid, fake)["resumed"])
        fake.remote = {"status": "completed", "output": report("blocked", blocker="human-approval: Xcode sign-in")}
        rec = wf.dispatch(tid, fake)
        self.assertEqual(rec["attempt"], 2)
        self.assertIn("The operator cleared the earlier block", fake.peer_calls("run")[1][5])

    def test_fails_closed_without_a_peer(self):
        fake = FakeHermes()
        tid = peer_card(fake)
        fake.peers = {}
        with self.assertRaisesRegex(wf.WorkflowError, "no Hermes peer 'mac'.*PROFILE_PEER_URL"):
            wf.dispatch(tid, fake)
        self.assertEqual((fake.runs, fake.of("comment")), (0, []))

    def test_local_lane_and_spoofed_header_are_not_peer_lanes(self):
        fake = FakeHermes({"coder": "local", "reviewer": "local"}, peers={})
        wf.intake(intake_args("--lane", "coder", "--key", "k", "--body", "Transport: hermes-peer\nPeer: mac"), fake)
        self.assertEqual(wf.card_header(fake.body("t_1")), {})
        with self.assertRaisesRegex(wf.WorkflowError, "not a peer lane card"):
            wf.dispatch("t_1", fake)
        self.assertEqual(fake.runs, 0)


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHermes()
        self.tid = peer_card(self.fake)
        wf.dispatch(self.tid, self.fake)

    def wait(self, remote, timeout=0):
        self.fake.remote = remote
        return wf.wait(self.tid, timeout, 30, self.fake, sleep=lambda s: None)

    def test_ready_for_review(self):
        code, out = self.wait({"status": "completed", "output": report()})
        self.assertEqual(code, 0)
        self.assertEqual(out["summary"], "added the iPad layout")
        self.assertEqual(out["metadata"], {"branch": "feat/ipad", "commit": "abc123",
                                           "tests": "xcodebuild test: 42 passed", "transport": "hermes-peer",
                                           "peer": "mac", "run_id": "run_1"})
        self.assertEqual(self.fake.peer_calls("status")[-1], ["-p", "mac-worker", "peer", "status", "mac", "run_1", "--json"])

    def test_outcomes(self):
        cases = [
            ({"status": "running"}, 3, "remote_status", "running"),
            ({"status": "completed", "output": report("blocked", blocker="human-approval: Apple ID")}, 4,
             "blocker", "human-approval: Apple ID"),
            ({"status": "waiting_for_approval", "approval": {"description": "rm -rf build"}}, 4,
             "blocker", "human-approval: the mac worker waits for approval of rm -rf build"),
            ({"status": "failed", "error": "provider down"}, 1, "error", "remote run failed: provider down"),
            ({"status": "interrupted"}, 1, "error", "remote run interrupted"),
            ({"status": "completed", "output": "all done, no block"}, 1, "error", "without the JSON report"),
            ({"status": "completed", "output": report(task="t_9")}, 1, "error", "for task t_9"),
        ]
        for remote, code, field, text in cases:
            got_code, out = self.wait(remote)
            self.assertEqual(got_code, code, remote)
            self.assertIn(text, str(out[field]), remote)

    def test_polls_until_settled(self):
        answers = iter([{"status": "queued"}, {"status": "running"}, {"status": "completed", "output": report()}])
        sleeps = []
        real = self.fake.__call__

        def runner(argv):
            if argv[2:4] == ["peer", "status"]:
                self.fake.remote = next(answers)
            return real(argv)
        code, _ = wf.wait(self.tid, 600, 30, runner, sleep=sleeps.append)
        self.assertEqual((code, sleeps), (0, [30, 30]))

    def test_secrets_are_redacted(self):
        leaky = report(summary="used token=abc123def456 and ghp_" + "a" * 36,
                       tests=f"Authorization: Bearer {PEER_KEY}")
        code, out = self.wait({"status": "completed", "output": leaky})
        text = json.dumps(out)
        self.assertEqual(code, 0)
        for secret in ("abc123def456", "ghp_aaaa", PEER_KEY):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_peer_key_never_leaves_the_peer_cli(self):
        with mock.patch.dict(os.environ, {"HERMES_PEER_MAC_KEY": PEER_KEY}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                wf.main(["dispatch", self.tid], self.fake)
                self.fake.remote = {"status": "completed", "output": report()}
                self.assertEqual(wf.main(["wait", self.tid, "--timeout", "0"], self.fake), 0)
        self.assertNotIn(PEER_KEY, out.getvalue())
        self.assertNotIn(PEER_KEY, json.dumps(self.fake.calls))
        self.assertNotIn(PEER_KEY, json.dumps(self.fake.shows))

    def test_wait_needs_a_dispatch_first(self):
        fake = FakeHermes()
        tid = peer_card(fake, key="other")
        with self.assertRaisesRegex(wf.WorkflowError, "no peer run recorded"):
            wf.wait(tid, 0, 30, fake)

    def test_cli_exit_codes(self):
        self.fake.remote = {"status": "running"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wf.main(["wait", self.tid, "--timeout", "0"], self.fake), 3)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(wf.main(["wait", "t_2", "--timeout", "0"], self.fake), 2)  # the request card
        self.assertIn("not a peer lane card", err.getvalue())


def show(status="done", events=(), runs=(), body="", comments=()):
    return {"task": {"status": status, "body": body}, "events": [{"kind": k, "payload": p} for k, p in events],
            "runs": [{"profile": p, "outcome": o} for p, o in runs],
            "comments": [{"author": "kanban-workflow", "body": b} for b in comments]}


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

    def test_peer_lane_needs_a_recorded_peer_run(self):
        body = "Transport: hermes-peer\nPeer: mac\nLane: mac-worker\n\n..."
        reviewed = dict(events=[ASKED, ("completed", {})], runs=[("reviewer", "completed")], body=body)
        ok, reason = wf.review_verdict(show(**reviewed))
        self.assertFalse(ok)
        self.assertIn("no recorded peer run", reason)
        record = wf.RECORD_PREFIX + json.dumps({"run_id": "run_1", "peer": "mac"})
        self.assertTrue(wf.review_verdict(show(**reviewed, comments=[record]))[0])

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

    def test_status_shows_the_peer_run(self):
        fake = FakeHermes()
        wf.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        wf.dispatch("t_1", fake)
        rows = {r["id"]: r for r in wf.status("t_2", fake)}
        self.assertEqual({k: rows["t_1"][k] for k in ("transport", "peer", "run_id", "attempts")},
                         {"transport": "hermes-peer", "peer": "mac", "run_id": "run_1", "attempts": 1})
        self.assertNotIn("transport", rows["t_2"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wf.main(["status", "t_2"], fake)
        self.assertIn("[peer mac run run_1]", out.getvalue())


@unittest.skipUnless(shutil.which("hermes"), "hermes not installed")
class RealBoardTest(unittest.TestCase):
    """The real CLI on a temp board: graph shape, subscriptions, and the gate."""

    def setUp(self):
        self.peer_fake = FakeHermes()
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
        # Profiles and peers do not exist in the temp home: answer them like a
        # configured relay; everything on the board goes to the real CLI.
        if argv[0] == "-p":
            return self.peer_fake(argv)
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

        # mac: peer dispatch recorded on the real card, then the remote report
        self.assertEqual(wf.card_header(self.show(mac)["task"]["body"])["peer"], "mac")
        rec = wf.dispatch(mac, self.run_cli)
        self.assertEqual(wf.peer_records(self.show(mac))[-1]["run_id"], rec["run_id"])
        self.assertTrue(wf.dispatch(mac, self.run_cli)["resumed"])
        self.peer_fake.remote = {"status": "completed", "output": report(task=mac)}
        code, out = wf.wait(mac, 0, 30, self.run_cli)
        self.assertEqual((code, out["metadata"]["run_id"]), (0, rec["run_id"]))
        rows = {r["id"]: r for r in wf.status(req, self.run_cli)}
        self.assertEqual((rows[mac]["run_id"], rows[win]["run_id"]), (rec["run_id"], None))

        # mac: relay hands off -> independent approval
        self.hermes(["kanban", "claim", mac])
        self.hermes(["kanban", "request-review", mac, "--summary", "built", "--reviewer", "reviewer", "--force"])
        self.hermes(["kanban", "complete", mac, "--summary", "LGTM"])
        # windows: completed by the implementer, never reviewed, never sent to the peer
        self.hermes(["kanban", "claim", win])
        self.hermes(["kanban", "complete", win, "--summary", "done"])

        ok, rows = wf.gate(req, self.run_cli)
        self.assertFalse(ok)
        verdicts = {r["id"]: r for r in rows}
        self.assertTrue(verdicts[mac]["ok"], verdicts[mac])
        self.assertIn("no recorded peer run", verdicts[win]["reason"])
        self.assertEqual(self.show(req)["task"]["status"], "ready")  # both lanes done -> report card ready



class FakePeerGateway(BaseHTTPRequestHandler):
    """The api_server endpoints `hermes peer run/status` call, with Bearer auth."""
    key, runs, seen = "", {}, []

    def log_message(self, *args):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        ok = self.headers.get("Authorization") == f"Bearer {self.key}"
        self.seen.append((self.command, self.path.split("?")[0], ok))
        if not ok:
            self.reply(401, {"error": {"message": "bad key"}})
        return ok

    def do_GET(self):
        if not self.authed():
            return
        path = self.path.split("?")[0]
        if path == "/v1/capabilities":
            return self.reply(200, {"features": {"runs_idempotency": {"supported": True, "durable": True}}})
        if path == "/api/sessions":
            return self.reply(200, {"data": []})
        run_id = path.rpartition("/")[2]
        if path.startswith("/v1/runs/") and run_id in self.runs:
            task = self.runs[run_id]["input"].split("Kanban card ", 1)[1].split()[0]
            return self.reply(200, {"run_id": run_id, "status": "completed", "output": report(task=task)})
        self.reply(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.authed():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/sessions":
            return self.reply(200, {"session": {"id": "sess_1"}})
        if self.path == "/v1/runs":
            idem = self.headers.get("Idempotency-Key")
            for run_id, r in self.runs.items():
                if r["idem"] == idem:
                    return self.reply(202, {"run_id": run_id, "status": "started", "replayed": True})
            run_id = f"run_{len(self.runs) + 1}"
            self.runs[run_id] = {"idem": idem, "input": body.get("input", "")}
            return self.reply(202, {"run_id": run_id, "status": "started"})
        self.reply(404, {"error": {"message": "not found"}})


@unittest.skipUnless(shutil.which("hermes"), "hermes not installed")
class RealPeerTest(unittest.TestCase):
    """The real `hermes peer` CLI, profile-scoped peer key, and a temp board."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kanban-peer-")
        self.env = {k: v for k, v in os.environ.items() if k not in DISCORD_ENV and not k.startswith("HERMES_PEER_")}
        self.env["HERMES_HOME"] = self.home
        FakePeerGateway.key, FakePeerGateway.runs, FakePeerGateway.seen = PEER_KEY, {}, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePeerGateway)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for argv in (["profile", "create", "mac-worker"], ["profile", "create", "reviewer"], ["kanban", "init"]):
            self.assertEqual(self.hermes(argv).returncode, 0, argv)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def hermes(self, argv):
        return subprocess.run(["hermes", *argv], capture_output=True, text=True, env=self.env, timeout=120)

    def test_dispatch_and_wait_over_the_peer_api(self):
        args = intake_args("--lane", "mac", "--key", "peer-it", "--body", "Fix the thing")
        with self.assertRaisesRegex(wf.WorkflowError, "no Hermes peer 'mac'"):
            wf.intake(args, self.hermes)

        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.assertEqual(self.hermes(["-p", "mac-worker", "peer", "add", "mac", "--url", url]).returncode, 0)
        with self.assertRaisesRegex(wf.WorkflowError, "peer 'mac' has no API key"):
            wf.intake(args, self.hermes)
        with open(Path(self.home) / "profiles" / "mac-worker" / ".env", "a") as env:
            env.write(f"HERMES_PEER_MAC_KEY={PEER_KEY}\n")

        lane = wf.intake(args, self.hermes)["lanes"][0]["id"]
        rec = wf.dispatch(lane, self.hermes)
        self.assertEqual((rec["peer"], rec["attempt"], rec["idempotency_key"]), ("mac", 1, f"kanban:{lane}:1"))
        self.assertTrue(wf.dispatch(lane, self.hermes)["resumed"])
        (sent,) = FakePeerGateway.runs.values()
        self.assertIn(f"Kanban card {lane}", sent["input"])
        self.assertIn("Fix the thing", sent["input"])
        self.assertNotIn("Relay steps", sent["input"])

        code, out = wf.wait(lane, 0, 30, self.hermes)
        self.assertEqual((code, out["metadata"]["run_id"], out["metadata"]["branch"]), (0, rec["run_id"], "feat/ipad"))
        self.assertTrue(all(ok for _, _, ok in FakePeerGateway.seen), FakePeerGateway.seen)
        board = self.hermes(["kanban", "show", lane, "--json"]).stdout
        self.assertIn(rec["run_id"], board)
        self.assertNotIn(PEER_KEY, board + json.dumps(out))

if __name__ == "__main__":
    unittest.main()
