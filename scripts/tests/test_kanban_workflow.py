"""Tests for the kanban-workflow skill (hermes/skills/kanban-workflow).

Central writer, evidence-only peers: the coordinator's code card pushes an exact
SHA, the Mac/Windows relays have their peers test that SHA and return evidence,
round cards open the next fix round or the review, and the report card gates on
the final SHA and an independent approval.

Unit tests drive the script with a fake `hermes` runner and real `git` on temp
repositories. The integration tests run the real `hermes kanban` and `hermes
peer` CLIs against a throwaway HERMES_HOME (never the live board or peers) and a
local fake peer gateway, and skip when Hermes is not installed.

Run: python3 -m unittest discover -s scripts/tests -p 'test_kanban_workflow.py'
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
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
REPO_URL = "https://github.com/acme/app.git"
SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "89abcdef0123456789abcdef0123456789abcdef"


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


PEER_KEY = "sk-live-0123456789abcdefSECRET"  # must never reach argv, output or the board
PEER_LIST = "mac\thttp://mac.tailnet:8642\t[key set]\nwindows\thttp://win.tailnet:8642\t[key set]\n"


class FakeHermes:
    """Records argv. Answers `config get` from a backend map, `peer list` from a
    per-profile listing, `peer run/status` from a scripted remote (per run id when
    set), and keeps a tiny board (create/show/comment/link/notify) keyed by
    idempotency key."""

    def __init__(self, backends=None, peers=None):
        self.calls = []
        self.backends = backends if backends is not None else {
            "mac-worker": "local", "windows-worker": "local", "reviewer": "local", "default": "local"}
        self.peers = peers if peers is not None else {"mac-worker": PEER_LIST, "windows-worker": PEER_LIST}
        self.cards, self.shows, self.subs = {}, {}, {}
        self.remote = {"status": "running"}
        self.remote_by_run = {}
        self.runs = []
        self.fail_comments = False
        self.fail_peer_run = False

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
                if self.fail_peer_run:
                    return done(1, err="Could not reach peer")
                idem = rest[rest.index("--idempotency-key") + 1]
                for r in self.runs:
                    if r["idem"] == idem:
                        return done(0, json.dumps({"run_id": r["run_id"], "session_id": "sess_1", "replayed": True}))
                run_id = "run_" + idem.split(":", 1)[1].replace(":", "_")
                self.runs.append({"idem": idem, "run_id": run_id, "message": rest[3], "profile": profile})
                return done(0, json.dumps({"peer": rest[2], "run_id": run_id, "session_id": "sess_1",
                                           "status": "started", "idempotency_key": idem, "replayed": False}))
            if rest[:2] == ["peer", "status"]:
                remote = self.remote_by_run.get(rest[3], self.remote)
                return done(0, json.dumps({"peer": rest[2], "run_id": rest[3], **remote}))
        if argv[:2] == ["kanban", "create"]:
            key = argv[argv.index("--idempotency-key") + 1]
            parents = [argv[i + 1] for i, a in enumerate(argv) if a == "--parent"]
            card = self.cards.setdefault(key, {"id": f"t_{len(self.cards) + 1}", "title": argv[2]})
            self.shows.setdefault(card["id"], {
                "task": {"id": card["id"], "title": argv[2], "status": "todo" if parents else "ready",
                         "assignee": argv[argv.index("--assignee") + 1], "body": argv[argv.index("--body") + 1]},
                "parents": parents, "comments": [], "events": [], "runs": []})
            return done(0, json.dumps(card))
        if argv[:2] == ["kanban", "show"]:
            show = json.loads(json.dumps(self.shows[argv[2]]))
            show["children"] = [tid for tid, s in self.shows.items() if argv[2] in s["parents"]]
            return done(0, json.dumps(show))
        if argv[:2] == ["kanban", "comment"]:
            if self.fail_comments:
                return done(1, err="kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI")
            show = self.shows[argv[2]]
            show["comments"].append({"author": argv[argv.index("--author") + 1], "body": argv[3]})
            show["events"].append({"kind": "commented", "payload": {}})
            return done(0, "ok")
        if argv[:2] == ["kanban", "link"]:
            parents = self.shows[argv[3]]["parents"]
            if argv[2] not in parents:
                parents.append(argv[2])
            return done(0, "Linked")
        if argv[:2] == ["kanban", "notify-subscribe"]:
            self.subs.setdefault(argv[2], []).append({
                "chat_id": flag(argv, "--chat-id"), "platform": flag(argv, "--platform"),
                "thread_id": flag(argv, "--thread-id") if "--thread-id" in argv else None,
                "delivery_mode": flag(argv, "--delivery-mode")})
            return done(0, "Subscribed")
        if argv[:2] == ["kanban", "notify-list"]:
            return done(0, json.dumps(self.subs.get(argv[2], [])))
        raise AssertionError(f"unexpected call {argv}")

    def of(self, verb):
        return [c for c in self.calls if c[:2] == ["kanban", verb]]

    def peer_calls(self, verb):
        return [c for c in self.calls if c[:1] == ["-p"] and c[2:4] == ["peer", verb]]

    def body(self, task_id):
        return self.shows[task_id]["task"]["body"]

    def finish(self, task_id, profile=None):
        """What kanban_complete does: the card is done, its run completed."""
        t = self.shows[task_id]["task"]
        t["status"] = "done"
        self.shows[task_id]["runs"].append({"profile": profile or t["assignee"], "outcome": "completed"})
        self.shows[task_id]["events"].append({"kind": "completed", "payload": {}})

    def by_title(self, prefix):
        return [tid for tid, s in self.shows.items() if s["task"]["title"].startswith(prefix)]


def intake_args(*extra, repo=REPO_URL):
    return wf.parser().parse_args(["intake", "--title", "Build the iPad app", "--repo", repo, *extra])


def flag(argv, name):
    return argv[argv.index(name) + 1]


def evidence(status="pass", task="t_2", sha=SHA, fenced=False, **extra):
    fields = {"task": task, "status": status, "sha_tested": sha, "platform": "iOS 19 simulator",
              "os": "macOS 16.1", "tools": "Xcode 27.0",
              "commands": [{"cmd": "xcodebuild test -scheme App", "exit_code": 0 if status == "pass" else 65}],
              "tests": {"passed": 42, "failed": 0 if status == "pass" else 2, "skipped": 1},
              "failures": [] if status == "pass" else ["LoginTests.testRotation: expected 1, got 2"],
              "artifacts": ["~/hermes-work/t_2/build/App.app"], "device": "iPhone 17 simulator",
              "dirty_tree": False, "blocker": "", "summary": "built and tested", **extra}
    if fenced:
        return f"Done.\n\n```json\n{json.dumps(fields)}\n```\nAnything else?\n"
    return f"Validation done.\nBEGIN-EVIDENCE\n{json.dumps(fields)}\nEND-EVIDENCE\n"


class StateDirMixin:
    """A temp run ledger per test, never the live ~/.hermes."""

    def setUp(self):
        super().setUp()
        self._state = tempfile.mkdtemp(prefix="kanban-workflow-state-")
        patcher = mock.patch.dict(os.environ, {"HERMES_WORKFLOW_STATE": self._state}, clear=False)
        patcher.start()
        os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._state, True)


def graph(fake, *extra, key="k", body="full ask"):
    """Round 1 on a fake board: t_1 code, t_2 mac, t_3 win, t_4 round, t_5 report."""
    return wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", key, "--body", body, *extra), fake)


def push(fake, code_id="t_1", sha=SHA, notes="changed the login screen"):
    """What record-sha leaves behind, without git."""
    h = wf.header_of(fake.shows[code_id])
    wf.put_record(fake, code_id, "code-sha", {"rid": f"code-sha:{sha}", "sha": sha, "branch": h["branch"],
                                              "repo": h["repo"], "round": int(h["round"]), "notes": notes})
    fake.finish(code_id)


def validate(fake, tid, output):
    """Relay: dispatch, wait until evidence, kanban_complete on exit 0."""
    wf.dispatch(tid, fake)
    run_id = wf.peer_runs(card(fake, tid))[-1]["run_id"]
    fake.remote_by_run[run_id] = {"status": "completed", "output": output.replace("t_2", tid)}
    code, out = wf.wait(tid, 0, 30, fake, sleep=lambda s: None)
    if code == 0:
        fake.finish(tid)
    return code, out


def card(fake, tid):
    return wf.card(fake, tid)


class IntakeTest(StateDirMixin, unittest.TestCase):
    def test_builds_round_one_and_the_report_and_subscribes_the_thread(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--body", "full ask"), fake)

        creates = fake.of("create")
        self.assertEqual([(c[2], flag(c, "--assignee")) for c in creates], [
            ("[code 1] Build the iPad app", "default"), ("[mac 1] Build the iPad app", "mac-worker"),
            ("[win 1] Build the iPad app", "windows-worker"), ("[round 1] Build the iPad app", "default"),
            ("Report: Build the iPad app", "default")])
        parents = lambda c: [c[i + 1] for i, a in enumerate(c) if a == "--parent"]  # noqa: E731
        self.assertEqual([parents(c) for c in creates], [[], ["t_1"], ["t_1"], ["t_2", "t_3"], ["t_4"]])
        self.assertEqual(plan["key"], "discord:111:333")
        self.assertEqual(flag(creates[-1], "--idempotency-key"), "discord:111:333:request")
        self.assertEqual(flag(creates[1], "--idempotency-key"), "discord:111:333:mac-worker:1")
        self.assertEqual((plan["branch"], plan["request"]["id"], plan["round"]["id"]),
                         ("feat/build-the-ipad-app", "t_5", "t_4"))
        self.assertEqual(flag(creates[0], "--max-runtime"), "6h")
        self.assertEqual(flag(creates[1], "--max-runtime"), "2h")

        modes = {c[2]: flag(c, "--delivery-mode") for c in fake.of("notify-subscribe")}
        self.assertEqual(modes, {"t_1": "notify", "t_2": "notify", "t_3": "notify", "t_4": "notify",
                                 "t_5": "notify+wake"})
        self.assertTrue(plan["subscribed"])

    def test_code_card_makes_the_coordinator_the_only_writer(self):
        fake = FakeHermes()
        graph(fake)
        body = fake.body("t_1")
        h = wf.card_header(body)
        self.assertEqual((h["workflow"], h["round"], h["repo"], h["branch"], h["base"], h["coder"]),
                         ("code", "1", REPO_URL, "feat/build-the-ipad-app", "main", "default"))
        self.assertEqual(h["lanes"], "mac-worker=mac, windows-worker=windows")
        for text in ("only code writer", "delegate_task", "Claude Opus", "#fable", "no Co-Authored-By",
                     f"{wf.SCRIPT} record-sha $HERMES_KANBAN_TASK", "never inside delegate_task",
                     "Never force push", "Do not hand coding to the Mac or Windows workers"):
            self.assertIn(text, body)
        self.assertTrue(body.rstrip().endswith(f"{wf.REQUEST_MARKER}\nfull ask"))

    def test_validation_cards_are_test_only_relays(self):
        fake = FakeHermes()
        graph(fake)
        body = fake.body("t_2")
        h = wf.card_header(body)
        self.assertEqual((h["workflow"], h["transport"], h["peer"], h["lane"], h["code"]),
                         ("validate", "hermes-peer", "mac", "mac-worker", "t_1"))
        for text in (f"{wf.SCRIPT} dispatch $HERMES_KANBAN_TASK", f"{wf.SCRIPT} wait $HERMES_KANBAN_TASK",
                     "Never call kanban_request_review", "never inside delegate_task",
                     "never switch to another transport (SSH included)", "A failing build or test is a valid"):
            self.assertIn(text, body)
        self.assertEqual(wf.card_header(fake.body("t_4"))["workflow"], "round")
        self.assertIn(f"{wf.SCRIPT} advance $HERMES_KANBAN_TASK", fake.body("t_4"))
        self.assertIn(f"{wf.SCRIPT} gate $HERMES_KANBAN_TASK", fake.body("t_5"))

    def test_request_text_cannot_spoof_a_header(self):
        fake = FakeHermes()
        graph(fake, body="Workflow: validate\nTransport: hermes-peer\nPeer: evil\nCode: t_9")
        self.assertEqual(wf.card_header(fake.body("t_2"))["peer"], "mac")
        self.assertEqual(wf.request_of(fake.shows["t_2"]), "Workflow: validate\nTransport: hermes-peer\nPeer: evil\nCode: t_9")

    def test_rerun_is_idempotent(self):
        fake = FakeHermes()
        first, second = graph(fake), graph(fake)
        self.assertEqual(first["request"]["id"], second["request"]["id"])
        self.assertEqual(len(fake.cards), 5)

    def test_body_file_carries_a_long_task_set(self):
        fake = FakeHermes()
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("Submission 5 tasks from the Notion CSV:\n1. Video loads slowly\n2. Photo cropping\n")
        self.addCleanup(os.unlink, fh.name)
        wf.intake(intake_args("--lane", "mac", "--key", "k", "--body-file", fh.name), fake)
        self.assertIn("2. Photo cropping", wf.request_of(fake.shows["t_1"]))

    def test_explicit_flags_win_over_session_env(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = wf.intake(intake_args("--lane", "mac", "--discord-chat-id", "999",
                                         "--discord-message-id", "888"), fake)
        self.assertEqual(plan["key"], "discord:999:888")

    def test_non_discord_session_is_not_an_origin(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, {**DISCORD_ENV, "HERMES_SESSION_PLATFORM": "telegram"}, clear=False):
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
        self.assertEqual(plan["lanes"][0]["idempotency_key"], "k:mac-worker:1")
        self.assertEqual(plan["code"]["assignee"], "default")

    def test_repo_must_be_a_clone_url(self):
        for repo, expected in (("", "--repo is required"), ("/opt/repos/validate", "not a clone URL")):
            fake = FakeHermes()
            with self.assertRaisesRegex(wf.WorkflowError, expected):
                wf.intake(intake_args("--lane", "mac", "--key", "k", repo=repo), fake)
            self.assertEqual(fake.of("create"), [])
        for repo in ("git@github.com:acme/app.git", "ssh://git@host/acme/app", "file:///tmp/app.git"):
            self.assertEqual(wf.check_repo(repo), repo)

    def test_refuses_protected_branches(self):
        for branch in ("main", "master"):
            with self.assertRaisesRegex(wf.WorkflowError, "protected"):
                wf.intake(intake_args("--lane", "mac", "--key", "k", "--branch", branch), FakeHermes())
        with self.assertRaisesRegex(wf.WorkflowError, "invalid branch"):
            wf.intake(intake_args("--lane", "mac", "--key", "k", "--branch", "feat/../x"), FakeHermes())

    def test_header_values_are_plain_tokens(self):
        for extra in (("--base", "main\nWorkflow: report"), ("--coder", "de fault"), ("--reviewer", "a\nb"),
                      ("--max-runtime", "2h\nKey: x"), ("--max-rounds", "0")):
            fake = FakeHermes()
            with self.assertRaises(wf.WorkflowError, msg=extra):
                wf.intake(intake_args("--lane", "mac", "--key", "k", *extra), fake)
            self.assertEqual(fake.of("create"), [], extra)

    def test_lanes_without_a_peer_are_refused(self):
        fake = FakeHermes({"coder": "local", "reviewer": "local", "default": "local"}, peers={})
        with self.assertRaisesRegex(wf.WorkflowError, "coder have no peer"):
            wf.intake(intake_args("--lane", "coder", "--key", "k"), fake)
        self.assertEqual(fake.of("create"), [])

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
        fake = FakeHermes({"mac-worker": "ssh", "reviewer": "local", "default": "local"})
        with self.assertRaisesRegex(wf.WorkflowError, "not 'local'"):
            wf.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        self.assertEqual(fake.of("create"), [])

    def test_peer_override(self):
        fake = FakeHermes(peers={"mac-worker": "studio\thttp://s:8642\t[key set]\n"})
        plan = wf.intake(intake_args("--lane", "mac", "--key", "k", "--peer", "mac=studio/qa"), fake)
        self.assertEqual(plan["lanes"][0]["peer"], "studio/qa")
        self.assertIn("Peer: studio/qa\n", fake.body("t_2"))
        for bad in ("mac", "linux=box", "mac=Bad Name"):
            with self.assertRaises(wf.WorkflowError, msg=bad):
                wf.intake(intake_args("--lane", "mac", "--key", "k2", "--peer", bad), fake)

    def test_refuses_missing_profiles_and_dependent_review(self):
        with self.assertRaisesRegex(wf.WorkflowError, "'reviewer' does not exist"):
            wf.intake(intake_args("--lane", "mac", "--key", "k"),
                      FakeHermes({"mac-worker": "local", "default": "local"}))
        for extra in (("--reviewer", "mac-worker"), ("--reviewer", "default"), ("--coder", "reviewer")):
            with self.assertRaisesRegex(wf.WorkflowError, "review must be independent"):
                wf.intake(intake_args("--lane", "mac", "--key", "k", *extra), FakeHermes())


class RecordShaTest(StateDirMixin, unittest.TestCase):
    """Real git: a bare remote, a clone on the card's branch."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="kanban-workflow-git-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        git = lambda *a, cwd=self.tmp: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
        self.git = git
        git("init", "--bare", "-b", "main", self.remote)
        git("clone", self.remote, self.work)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=self.work)
        Path(self.work, "a.txt").write_text("1\n")
        git("add", "a.txt", cwd=self.work)
        git("commit", "-m", "chore: init", cwd=self.work)
        git("push", "origin", "main", cwd=self.work)
        git("checkout", "-b", "feat/build-the-ipad-app", cwd=self.work)
        Path(self.work, "a.txt").write_text("2\n")
        git("commit", "-am", "feat: change", cwd=self.work)
        self.fake = FakeHermes()
        wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "k", repo=f"file://{self.remote}"),
                  self.fake)

    def record(self):
        return wf.record_sha("t_1", self.work, "notes: check rotation; token=abcdef123456", self.fake)

    def test_requires_a_push_a_clean_tree_and_the_card_branch(self):
        with self.assertRaisesRegex(wf.WorkflowError, "push first"):
            self.record()
        self.git("push", "origin", "feat/build-the-ipad-app", cwd=self.work)
        Path(self.work, "a.txt").write_text("dirty\n")
        with self.assertRaisesRegex(wf.WorkflowError, "uncommitted tracked changes"):
            self.record()
        self.git("checkout", "a.txt", cwd=self.work)
        self.git("checkout", "main", cwd=self.work)
        with self.assertRaisesRegex(wf.WorkflowError, "checkout is on 'main'"):
            self.record()
        self.git("checkout", "feat/build-the-ipad-app", cwd=self.work)
        out = self.record()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out["metadata"], {"state": "SHA_PUSHED", "commit": head,
                                           "branch": "feat/build-the-ipad-app", "round": 1})
        self.assertTrue(out["summary"].startswith(f"SHA_PUSHED {head[:12]}"))
        rec = wf.code_sha(self.fake, "t_1")
        self.assertEqual(rec["sha"], head)
        self.assertNotIn("abcdef123456", rec["notes"])
        self.assertTrue(wf.ledger_path("t_1").exists())

    def test_refused_inside_delegate_task(self):
        with mock.patch.dict(os.environ, {"HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
            with self.assertRaisesRegex(wf.WorkflowError, "not inside delegate_task"):
                self.record()

    def test_only_code_cards(self):
        with self.assertRaisesRegex(wf.WorkflowError, "not a code card"):
            wf.record_sha("t_2", self.work, "", self.fake)


class DispatchTest(StateDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        graph(self.fake, body="Fix /opt/repos/app per the Notion CSV")

    def test_needs_the_pushed_sha_first(self):
        with self.assertRaisesRegex(wf.WorkflowError, "no recorded SHA"):
            wf.dispatch("t_2", self.fake)
        self.assertEqual(self.fake.runs, [])

    def test_sends_a_test_only_task_for_the_exact_sha_and_records_the_run(self):
        push(self.fake)
        rec = wf.dispatch("t_2", self.fake)
        run = self.fake.peer_calls("run")[0]
        self.assertEqual(run[:5], ["-p", "mac-worker", "peer", "run", "mac"])
        self.assertEqual(flag(run, "--idempotency-key"), "kanban:t_2:1")
        message = run[5]
        self.assertTrue(message.startswith("You are the Mac worker's own Hermes agent. Kanban card t_2"), message[:80])
        for text in (f"Commit: {SHA}", f"Repository: {REPO_URL}", f"git checkout --detach {SHA}",
                     "you never change source code", "Forbidden: editing or adding tracked files, committing, pushing",
                     "BEGIN-EVIDENCE", "END-EVIDENCE", "~/hermes-work/t_2", "changed the login screen",
                     "Paths in the request below are on the\n   coordinator", "Fix /opt/repos/app per the Notion CSV"):
            self.assertIn(text, message)
        self.assertNotIn("$HERMES_KANBAN_TASK", message)
        self.assertNotIn("Relay steps", message)
        self.assertEqual((rec["run_id"], rec["peer"], rec["attempt"], rec["sha"], rec["resumed"]),
                         ("run_t_2_1", "mac", 1, SHA, False))
        self.assertEqual(wf.peer_runs(card(self.fake, "t_2"))[-1]["run_id"], "run_t_2_1")
        self.assertEqual(flag(self.fake.of("comment")[-1], "--author"), "kanban-workflow")

    def test_redispatch_resumes_the_recorded_run(self):
        push(self.fake)
        wf.dispatch("t_2", self.fake)
        self.assertTrue(wf.dispatch("t_2", self.fake)["resumed"])
        self.assertEqual(len(self.fake.peer_calls("run")), 1)

    def test_a_failed_card_write_keeps_the_run_in_the_ledger(self):
        push(self.fake)
        self.fake.fail_comments = True
        rec = wf.dispatch("t_2", self.fake)
        self.assertFalse(rec["recorded_on_card"])
        self.assertEqual([c["body"][:9] for c in self.fake.shows["t_2"]["comments"]], [])
        again = wf.dispatch("t_2", self.fake)  # recovery: the ledger knows the run
        self.assertTrue(again["resumed"])
        self.assertEqual(len(self.fake.peer_calls("run")), 1)
        self.fake.fail_comments = False
        wf.dispatch("t_2", self.fake)  # the next command mirrors it to the card
        self.assertEqual(wf.card_records(self.fake.shows["t_2"])[-1]["run_id"], "run_t_2_1")
        wf.dispatch("t_2", self.fake)
        self.assertEqual(len([r for r in wf.card_records(self.fake.shows["t_2"]) if r["type"] == "peer-run"]), 1)

    def test_a_crash_before_the_run_id_resends_the_same_key(self):
        push(self.fake)
        self.fake.fail_peer_run = True
        with self.assertRaises(wf.WorkflowError):
            wf.dispatch("t_2", self.fake)
        self.assertEqual(wf.read_ledger("t_2")[-1]["run_id"], "")
        self.fake.fail_peer_run = False
        rec = wf.dispatch("t_2", self.fake)
        self.assertEqual((rec["attempt"], rec["idempotency_key"]), (1, "kanban:t_2:1"))
        self.assertEqual([flag(c, "--idempotency-key") for c in self.fake.peer_calls("run")],
                         ["kanban:t_2:1", "kanban:t_2:1"])

    def test_refused_inside_delegate_task_before_any_side_effect(self):
        push(self.fake)
        with mock.patch.dict(os.environ, {"HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
            with self.assertRaisesRegex(wf.WorkflowError, "not inside delegate_task"):
                wf.dispatch("t_2", self.fake)
        self.assertEqual((self.fake.runs, wf.read_ledger("t_2")), ([], []))

    def test_unblock_resumes_a_live_run_and_restarts_an_ended_one(self):
        push(self.fake)
        wf.dispatch("t_2", self.fake)
        self.fake.shows["t_2"]["events"].append({"kind": "unblocked", "payload": {}})
        self.fake.remote = {"status": "waiting_for_approval"}
        self.assertTrue(wf.dispatch("t_2", self.fake)["resumed"])
        self.fake.remote = {"status": "completed", "output": evidence("blocked", blocker="device: no iPhone")}
        rec = wf.dispatch("t_2", self.fake)
        self.assertEqual((rec["attempt"], rec["run_id"]), (2, "run_t_2_2"))
        self.assertIn("The operator cleared the earlier block", self.fake.runs[-1]["message"])
        self.assertIn(f"git checkout --detach {SHA}", self.fake.runs[-1]["message"])

    def test_fails_closed_without_a_peer(self):
        push(self.fake)
        self.fake.peers = {}
        with self.assertRaisesRegex(wf.WorkflowError, "no Hermes peer 'mac'.*PROFILE_PEER_URL"):
            wf.dispatch("t_2", self.fake)
        self.assertEqual(self.fake.runs, [])

    def test_only_validation_cards(self):
        push(self.fake)
        for tid in ("t_1", "t_4", "t_5"):
            with self.assertRaisesRegex(wf.WorkflowError, "not a validation card"):
                wf.dispatch(tid, self.fake)


class WaitTest(StateDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        graph(self.fake)
        push(self.fake)
        wf.dispatch("t_2", self.fake)

    def wait(self, remote=None, timeout=0, by_run=None):
        if remote is not None:
            self.fake.remote = remote
        self.fake.remote_by_run.update(by_run or {})
        return wf.wait("t_2", timeout, 30, self.fake, sleep=lambda s: None)

    def test_pass_records_structured_evidence(self):
        code, out = self.wait({"status": "completed", "output": evidence()})
        self.assertEqual((code, out["verdict"]), (0, "pass"))
        self.assertEqual(out["summary"], f"MAC_PASSED at {SHA[:12]}: 42 passed, 0 failed, 1 skipped")
        self.assertEqual(out["metadata"], {"state": "MAC_PASSED", "verdict": "pass", "commit": SHA,
                                           "lane": "mac-worker", "transport": "hermes-peer", "peer": "mac",
                                           "run_id": "run_t_2_1", "tests": {"passed": 42, "failed": 0, "skipped": 1}})
        ev = wf.latest(card(self.fake, "t_2"), "evidence")
        self.assertEqual((ev["status"], ev["sha"], ev["sha_tested"], ev["device"], ev["run_id"]),
                         ("pass", SHA, SHA, "iPhone 17 simulator", "run_t_2_1"))
        self.assertEqual(ev["commands"], [{"cmd": "xcodebuild test -scheme App", "exit_code": 0}])
        self.assertIn("evidence ", self.fake.shows["t_2"]["comments"][-1]["body"][:9])
        self.wait()  # a rerun does not record the evidence twice
        self.assertEqual(len(wf.records(card(self.fake, "t_2"), "evidence")), 1)

    def test_a_failing_test_is_evidence_not_an_error(self):
        code, out = self.wait({"status": "completed", "output": evidence("fail")})
        self.assertEqual((code, out["verdict"], out["metadata"]["state"]), (0, "fail", "MAC_FAILED"))
        self.assertIn("LoginTests.testRotation", out["summary"])

    def test_tolerant_parsing(self):
        bare = json.dumps({"task": "t_2", "status": "passed", "sha_tested": SHA[:10]})
        for output in (evidence(fenced=True), f"Summary first.\n{bare}\nThanks.",
                       evidence().replace("BEGIN-EVIDENCE\n", "BEGIN-EVIDENCE\n```json\n").replace("\nEND", "\n```\nEND"),
                       evidence() + "\nSome trailing prose { not json"):
            self.assertEqual(wf.extract_report(output)["task"], "t_2", output[:60])
        code, out = self.wait({"status": "completed", "output": f"done\n{bare}\n"})
        self.assertEqual((code, out["verdict"]), (0, "pass"))

    def test_dirty_tree_and_failed_counts_turn_a_pass_into_a_fail(self):
        raw = json.loads(evidence().split("\n")[2])
        for extra, text in (({"dirty_tree": "M ios/Podfile.lock"}, "tracked files changed"),
                            ({"tests": {"passed": 3, "failed": 1}}, "1 test(s) failed")):
            ev = wf.normalize_evidence({**raw, **extra}, "t_2", SHA, "mac-worker")
            self.assertEqual(ev["status"], "fail")
            self.assertIn(text, " ".join(ev["failures"]))

    def test_missing_evidence_asks_the_worker_once_for_the_report(self):
        code, out = self.wait({"status": "completed", "output": "All done, it works."},
                              by_run={"run_t_2_1_report": {"status": "running"}})
        self.assertEqual(code, 3)
        report_run = self.fake.runs[-1]
        self.assertEqual(report_run["idem"], "kanban:t_2:1:report")
        self.assertIn("Do not start the build or tests again", report_run["message"])
        self.assertEqual(out["run_id"], "run_t_2_1_report")
        code, out = self.wait(by_run={"run_t_2_1_report": {"status": "completed", "output": evidence()}})
        self.assertEqual((code, out["run_id"]), (0, "run_t_2_1_report"))

    def test_unusable_runs_restart_automatically_then_fail(self):
        self.fake.remote = {"status": "interrupted"}
        code, out = self.wait()
        self.assertEqual(code, 1)
        self.assertEqual([r["idem"] for r in self.fake.runs],
                         ["kanban:t_2:1", "kanban:t_2:2", "kanban:t_2:3"])
        self.assertIn("previous attempt ended without usable evidence", self.fake.runs[-1]["message"])
        self.assertIn("remote run interrupted", out["error"])

    def test_wrong_sha_or_task_is_not_evidence(self):
        self.fake.remote_by_run = {"run_t_2_2": {"status": "completed", "output": evidence()}}
        code, out = self.wait({"status": "completed", "output": evidence(sha=SHA2)})
        self.assertEqual((code, out["run_id"]), (0, "run_t_2_2"))  # attempt 2 tested the right SHA
        for raw, text in (({"task": "t_9", "status": "pass", "sha_tested": SHA}, "for task t_9"),
                          ({"status": "pass|fail|blocked", "sha_tested": SHA}, "is not pass, fail or blocked"),
                          ({"status": "pass"}, "no sha_tested")):
            with self.assertRaisesRegex(wf.EvidenceError, text):
                wf.normalize_evidence(raw, "t_2", SHA, "mac-worker")

    def test_human_gates_block(self):
        cases = [
            ({"status": "completed", "output": evidence("blocked", blocker="human-approval: Apple ID sign-in")},
             "human-approval: Apple ID sign-in"),
            ({"status": "waiting_for_approval", "approval": {"description": "git reset --hard"}},
             "human-approval: the mac worker waits for approval of git reset --hard (card t_2, run run_t_2_1)"),
        ]
        for remote, text in cases:
            code, out = self.wait(remote)
            self.assertEqual(code, 4, remote)
            self.assertIn(text, out["blocker"])
        self.assertEqual(len(self.fake.runs), 1)

    def test_polls_until_settled(self):
        answers = iter([{"status": "queued"}, {"status": "running"}, {"status": "completed", "output": evidence()}])
        sleeps = []
        real = self.fake.__call__

        def runner(argv):
            if argv[2:4] == ["peer", "status"]:
                self.fake.remote = next(answers)
            return real(argv)
        code, _ = wf.wait("t_2", 600, 30, runner, sleep=sleeps.append)
        self.assertEqual((code, sleeps), (0, [30, 30]))

    def test_secrets_are_redacted(self):
        leaky = evidence(summary="used token=abc123def456 and ghp_" + "a" * 36,
                         failures=[f"Authorization: Bearer {PEER_KEY}"], status="fail")
        code, out = self.wait({"status": "completed", "output": leaky})
        stored = json.dumps(self.fake.shows["t_2"]) + wf.ledger_path("t_2").read_text() + json.dumps(out)
        self.assertEqual(code, 0)
        for secret in ("abc123def456", "ghp_aaaa", PEER_KEY):
            self.assertNotIn(secret, stored)
        self.assertIn("[REDACTED]", stored)

    def test_peer_key_never_leaves_the_peer_cli(self):
        with mock.patch.dict(os.environ, {"HERMES_PEER_MAC_KEY": PEER_KEY}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                wf.main(["dispatch", "t_2"], self.fake)
                self.fake.remote = {"status": "completed", "output": evidence()}
                self.assertEqual(wf.main(["wait", "t_2", "--timeout", "0"], self.fake), 0)
        self.assertNotIn(PEER_KEY, out.getvalue())
        self.assertNotIn(PEER_KEY, json.dumps(self.fake.calls))
        self.assertNotIn(PEER_KEY, json.dumps(self.fake.shows))

    def test_wait_needs_a_dispatch_first(self):
        with self.assertRaisesRegex(wf.WorkflowError, "no peer run recorded"):
            wf.wait("t_3", 0, 30, self.fake)

    def test_cli_exit_codes(self):
        self.fake.remote = {"status": "running"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wf.main(["wait", "t_2", "--timeout", "0"], self.fake), 3)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(wf.main(["wait", "t_4", "--timeout", "0"], self.fake), 2)
        self.assertIn("not a validation card", err.getvalue())


class RoundTest(StateDirMixin, unittest.TestCase):
    """Round cards: review when every lane passed, else the next fix round."""

    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            graph(self.fake, "--max-rounds", "2")
        push(self.fake)

    def lanes(self, mac="pass", win="pass", ids=("t_2", "t_3")):
        for tid, verdict in zip(ids, (mac, win)):
            self.assertEqual(validate(self.fake, tid, evidence(verdict, sha=self.sha))[0], 0)

    sha = SHA

    def test_all_pass_opens_the_review_before_the_round_completes(self):
        self.lanes()
        code, out = wf.advance("t_4", self.fake)
        self.assertEqual((code, out["state"]), (0, "VALIDATED"))
        (review,) = self.fake.by_title("Review: ")
        self.assertEqual(out["metadata"]["next"], {"review": review})
        h = wf.header_of(self.fake.shows[review])
        self.assertEqual((h["workflow"], h["commit"], h["round"], h["round-card"]), ("review", SHA, "1", "t_4"))
        self.assertEqual(self.fake.shows[review]["parents"], ["t_4"])
        self.assertEqual(self.fake.shows["t_5"]["parents"], ["t_4", review])
        self.assertEqual(self.fake.shows[review]["task"]["assignee"], "reviewer")
        self.assertEqual([s["delivery_mode"] for s in self.fake.subs[review]], ["notify"])
        self.assertEqual(wf.advance("t_4", self.fake)[1]["metadata"]["next"], {"review": review})  # idempotent
        self.assertEqual(len(self.fake.by_title("Review: ")), 1)

    def test_a_failure_opens_a_fix_round_with_the_evidence(self):
        self.lanes(win="fail")
        code, out = wf.advance("t_4", self.fake)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        nxt = out["metadata"]["next"]
        self.assertEqual(self.fake.shows[nxt["code"]]["task"]["title"], "[code 2] Build the iPad app")
        self.assertEqual(self.fake.shows[nxt["code"]]["parents"], ["t_4"])
        self.assertEqual(self.fake.shows[nxt["code"]]["task"]["assignee"], "default")
        body = self.fake.body(nxt["code"])
        self.assertIn("Fix this round (from round 1", body)
        self.assertIn("windows-worker (iOS 19 simulator; iPhone 17 simulator):", body)
        self.assertIn("LoginTests.testRotation", body)
        self.assertIn("$ xcodebuild test -scheme App  (exit 65)", body)
        self.assertTrue(body.rstrip().endswith("full ask"))
        self.assertEqual(sorted(nxt["lanes"]), ["mac-worker", "windows-worker"])
        self.assertEqual(self.fake.shows[nxt["round_card"]]["parents"], list(nxt["lanes"].values()))
        self.assertEqual(self.fake.shows["t_5"]["parents"], ["t_4", nxt["round_card"]])
        self.assertTrue(all(nxt[k] in self.fake.subs for k in ("code", "round_card")))
        self.assertEqual(wf.advance("t_4", self.fake)[1]["metadata"]["next"], nxt)  # idempotent

    def test_lane_without_evidence_blocks(self):
        self.lanes()
        self.fake.shows["t_3"]["comments"] = []
        os.unlink(wf.ledger_path("t_3"))
        code, out = wf.advance("t_4", self.fake)
        self.assertEqual(code, 4)
        self.assertIn("t_3 (windows-worker) is done without evidence", out["blocker"])

    def test_fix_loop_limit_blocks_and_an_unblock_grants_a_round(self):
        self.lanes(win="fail")
        nxt = wf.advance("t_4", self.fake)[1]["metadata"]["next"]
        self.fake.finish("t_4")
        self.sha = SHA2
        push(self.fake, nxt["code"], sha=SHA2)
        self.lanes(win="fail", ids=list(nxt["lanes"].values()))
        code, out = wf.advance(nxt["round_card"], self.fake)
        self.assertEqual(code, 4)
        self.assertIn("fix-loop: round 2 of 2 failed", out["blocker"])
        self.fake.shows[nxt["round_card"]]["events"].append({"kind": "unblocked", "payload": {}})
        code, out = wf.advance(nxt["round_card"], self.fake)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        self.assertTrue(self.fake.by_title("[code 3]"))

    def test_cli_exit_codes(self):
        self.lanes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(["advance", "t_4"], self.fake), 0)
        self.assertIn("VALIDATED", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wf.main(["advance", "t_2"], self.fake), 2)


class ReviewAndGateTest(StateDirMixin, unittest.TestCase):
    """Fix round, retest, independent review, gate: the whole loop without the operator."""

    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        graph(self.fake)

    def round(self, code_id, lanes, round_id, sha, verdicts):
        push(self.fake, code_id, sha=sha)
        for tid, v in zip(lanes, verdicts):
            self.assertEqual(validate(self.fake, tid, evidence(v, sha=sha))[0], 0)
        code, out = wf.advance(round_id, self.fake)
        self.assertEqual(code, 0, out)
        self.fake.finish(round_id)
        return out["metadata"]["next"]

    def gate(self):
        return wf.gate("t_5", self.fake)

    def test_fix_retest_review_gate(self):
        nxt = self.round("t_1", ["t_2", "t_3"], "t_4", SHA, ["pass", "fail"])
        ok, rows = self.gate()
        self.assertFalse(ok)
        nxt = self.round(nxt["code"], list(nxt["lanes"].values()), nxt["round_card"], SHA2, ["pass", "pass"])
        review = nxt["review"]
        ok, rows = self.gate()
        self.assertFalse(ok)
        self.assertIn("review is todo", " ".join(r["reason"] for r in rows if r["id"] == review))

        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "reviewer"}):
            code, out = wf.verdict(review, "approve", "", self.fake)
        self.assertEqual((code, out["metadata"]["state"], out["metadata"]["commit"]), (0, "REVIEW_APPROVED", SHA2))
        self.fake.finish(review)
        ok, rows = self.gate()
        self.assertTrue(ok, rows)
        self.assertEqual(len([r for r in rows if "passed" in r["reason"]]), 2)

        states = [(r["kind"], r["round"], r["state"]) for r in wf.status("t_5", self.fake)]
        self.assertEqual(states, [
            ("code", 1, "SHA_PUSHED"), ("validate", 1, "MAC_PASSED"), ("validate", 1, "WIN_FAILED"),
            ("round", 1, "FIX_REQUIRED"), ("code", 2, "SHA_PUSHED"), ("validate", 2, "MAC_PASSED"),
            ("validate", 2, "WIN_PASSED"), ("round", 2, "VALIDATED"), ("review", 2, "REVIEW_APPROVED"),
            ("report", None, "WAITING")])
        self.assertEqual(wf.status("t_1", self.fake), wf.status("t_5", self.fake))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(["gate", "t_5"], self.fake), 0)
            wf.main(["status", "t_5"], self.fake)
        self.assertIn("review gate: PASS", out.getvalue())
        self.assertIn("[peer mac run run_", out.getvalue())

    def test_changes_requested_opens_the_next_round(self):
        review = self.round("t_1", ["t_2", "t_3"], "t_4", SHA, ["pass", "pass"])["review"]
        with self.assertRaisesRegex(wf.WorkflowError, "needs --reason"):
            wf.verdict(review, "changes", "", self.fake)
        code, out = wf.verdict(review, "changes", "1. Add a test for the empty state", self.fake)
        self.assertEqual((code, out["metadata"]["state"]), (0, "CHANGES_REQUESTED"))
        nxt = out["metadata"]["next"]
        self.assertIn("1. Add a test for the empty state", self.fake.body(nxt["code"]))
        self.assertEqual(self.fake.shows[nxt["code"]]["parents"], [review])
        self.fake.finish(review)
        self.assertIn(nxt["round_card"], self.fake.shows["t_5"]["parents"])
        ok, rows = self.gate()
        self.assertFalse(ok)
        self.assertIn("status is todo", " ".join(r["reason"] for r in rows))

    def test_gate_failures(self):
        review = self.round("t_1", ["t_2", "t_3"], "t_4", SHA, ["pass", "pass"])["review"]
        wf.verdict(review, "approve", "", self.fake)
        self.fake.finish(review, profile="default")
        ok, rows = self.gate()
        self.assertFalse(ok)
        self.assertIn("approved by default, who wrote or relayed the work", " ".join(r["reason"] for r in rows))

        self.fake.shows[review]["runs"] = [{"profile": "reviewer", "outcome": "completed"}]
        self.assertTrue(self.gate()[0])
        self.fake.shows["t_3"]["comments"] = [c for c in self.fake.shows["t_3"]["comments"]
                                              if not c["body"].startswith("evidence ")]
        os.unlink(wf.ledger_path("t_3"))
        ok, rows = self.gate()
        self.assertFalse(ok)
        self.assertIn("done without evidence", " ".join(r["reason"] for r in rows))

    def test_coder_cannot_review(self):
        review = self.round("t_1", ["t_2", "t_3"], "t_4", SHA, ["pass", "pass"])["review"]
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "default"}):
            with self.assertRaisesRegex(wf.WorkflowError, "cannot review it"):
                wf.verdict(review, "approve", "", self.fake)

    def test_gate_needs_rounds(self):
        run = lambda argv: done(0, json.dumps({"task": {"id": "t_x", "status": "todo"}, "parents": []}))  # noqa: E731
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wf.main(["gate", "t_x"], run), 1)


class StateDirTest(unittest.TestCase):
    def test_resolves_the_shared_kanban_root(self):
        cases = [
            ({"HERMES_KANBAN_DB": "/h/.hermes/kanban.db"}, "/h/.hermes/kanban/workflow-runs"),
            ({"HERMES_HOME": "/h/.hermes/profiles/mac-worker"}, "/h/.hermes/kanban/workflow-runs"),
            ({"HERMES_HOME": "/h/.hermes"}, "/h/.hermes/kanban/workflow-runs"),
            ({"HERMES_WORKFLOW_STATE": "/s"}, "/s"),
        ]
        for env, expected in cases:
            clean = {k: v for k, v in os.environ.items()
                     if k not in ("HERMES_WORKFLOW_STATE", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_HOME")}
            with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
                self.assertEqual(str(wf.state_dir()), expected, env)


@unittest.skipUnless(shutil.which("hermes"), "hermes not installed")
class RealBoardTest(StateDirMixin, unittest.TestCase):
    """The real CLI on a temp board: graph shape, links, subscriptions, the loop and the gate."""

    def setUp(self):
        super().setUp()
        self.peer_fake = FakeHermes()
        self.home = tempfile.mkdtemp(prefix="kanban-workflow-")
        self.env = {**os.environ, "HERMES_HOME": self.home}
        for k in [*DISCORD_ENV, "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_PROFILE"]:
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

    def complete(self, tid):
        self.assertEqual(self.hermes(["kanban", "claim", tid]).returncode, 0, tid)
        r = self.hermes(["kanban", "complete", tid, "--summary", "ok"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def validate(self, tid, sha, verdict):
        wf.dispatch(tid, self.run_cli)
        run_id = wf.peer_runs(self.show(tid))[-1]["run_id"]
        self.peer_fake.remote_by_run[run_id] = {"status": "completed",
                                                "output": evidence(verdict, task=tid, sha=sha)}
        self.assertEqual(wf.wait(tid, 0, 30, self.run_cli)[0], 0)
        self.complete(tid)

    def test_full_loop(self):
        args = intake_args("--lane", "mac", "--lane", "windows", "--key", "it",
                           "--discord-chat-id", "111", "--discord-thread-id", "222", "--discord-chat-type", "thread")
        plan = wf.intake(args, self.run_cli)
        req, code1, rnd1 = plan["request"]["id"], plan["code"]["id"], plan["round"]["id"]
        mac1, win1 = [lane["id"] for lane in plan["lanes"]]
        self.assertEqual(wf.intake(args, self.run_cli)["request"]["id"], req)  # idempotent

        self.assertEqual(self.show(code1)["task"]["status"], "ready")
        self.assertEqual(self.show(mac1)["task"]["status"], "todo")
        self.assertEqual(self.show(req)["parents"], [rnd1])
        subs = json.loads(self.hermes(["kanban", "notify-list", req, "--json"]).stdout)
        self.assertEqual([(s["chat_id"], s["thread_id"], s["delivery_mode"]) for s in subs],
                         [("111", "222", "notify+wake")])

        # round 1: the coordinator pushes SHA, Windows fails -> fix round opened automatically
        wf.put_record(self.run_cli, code1, "code-sha", {"rid": f"code-sha:{SHA}", "sha": SHA, "notes": "n"})
        self.complete(code1)
        self.assertEqual(self.show(mac1)["task"]["status"], "ready")
        self.validate(mac1, SHA, "pass")
        self.validate(win1, SHA, "fail")
        code, out = wf.advance(rnd1, self.run_cli)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        nxt = out["metadata"]["next"]
        self.assertEqual(sorted(self.show(req)["parents"]), sorted([rnd1, nxt["round_card"]]))
        self.complete(rnd1)
        self.assertEqual(self.show(req)["task"]["status"], "todo")  # waits for round 2
        self.assertEqual(self.show(nxt["code"])["task"]["status"], "ready")
        self.assertIn("LoginTests.testRotation", self.show(nxt["code"])["task"]["body"])
        new_subs = json.loads(self.hermes(["kanban", "notify-list", nxt["code"], "--json"]).stdout)
        self.assertEqual([s["delivery_mode"] for s in new_subs], ["notify"])

        # round 2: new SHA, both pass -> review -> approve -> gate
        wf.put_record(self.run_cli, nxt["code"], "code-sha", {"rid": f"code-sha:{SHA2}", "sha": SHA2, "notes": "fix"})
        self.complete(nxt["code"])
        for tid in nxt["lanes"].values():
            self.validate(tid, SHA2, "pass")
        code, out = wf.advance(nxt["round_card"], self.run_cli)
        self.assertEqual((code, out["state"]), (0, "VALIDATED"))
        review = out["metadata"]["next"]["review"]
        self.complete(nxt["round_card"])
        self.assertEqual(self.show(req)["task"]["status"], "todo")  # waits for the review
        self.assertEqual(wf.verdict(review, "approve", "", self.run_cli)[0], 0)
        self.assertFalse(wf.gate(req, self.run_cli)[0])
        self.complete(review)  # the reviewer profile is the assignee of the completing run
        self.assertEqual(self.show(req)["task"]["status"], "ready")
        ok, rows = wf.gate(req, self.run_cli)
        self.assertTrue(ok, rows)
        states = {r["id"]: r["state"] for r in wf.status(req, self.run_cli)}
        self.assertEqual((states[win1], states[rnd1], states[review]), ("WIN_FAILED", "FIX_REQUIRED", "REVIEW_APPROVED"))

    def test_ledger_survives_a_refused_card_write(self):
        plan = wf.intake(intake_args("--lane", "mac", "--key", "it2"), self.run_cli)
        code1, mac1 = plan["code"]["id"], plan["lanes"][0]["id"]
        wf.put_record(self.run_cli, code1, "code-sha", {"rid": f"code-sha:{SHA}", "sha": SHA})
        self.complete(code1)
        child_env = {**self.env, "HERMES_DELEGATED_CHILD_CONTEXT": "1"}
        refused = lambda argv: self.peer_fake(argv) if argv[0] == "-p" else subprocess.run(  # noqa: E731
            ["hermes", *argv], capture_output=True, text=True, env=child_env, timeout=120)
        rec = wf.dispatch(mac1, refused)  # the script itself runs outside the child; the board refuses
        self.assertFalse(rec["recorded_on_card"])
        self.assertEqual(wf.card_records(self.show(mac1)), [])
        self.assertTrue(wf.dispatch(mac1, self.run_cli)["resumed"])  # recovered from the ledger, mirrored
        self.assertEqual(wf.card_records(self.show(mac1))[0]["run_id"], rec["run_id"])
        self.assertEqual(len(self.peer_fake.runs), 1)


class FakePeerGateway(BaseHTTPRequestHandler):
    """The api_server endpoints `hermes peer run/status` call, with Bearer auth.
    A run answers with evidence for the card and commit named in its input."""
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
            text = self.runs[run_id]["input"]
            task = text.split("Kanban card ", 1)[1].split()[0]
            sha = re.search(r"Commit: ([0-9a-f]{40})", text).group(1)
            return self.reply(200, {"run_id": run_id, "status": "completed",
                                    "output": evidence(task=task, sha=sha, fenced=True)})
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
class RealPeerTest(StateDirMixin, unittest.TestCase):
    """The real `hermes peer` CLI, profile-scoped peer key, and a temp board."""

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="kanban-peer-")
        self.env = {k: v for k, v in os.environ.items() if k not in DISCORD_ENV and not k.startswith("HERMES_PEER_")
                    and k not in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_PROFILE")}
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

        plan = wf.intake(args, self.hermes)
        code_id, lane = plan["code"]["id"], plan["lanes"][0]["id"]
        wf.put_record(self.hermes, code_id, "code-sha", {"rid": f"code-sha:{SHA}", "sha": SHA, "notes": "n"})
        rec = wf.dispatch(lane, self.hermes)
        self.assertEqual((rec["peer"], rec["attempt"], rec["idempotency_key"]), ("mac", 1, f"kanban:{lane}:1"))
        self.assertTrue(wf.dispatch(lane, self.hermes)["resumed"])
        (sent,) = FakePeerGateway.runs.values()
        for text in (f"Kanban card {lane}", f"Commit: {SHA}", "Fix the thing", "you never change source code"):
            self.assertIn(text, sent["input"])
        self.assertNotIn("Relay steps", sent["input"])

        code, out = wf.wait(lane, 0, 30, self.hermes)
        self.assertEqual((code, out["metadata"]["run_id"], out["metadata"]["commit"]), (0, rec["run_id"], SHA))
        self.assertTrue(all(ok for _, _, ok in FakePeerGateway.seen), FakePeerGateway.seen)
        board = self.hermes(["kanban", "show", lane, "--json"]).stdout
        self.assertIn(rec["run_id"], board)
        self.assertIn("evidence ", board)
        self.assertNotIn(PEER_KEY, board + json.dumps(out) + wf.ledger_path(lane).read_text())


if __name__ == "__main__":
    unittest.main()
