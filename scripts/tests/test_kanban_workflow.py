"""Tests for the kanban-workflow skill (hermes/skills/kanban-workflow).

Central writer, evidence-only peers: the coordinator's code card pushes an exact
SHA, the Mac/Windows relays have their peers test that SHA and return evidence,
round cards open the next fix round or the review, and the report card gates on
the final SHA and an independent approval. `--mobile ios` stages it: Mac iPhone +
iPad simulators, then BrowserStack real devices (disabled and fail-closed until the
operator's activation file exists; a pass needs a build re-read from the API, here a
fake API); Windows deferred.

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
import time
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


# The native tools' argument keys at upstream 71736c9f (tools/kanban_tools_schemas.py), written out
# here independently of the kit's NATIVE_SCHEMA; UpstreamCompatTest re-reads the real schemas.
UPSTREAM_SCHEMA_KEYS = {
    "kanban_create": {"assignee", "board", "body", "completion_contract", "goal_max_turns", "goal_mode",
                      "idempotency_key", "initial_status", "max_runtime_seconds", "model", "parents", "priority",
                      "project", "provider", "skills", "tenant", "title", "triage", "workspace_kind",
                      "workspace_path"},
    "kanban_comment": {"board", "body", "task_id"},
    "kanban_link": {"board", "child_id", "parent_id"},
}
BOARD_WRITE_VERBS = {"create", "comment", "link", "unlink", "complete", "block", "unblock", "claim", "assign",
                     "reassign", "archive", "edit", "notify-subscribe", "notify-unsubscribe", "heartbeat",
                     "attach", "promote", "dispatch", "request-review", "request-changes"}
PARENT_CMDLINE = wf.rl.proc_cmdline(os.getppid())  # the stand-in bound managed worker: this test's parent


class FakeHermes:
    """Records argv. Answers `config get` from a backend map, `peer list` from a
    per-profile listing, `peer run/status` from a scripted remote (per run id when
    set), and `kanban show` from a tiny board. The board is written only through
    `native()`, which behaves like the runtime's kanban_create / kanban_comment /
    kanban_link handlers (exact schema keys, author / created_by = the calling
    profile, idempotency key returns the existing card). A CLI board write fails."""

    current = None

    def __init__(self, backends=None, peers=None):
        self.calls = []
        self.backends = backends if backends is not None else {
            "mac-worker": "local", "windows-worker": "local", "reviewer": "local", "default": "local"}
        self.peers = peers if peers is not None else {"mac-worker": PEER_LIST, "windows-worker": PEER_LIST}
        self.cards, self.shows = {}, {}
        self.remote = {"status": "running"}
        self.remote_by_run = {}
        self.runs = []
        self.native_calls, self.cli_writes = [], []
        self.refuse_native = False  # what a delegate_task child gets from the native tools
        self.fail_peer_run = False
        self.run_seq = 0
        FakeHermes.current = self

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
        if argv[:2] == ["kanban", "show"]:
            if argv[2] not in self.shows:
                return done(1, err=f"no such task: {argv[2]}")
            show = json.loads(json.dumps(self.shows[argv[2]]))
            show["children"] = [tid for tid, s in self.shows.items() if argv[2] in s["parents"]]
            return done(0, json.dumps(show))
        if argv[:1] == ["kanban"] and argv[1] in BOARD_WRITE_VERBS:
            self.cli_writes.append(argv)
            raise AssertionError(f"CLI board write {argv[:3]}: the workflow writes only through native ops")
        raise AssertionError(f"unexpected call {argv}")

    def native(self, tool, args, profile):
        """The runtime's handler for one native op, as `profile` (the runtime identity)."""
        self.native_calls.append((tool, json.loads(json.dumps(args)), profile))
        unknown = sorted(set(args) - UPSTREAM_SCHEMA_KEYS[tool])
        if unknown:
            return {"error": f"{tool}: unknown parameter(s): {', '.join(unknown)}. Nothing changed."}
        if self.refuse_native:
            return {"error": f"{tool} refused: delegate_task child agents are not Kanban run owners."}
        if tool == "kanban_create":
            key = args.get("idempotency_key")
            if key and key in self.cards:
                return {"ok": True, "task_id": self.cards[key]["id"]}
            tid = f"t_{len(self.shows) + 1}"
            parents = list(args.get("parents") or [])
            self.shows[tid] = {
                "task": {"id": tid, "title": args["title"], "assignee": args["assignee"], "body": args.get("body"),
                         "status": "todo" if any(self.shows[p]["task"]["status"] != "done" for p in parents)
                         else "ready", "created_by": profile,
                         "max_runtime_seconds": args.get("max_runtime_seconds")},
                "parents": parents, "comments": [], "events": [], "runs": []}
            if key:
                self.cards[key] = {"id": tid, "title": args["title"]}
            return {"ok": True, "task_id": tid}
        if tool == "kanban_comment":
            show = self.shows[args["task_id"]]
            show["comments"].append({"author": profile, "body": args["body"], "created_at": int(time.time())})
            show["events"].append({"kind": "commented", "payload": {}})
            return {"ok": True, "task_id": args["task_id"]}
        parents = self.shows[args["child_id"]]["parents"]
        if args["parent_id"] not in parents:
            parents.append(args["parent_id"])
        return {"ok": True, "parent_id": args["parent_id"], "child_id": args["child_id"]}

    def of(self, tool):
        """The native calls of one tool (`create`, `comment`, `link`), as (args, profile)."""
        return [(args, profile) for t, args, profile in self.native_calls if t == f"kanban_{tool}"]

    def peer_calls(self, verb):
        return [c for c in self.calls if c[:1] == ["-p"] and c[2:4] == ["peer", verb]]

    def body(self, task_id):
        return self.shows[task_id]["task"]["body"]

    def claim(self, task_id, profile=None, pid=None):
        """What the dispatcher and the managed worker leave on the board: the card running, one
        running run, and that run's worker_bound event (pid + `<epoch>|<start ticks>`). The
        stand-in bound interpreter is this test's parent process, a live ancestor."""
        show = self.shows[task_id]
        self.run_seq += 1
        pid = pid or os.getppid()
        show["task"]["status"] = "running"
        show["runs"].append({"id": self.run_seq, "profile": profile or show["task"]["assignee"],
                             "status": "running", "outcome": None, "ended_at": None})
        show["events"].append({"kind": "worker_bound", "run_id": self.run_seq, "payload": {
            "pid": pid, "claim_lock": f"lock-{self.run_seq}",
            "started_at": f"{wf.rl.instantiation_epoch()}|{wf.rl.proc_start_ticks(pid)}"}})
        return self.run_seq

    def finish(self, task_id, profile=None):
        """What kanban_complete does: the card is done, its run completed."""
        show = self.shows[task_id]
        t = show["task"]
        t["status"] = "done"
        live = [r for r in show["runs"] if r.get("status") == "running"]
        if live:
            live[-1].update({"status": "done", "outcome": "completed", "ended_at": 1,
                             "profile": profile or live[-1]["profile"]})
        else:
            show["runs"].append({"profile": profile or t["assignee"], "outcome": "completed", "status": "done"})
        show["events"].append({"kind": "completed", "payload": {}})

    def by_title(self, prefix):
        return [tid for tid, s in self.shows.items() if s["task"]["title"].startswith(prefix)]


def apply_ops(fake, ops, profile):
    """Apply an exit-5 envelope's ops in order, as the agent's native tools would. Returns the
    task id of a root create (the --bind value) when there was one."""
    root = None
    for op in ops:
        assert set(op) == {"op_id", "tool", "args", "why"}, op
        assert set(op["args"]) <= UPSTREAM_SCHEMA_KEYS[op["tool"]], op
        res = fake.native(op["tool"], op["args"], profile)
        if op["tool"] == "kanban_create" and not op["args"].get("parents"):
            root = res.get("task_id")
    return root


def task_of(fake, a, kw):
    for value in [*a, *kw.values()]:
        value = getattr(value, "task_id", value)
        if isinstance(value, str) and value in fake.shows:
            return value
    return None


def bound_adaptor(on=True):
    """The review card's bound managed worker is this test's parent process (FakeHermes.claim);
    only its command line stands in for `python -m agent.managed_worker` (see CheckoutRepo)."""
    if not on:
        return contextlib.nullcontext()
    return mock.patch.object(wf.rl, "is_managed_worker", lambda cmd: cmd == PARENT_CMDLINE)


def converge(fn, *a, profile=None, **kw):
    """Run a workflow command the way the agent does: apply the native ops it queues (as the
    card's assignee, or `profile`), rerun on a pending create (with the root bound), read
    finished commands back with `sync`, until nothing is left (at most 8 rounds)."""
    fake = next((v for v in [*a, *kw.values()] if isinstance(v, FakeHermes)), None) or FakeHermes.current
    for _ in range(8):
        task = task_of(fake, a, kw)
        who = profile or (fake.shows[task]["task"]["assignee"] if task else "default")
        if fn is wf.verdict and task and fake.shows[task]["task"]["status"] != "running":
            fake.claim(task, who)  # the dispatcher claimed the review card for its reviewer
        with wf.transport() as box, bound_adaptor(fn is wf.verdict):
            try:
                result, pending = fn(*a, **kw), False
            except wf.Pending:
                result, pending = None, True
            ops = list(box.ops)
        if not ops:
            assert not pending
            return result
        root = apply_ops(fake, ops, who)
        if pending:
            if root and fn is wf.intake:
                a[0].bind = root
            continue
        for _ in range(4):
            with wf.transport() as box:
                wf.sync(task, fake)
                ops = list(box.ops)
            if not ops:
                return result
            apply_ops(fake, ops, who)
        raise AssertionError(f"{fn.__name__}: ops did not read back")
    raise AssertionError(f"{fn.__name__}: did not converge in 8 rounds")


def converge_main(argv, run=None, *rest, profile=None, **kw):
    """`workflow.py ...` until it exits other than 5, applying each envelope's ops natively."""
    fake = run if isinstance(run, FakeHermes) else FakeHermes.current
    argv = list(argv)
    for _ in range(10):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = wf.main(argv, *([run] if run is not None else []), *rest, **kw)
        if code != wf.EXIT_PENDING:
            sys.stdout.write(buf.getvalue())
            return code
        env = json.loads(buf.getvalue())
        assert env["converged"] is False and env["rerun"][:2] == [sys.executable, str(wf.SCRIPT)], env
        task = task_of(fake, argv, {})
        who = profile or (fake.shows[task]["task"]["assignee"] if task else "default")
        root = apply_ops(fake, env["ops"], who)
        argv = env["rerun"][2:] + (["--bind", root] if env["bind_root"] else [])
    raise AssertionError("main did not converge")


class _Native:
    """`wf` with every board-writing command driven to convergence (see `converge`)."""
    COMMANDS = {"intake", "record_sha", "dispatch", "wait", "advance", "verdict", "checkout_command", "put_record",
                "completion_command", "completion_write", "completion_handoff", "completion_final", "sync"}

    def __getattr__(self, name):
        if name == "main":
            return converge_main
        fn = getattr(wf, name)
        if name in self.COMMANDS:
            return lambda *a, **kw: converge(fn, *a, **kw)
        return fn


nw = _Native()


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
              "dirty_tree": False, "blocker": "", "summary": "built and tested",
              "repo_dir": f"/Users/norkz/Documents/.hermes-validate/{task}", **extra}
    if fenced:
        return f"Done.\n\n```json\n{json.dumps(fields)}\n```\nAnything else?\n"
    return f"Validation done.\nBEGIN-EVIDENCE\n{json.dumps(fields)}\nEND-EVIDENCE\n"


class StateDirMixin:
    """A temp run ledger per test, never the live ~/.hermes."""

    def setUp(self):
        super().setUp()
        self._state = tempfile.mkdtemp(prefix="kanban-workflow-state-")
        patcher = mock.patch.dict(os.environ, {"HERMES_WORKFLOW_STATE": self._state,
                                               "KANBAN_BROWSERSTACK_CONFIG": os.path.join(self._state, "bs.env")},
                                  clear=False)
        patcher.start()
        os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._state, True)


def graph(fake, *extra, key="k", body="full ask"):
    """Round 1 on a fake board: t_1 code, t_2 mac, t_3 win, t_4 round, t_5 report."""
    return nw.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", key, "--body", body, *extra), fake)


def push(fake, code_id="t_1", sha=SHA, notes="changed the login screen"):
    """What record-sha leaves behind, without git."""
    h = wf.header_of(fake.shows[code_id])
    nw.put_record(fake, code_id, "code-sha", {"rid": f"code-sha:{sha}", "sha": sha, "branch": h["branch"],
                                              "repo": h["repo"], "round": int(h["round"]), "notes": notes})
    fake.finish(code_id)


def validate(fake, tid, output):
    """Relay: dispatch, wait until evidence, kanban_complete on exit 0."""
    nw.dispatch(tid, fake)
    run_id = wf.peer_runs(card(fake, tid))[-1]["run_id"]
    fake.remote_by_run[run_id] = {"status": "completed", "output": output.replace("t_2", tid)}
    code, out = nw.wait(tid, 0, 30, fake, sleep=lambda s: None)
    if code == 0:
        fake.finish(tid)
    return code, out


def card(fake, tid):
    return wf.card(fake, tid)


class IntakeTest(StateDirMixin, unittest.TestCase):
    def test_builds_round_one_and_the_report_with_native_creates_only(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = nw.intake(intake_args("--lane", "mac", "--lane", "windows", "--body", "full ask"), fake)

        creates = [args for args, _ in fake.of("create")]
        self.assertEqual([(c["title"], c["assignee"]) for c in creates], [
            ("[code 1] Build the iPad app", "default"), ("[mac 1] Build the iPad app", "mac-worker"),
            ("[win 1] Build the iPad app", "windows-worker"), ("[round 1] Build the iPad app", "default"),
            ("Report: Build the iPad app", "default")])
        self.assertEqual([c.get("parents", []) for c in creates], [[], ["t_1"], ["t_1"], ["t_2", "t_3"], ["t_4"]])
        self.assertEqual(plan["key"], "discord:111:333")
        self.assertEqual(creates[-1]["idempotency_key"], "discord:111:333:request")
        self.assertEqual(creates[1]["idempotency_key"], "discord:111:333:mac-worker:1")
        self.assertEqual((plan["branch"], plan["request"]["id"], plan["round"]["id"]),
                         ("feat/build-the-ipad-app", "t_5", "t_4"))
        self.assertEqual((creates[0]["max_runtime_seconds"], creates[1]["max_runtime_seconds"]), (6 * 3600, 2 * 3600))
        self.assertNotIn("max_runtime_seconds", creates[3])
        self.assertEqual({p for _, p in fake.of("create")}, {"default"})  # created_by is the runtime profile
        self.assertEqual({fake.shows[t]["task"]["created_by"] for t in fake.shows}, {"default"})
        # no notify-subscribe: native kanban_create subscribes the creating Discord session itself
        self.assertEqual(fake.cli_writes, [])
        self.assertIn("native auto-subscribe", plan["notify"])
        self.assertTrue(all(wf.native_v1(fake.shows[t]) for t in fake.shows))

    def test_code_card_makes_the_coordinator_the_only_writer(self):
        fake = FakeHermes()
        graph(fake)
        body = fake.body("t_1")
        h = wf.card_header(body)
        self.assertEqual((h["workflow"], h["round"], h["repo"], h["branch"], h["base"], h["coder"]),
                         ("code", "1", REPO_URL, "feat/build-the-ipad-app", "main", "default"))
        self.assertEqual(h["lanes"], "mac-worker=mac, windows-worker=windows")
        for text in ("only code writer", "delegate_task", "Claude Opus", "#fable", "no Co-Authored-By",
                     f"{wf.SCRIPT} record-sha {wf.TASK_TOKEN}", "never inside delegate_task",
                     "Never force push", "Do not hand coding to the Mac or Windows workers"):
            self.assertIn(text, body)
        self.assertTrue(body.rstrip().endswith(f"{wf.REQUEST_MARKER}\nfull ask"))

    def test_code_card_merges_only_with_named_feature_to_feature_approval(self):
        fake = FakeHermes()
        graph(fake)
        body = fake.body("t_1")
        self.assertNotIn("never merge.", body)
        self.assertNotIn("- Never merge, deploy", body)
        for text in ("Never merge unless the merge gates below allow it",
                     "Merge only one feature branch into another, and only with the operator's explicit approval\n"
                     "  naming that source and that destination",
                     "Without\n  that approval, do not merge.",
                     "leave conflicts for the operator and never force push",
                     "Merges into main or another protected branch (develop, main, master, production, release)",
                     "deploys and releases each need their own separate explicit approval",
                     "a feature-to-feature\n  approval never covers them",
                     "never change production,\n  without the operator's explicit approval",
                     "kanban_block(reason=\"human-approval: <what you hit>\")",
                     "Never run destructive commands", "Never send Discord messages yourself",
                     "Never commit to main or main"):
            self.assertIn(text, body)
        for card in ("t_2", "t_3", "t_5"):  # validators and the report keep the blanket no-merge gate
            self.assertIn("- Never merge, deploy, release, publish, sign", fake.body(card))
            self.assertNotIn("Merge only one feature branch", fake.body(card))

    def test_validation_cards_are_test_only_relays(self):
        fake = FakeHermes()
        graph(fake)
        body = fake.body("t_2")
        h = wf.card_header(body)
        self.assertEqual((h["workflow"], h["transport"], h["peer"], h["lane"], h["code"]),
                         ("validate", "hermes-peer", "mac", "mac-worker", "t_1"))
        for text in (f"{wf.SCRIPT} dispatch {wf.TASK_TOKEN}", f"{wf.SCRIPT} wait {wf.TASK_TOKEN}",
                     "Never call kanban_request_review", "never inside delegate_task",
                     "never switch to another transport (SSH included)", "A failing build or test is a valid"):
            self.assertIn(text, body)
        self.assertEqual(wf.card_header(fake.body("t_4"))["workflow"], "round")
        self.assertIn(f"{wf.SCRIPT} advance {wf.TASK_TOKEN}", fake.body("t_4"))
        self.assertIn(f"{wf.SCRIPT} gate {wf.TASK_TOKEN}", fake.body("t_5"))
        for tid in ("t_1", "t_2", "t_4", "t_5"):  # no shell variable stands for the card id any more
            self.assertNotIn("$HERMES_KANBAN_TASK", fake.body(tid))
            self.assertIn("never the `hermes kanban` CLI", fake.body(tid))

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
        nw.intake(intake_args("--lane", "mac", "--key", "k", "--body-file", fh.name), fake)
        self.assertIn("2. Photo cropping", wf.request_of(fake.shows["t_1"]))

    def test_explicit_flags_win_over_session_env(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, DISCORD_ENV, clear=False):
            plan = nw.intake(intake_args("--lane", "mac", "--discord-chat-id", "999",
                                         "--discord-message-id", "888"), fake)
        self.assertEqual(plan["key"], "discord:999:888")

    def test_non_discord_session_is_not_an_origin(self):
        fake = FakeHermes()
        with mock.patch.dict(os.environ, {**DISCORD_ENV, "HERMES_SESSION_PLATFORM": "telegram"}, clear=False):
            plan = nw.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        self.assertIn("none", plan["notify"])
        self.assertEqual(fake.cli_writes, [])

    def test_requires_an_idempotency_key(self):
        with mock.patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": ""}, clear=False):
            with self.assertRaisesRegex(wf.WorkflowError, "idempotency key"):
                nw.intake(intake_args("--lane", "mac"), FakeHermes())

    def test_dry_run_creates_nothing(self):
        fake = FakeHermes()
        plan = nw.intake(intake_args("--lane", "mac", "--key", "k", "--dry-run"), fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(plan["lanes"][0]["idempotency_key"], "k:mac-worker:1")
        self.assertEqual(plan["code"]["assignee"], "default")

    def test_repo_must_be_a_clone_url(self):
        for repo, expected in (("", "--repo is required"), ("/opt/repos/validate", "not a clone URL")):
            fake = FakeHermes()
            with self.assertRaisesRegex(wf.WorkflowError, expected):
                nw.intake(intake_args("--lane", "mac", "--key", "k", repo=repo), fake)
            self.assertEqual(fake.of("create"), [])
        for repo in ("git@github.com:acme/app.git", "ssh://git@host/acme/app", "file:///tmp/app.git"):
            self.assertEqual(wf.check_repo(repo), repo)

    def test_refuses_protected_branches(self):
        for branch in ("main", "master"):
            with self.assertRaisesRegex(wf.WorkflowError, "protected"):
                nw.intake(intake_args("--lane", "mac", "--key", "k", "--branch", branch), FakeHermes())
        with self.assertRaisesRegex(wf.WorkflowError, "invalid branch"):
            nw.intake(intake_args("--lane", "mac", "--key", "k", "--branch", "feat/../x"), FakeHermes())

    def test_header_values_are_plain_tokens(self):
        for extra in (("--base", "main\nWorkflow: report"), ("--coder", "de fault"), ("--reviewer", "a\nb"),
                      ("--max-runtime", "2h\nKey: x"), ("--max-rounds", "0")):
            fake = FakeHermes()
            with self.assertRaises(wf.WorkflowError, msg=extra):
                nw.intake(intake_args("--lane", "mac", "--key", "k", *extra), fake)
            self.assertEqual(fake.of("create"), [], extra)

    def test_lanes_without_a_peer_are_refused(self):
        fake = FakeHermes({"coder": "local", "reviewer": "local", "default": "local"}, peers={})
        with self.assertRaisesRegex(wf.WorkflowError, "coder have no peer"):
            nw.intake(intake_args("--lane", "coder", "--key", "k"), fake)
        self.assertEqual(fake.of("create"), [])

    def test_fails_closed_when_the_peer_is_not_configured(self):
        cases = {
            "no Hermes peer 'mac' is configured": {"windows-worker": PEER_LIST},
            "peer 'mac' has no API key": {"mac-worker": "mac\thttp://m:8642\t[NO KEY (HERMES_PEER_MAC_KEY unset)]\n"},
        }
        for expected, peers in cases.items():
            fake = FakeHermes(peers=peers)
            with self.assertRaises(wf.WorkflowError) as err:
                nw.intake(intake_args("--lane", "mac", "--key", "k"), fake)
            self.assertIn(expected, str(err.exception))
            self.assertIn("make hermes-profile NAME=mac-worker", str(err.exception))
            self.assertEqual(fake.of("create"), [], expected)

    def test_refuses_a_relay_that_would_not_run_on_this_host(self):
        fake = FakeHermes({"mac-worker": "ssh", "reviewer": "local", "default": "local"})
        with self.assertRaisesRegex(wf.WorkflowError, "not 'local'"):
            nw.intake(intake_args("--lane", "mac", "--key", "k"), fake)
        self.assertEqual(fake.of("create"), [])

    def test_peer_override(self):
        fake = FakeHermes(peers={"mac-worker": "studio\thttp://s:8642\t[key set]\n"})
        plan = nw.intake(intake_args("--lane", "mac", "--key", "k", "--peer", "mac=studio/qa"), fake)
        self.assertEqual(plan["lanes"][0]["peer"], "studio/qa")
        self.assertIn("Peer: studio/qa\n", fake.body("t_2"))
        for bad in ("mac", "linux=box", "mac=Bad Name"):
            with self.assertRaises(wf.WorkflowError, msg=bad):
                nw.intake(intake_args("--lane", "mac", "--key", "k2", "--peer", bad), fake)

    def test_refuses_missing_profiles_and_dependent_review(self):
        with self.assertRaisesRegex(wf.WorkflowError, "'reviewer' does not exist"):
            nw.intake(intake_args("--lane", "mac", "--key", "k"),
                      FakeHermes({"mac-worker": "local", "default": "local"}))
        for extra in (("--reviewer", "mac-worker"), ("--reviewer", "default"), ("--coder", "reviewer")):
            with self.assertRaisesRegex(wf.WorkflowError, "review must be independent"):
                nw.intake(intake_args("--lane", "mac", "--key", "k", *extra), FakeHermes())


class GitCard(StateDirMixin):
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
        nw.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "k", repo=f"file://{self.remote}"),
                  self.fake)



class RecordShaTest(GitCard, unittest.TestCase):
    def record(self):
        return nw.record_sha("t_1", self.work, "notes: check rotation; token=abcdef123456", self.fake)

    def test_requires_a_push_a_clean_tree_and_the_card_branch(self):
        with self.assertRaisesRegex(wf.WorkflowError, "push first"):
            self.record()
        self.git("push", "origin", "feat/build-the-ipad-app", cwd=self.work)
        Path(self.work, "a.txt").write_text("dirty\n")
        with self.assertRaisesRegex(wf.WorkflowError, "uncommitted or untracked paths"):
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

    def test_a_delegate_child_cannot_land_the_record(self):
        """Every worker shell is fenced now, so the script cannot tell a delegate child from the
        worker; the native kanban_comment refuses the child. The SHA stays in the ledger only and
        is never on the card, so the validators do not start from it."""
        self.git("push", "origin", "feat/build-the-ipad-app", cwd=self.work)
        self.fake.refuse_native = True
        with wf.transport() as box:
            out = wf.record_sha("t_1", self.work, "n", self.fake)
            ops = list(box.ops)
        self.assertEqual(([o["tool"] for o in ops], out["recorded_on_card"]), (["kanban_comment"], False))
        self.assertEqual(apply_ops(self.fake, ops, "default"), None)
        self.assertEqual(wf.card_records(self.fake.shows["t_1"]), [])
        self.assertTrue(wf.read_ledger("t_1"))

    def test_only_code_cards(self):
        with self.assertRaisesRegex(wf.WorkflowError, "not a code card"):
            nw.record_sha("t_2", self.work, "", self.fake)


PASS_SCAN = "PASS: Inspected 1 process directories.\nPASS: No repository CWD matches found.\n"


def passing_scan(argv):
    return done(0, PASS_SCAN)


class CheckoutRepo(StateDirMixin):
    """A bare remote, the operator's clone of it (the designated checkout), a fake board and,
    in the temp state dir, a registered scanner (`/bin/true` stands in for the root helper)."""

    def setUp(self):
        super().setUp()
        self.tmp = str(Path(tempfile.mkdtemp(prefix="kanban-workflow-co-")).resolve())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.url = f"file://{self.remote}"
        self.co = os.path.join(self.tmp, "validate")
        git = lambda *a, cwd=self.tmp: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
        self.git = git
        git("init", "--bare", "-b", "main", self.remote)
        git("clone", self.url, self.co)  # origin is the clone URL the operator passes as --repo
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=self.co)
        Path(self.co, "a.txt").write_text("1\n")
        git("add", "a.txt", cwd=self.co)
        git("commit", "-m", "chore: init", cwd=self.co)
        git("push", "origin", "main", cwd=self.co)
        self.fake = FakeHermes()
        real = wf.rl.helper_repository  # `/bin/true` is bound to this checkout (see helper_repository)
        binding = mock.patch.object(wf.rl, "helper_repository", lambda h: self.co if h == "/bin/true" else real(h))
        binding.start()
        self.addCleanup(binding.stop)
        wf.rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        # Lease adaptor: the code card's bound managed worker is this test's parent process (a
        # live ancestor, recorded by FakeHermes.claim as the run's worker_bound event). Only its
        # command line stands in for `python -m agent.managed_worker` (genuine path, a real child
        # process: test_repo_lease's BoundWorkerTest).
        adaptor = mock.patch.object(wf.rl, "is_managed_worker", lambda cmd: cmd == PARENT_CMDLINE)
        adaptor.start()
        self.addCleanup(adaptor.stop)

    def intake(self, *extra, repo=None, key="k"):
        plan = nw.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", key, "--body", "full ask",
                                     *extra, repo=repo or self.url), self.fake)
        if not plan.get("dry_run") and self.fake.shows["t_1"]["task"]["status"] != "running":
            self.fake.claim("t_1")  # the coordinator's run of the code card
        return plan


class CheckoutTest(CheckoutRepo, unittest.TestCase):
    """`--checkout`: the code cards work in the operator's designated existing checkout
    instead of a fresh clone; the default stays a fresh clone per card."""

    def test_default_is_a_fresh_clone_per_card(self):
        plan = self.intake()
        body = self.fake.body("t_1")
        self.assertNotIn("checkout", plan)
        self.assertNotIn("checkout", wf.card_header(body))
        for text in (f"1. Work in a fresh checkout on this host: ~/hermes-work/{wf.TASK_TOKEN}.",
                     f"git clone {self.url} there", "never reuse another card's checkout",
                     f"--repo-dir ~/hermes-work/{wf.TASK_TOKEN} --notes"):
            self.assertIn(text, body)
        self.assertNotIn("designated", body)

    def test_code_card_works_in_the_designated_checkout(self):
        plan = self.intake("--checkout", self.co)
        self.assertEqual(plan["checkout"], self.co)
        body = self.fake.body("t_1")
        self.assertEqual(wf.card_header(body)["checkout"], self.co)
        self.assertNotIn("git clone", body)
        self.assertNotIn("hermes-work", body)
        co = self.co
        for text in (f"Work only in the operator's designated existing checkout on this host: {co}.",
                     "Do not clone.", "never run git reset, clean, stash,\n   restore",
                     f"python3 {wf.SCRIPT} preflight {wf.TASK_TOKEN} --repo-dir {co}",
                     f"python3 {wf.SCRIPT} checkpoint {wf.TASK_TOKEN} --repo-dir {co}",
                     f"python3 {wf.SCRIPT} release {wf.TASK_TOKEN} --repo-dir {co}",
                     f"origin is {self.url}; no index.lock", "classified by\n   process ancestry",
                     "known\n   editors, language servers, dev servers, idle shells and read-only git are not writers",
                     "any other program or script", "never adopted\n   this way",
                     f"git -C {co} switch -c feat/build-the-ipad-app origin/main",
                     f"git -C {co} merge --ff-only origin/feat/build-the-ipad-app",
                     "Never remove a lease, a lock or a file to pass",
                     "exit 3 (unknown visibility", 'kanban_block(reason="checkout: <the failing\n     check and its detail verbatim>")',
                     "Unfinished work is not a blocker", "Never block for the mere\n   existence of uncommitted work",
                     "Every later round of this request works in this same checkout",
                     f"record-sha {wf.TASK_TOKEN} --repo-dir {co} --notes",
                     "Never commit to main or main", "Never force push"):
            self.assertIn(text, body)
        for gone in ("find /proc", "-lname", "status --porcelain prints nothing"):
            self.assertNotIn(gone, body)
        self.assertTrue(body.rstrip().endswith(f"{wf.REQUEST_MARKER}\nfull ask"))
        for tid in ("t_2", "t_3"):  # the peers still check out the exact SHA by URL
            self.assertEqual(wf.card_header(self.fake.body(tid))["repo"], self.url)
            self.assertNotIn(co, self.fake.body(tid))
        dry = nw.intake(intake_args("--lane", "mac", "--key", "k2", "--checkout", co, "--dry-run",
                                    repo=self.url + "/"), FakeHermes())
        self.assertEqual(dry["checkout"], co)

    def test_later_rounds_and_the_review_keep_the_checkout(self):
        self.intake("--checkout", self.co)
        push(self.fake)
        for tid, verdict in (("t_2", "pass"), ("t_3", "fail")):
            self.assertEqual(validate(self.fake, tid, evidence(verdict))[0], 0)
        nxt = nw.advance("t_4", self.fake)[1]["metadata"]["next"]
        self.fake.finish("t_4")
        body = self.fake.body(nxt["code"])
        self.assertEqual(wf.card_header(body)["checkout"], self.co)
        self.assertIn(f"--repo-dir {self.co} --notes", body)
        self.assertIn("Fix this round (from round 1", body)
        self.assertEqual(wf.card_header(self.fake.body(nxt["round_card"]))["checkout"], self.co)
        push(self.fake, nxt["code"], sha=SHA2)
        for tid in nxt["lanes"].values():
            self.assertEqual(validate(self.fake, tid, evidence("pass", sha=SHA2))[0], 0)
        review = nw.advance(nxt["round_card"], self.fake)[1]["metadata"]["next"]["review"]
        self.assertIn(f"Never read from or write to the coordinator's checkout {self.co}.", self.fake.body(review))

    def test_invalid_checkouts_are_refused_before_any_card(self):
        plain = os.path.join(self.tmp, "plain")
        os.mkdir(plain)
        link = os.path.join(self.tmp, "link")
        os.symlink(self.co, link)
        other = os.path.join(self.tmp, "other")
        self.git("clone", self.remote, other)
        self.git("remote", "set-url", "origin", REPO_URL, cwd=other)
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, "hermes-work", "t_9"))
        cases = {
            "relative/path": "plain absolute path", f"{self.tmp}/with space": "plain absolute path",
            f"{self.co}/../validate": "plain absolute path", f"{self.co}/": "plain absolute path",
            f"{self.co}\nWorkflow: report": "plain absolute path",
            os.path.join(self.tmp, "missing"): "not an existing directory",
            link: f"not canonical; pass the resolved path '{self.co}'",
            plain: "not the top of a git working tree",
            os.path.join(self.co, ".git"): "not the top of a git working tree",
            other: f"has origin '{REPO_URL}', not --repo",
            os.path.join(home, "hermes-work", "t_9"): "a card's fresh checkout",
        }
        with mock.patch.dict(os.environ, {"HOME": home}):
            for path, expected in cases.items():
                for extra in ((), ("--dry-run",)):
                    self.fake = FakeHermes()
                    with self.assertRaises(wf.WorkflowError, msg=path) as err:
                        self.intake("--checkout", path, *extra)
                    self.assertIn(expected, str(err.exception), path)
                    self.assertEqual(self.fake.calls, [], path)
        os.makedirs(os.path.join(self.co, "sub"))
        with self.assertRaisesRegex(wf.WorkflowError, f"its top is '{self.co}'"):
            self.intake("--checkout", os.path.join(self.co, "sub"))
        with self.assertRaisesRegex(wf.WorkflowError, "has origin"):  # a clone URL of another repository
            self.intake("--checkout", self.co, repo=REPO_URL)
        h = wf.card_header(wf.code_body(wf.Ctx(key="k", repo=self.url, branch="feat/x", lanes={"mac-worker": "mac"},
                                               coder="default", reviewer="reviewer", checkout="a b"), 1, "x", ""))
        with self.assertRaisesRegex(wf.WorkflowError, "invalid checkout"):
            wf.Ctx.from_header(h)

    def test_record_sha_runs_in_the_designated_checkout_only(self):
        self.intake("--checkout", self.co)
        self.git("checkout", "-b", "feat/build-the-ipad-app", cwd=self.co)
        Path(self.co, "a.txt").write_text("2\n")
        self.git("commit", "-am", "feat: change", cwd=self.co)
        self.git("push", "origin", "feat/build-the-ipad-app", cwd=self.co)
        fresh = os.path.join(self.tmp, "fresh")
        self.git("clone", "-b", "feat/build-the-ipad-app", self.remote, fresh)
        with self.assertRaisesRegex(wf.WorkflowError, f"designated checkout {self.co}.*--repo-dir {self.co}"):
            nw.record_sha("t_1", fresh, "", self.fake, scan_runner=passing_scan)
        out = nw.record_sha("t_1", self.co, "changed a.txt", self.fake, scan_runner=passing_scan)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.co, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out["metadata"]["commit"], head)
        self.assertEqual(wf.code_sha(self.fake, "t_1")["sha"], head)

    def test_cli_passes_the_checkout(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = nw.main(["intake", "--title", "Build the iPad app", "--repo", self.url, "--lane", "mac",
                            "--key", "k", "--checkout", self.co, "--dry-run"], self.fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["checkout"], self.co)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(nw.main(["intake", "--title", "t", "--repo", self.url, "--lane", "mac", "--key", "k",
                                      "--checkout", "relative", "--dry-run"], self.fake), 2)
        self.assertIn("--checkout 'relative'", err.getvalue())


class DispatchTest(StateDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        graph(self.fake, body="Fix /opt/repos/app per the Notion CSV")

    def test_needs_the_pushed_sha_first(self):
        with self.assertRaisesRegex(wf.WorkflowError, "no recorded SHA"):
            nw.dispatch("t_2", self.fake)
        self.assertEqual(self.fake.runs, [])

    def test_sends_a_test_only_task_for_the_exact_sha_and_records_the_run(self):
        push(self.fake)
        rec = nw.dispatch("t_2", self.fake)
        run = self.fake.peer_calls("run")[0]
        self.assertEqual(run[:5], ["-p", "mac-worker", "peer", "run", "mac"])
        self.assertEqual(flag(run, "--idempotency-key"), "kanban:t_2:1")
        message = run[5]
        self.assertTrue(message.startswith("You are the Mac worker's own Hermes agent. Kanban card t_2"), message[:80])
        for text in (f"Commit: {SHA}", f"Repository: {REPO_URL}",
                     f"git -C <clone> worktree add --detach /Users/norkz/Documents/.hermes-validate/t_2 {SHA}",
                     f"under {wf.MAC_REPO_ROOT}", "Never change that clone's own checkout",
                     '"repo_dir": "/Users/norkz/Documents/.hermes-validate/t_2"',
                     "you never change source code", "Forbidden: editing or adding tracked files, committing, pushing",
                     "BEGIN-EVIDENCE", "END-EVIDENCE", "~/hermes-work/t_2", "changed the login screen",
                     "are on the coordinator, not on this machine; never use them",
                     "Fix /opt/repos/app per the Notion CSV"):
            self.assertIn(text, message)
        self.assertNotIn("$HERMES_KANBAN_TASK", message)
        self.assertNotIn("Relay steps", message)
        self.assertEqual((rec["run_id"], rec["peer"], rec["attempt"], rec["sha"], rec["resumed"]),
                         ("run_t_2_1", "mac", 1, SHA, False))
        self.assertEqual(wf.peer_runs(card(self.fake, "t_2"))[-1]["run_id"], "run_t_2_1")
        args, author = self.fake.of("comment")[-1]
        self.assertEqual((args["task_id"], author, sorted(args)), ("t_2", "mac-worker", ["body", "task_id"]))
        self.assertEqual(self.fake.shows["t_2"]["comments"][-1]["author"], "mac-worker")  # runtime identity

    def test_redispatch_resumes_the_recorded_run(self):
        push(self.fake)
        nw.dispatch("t_2", self.fake)
        self.assertTrue(nw.dispatch("t_2", self.fake)["resumed"])
        self.assertEqual(len(self.fake.peer_calls("run")), 1)

    def test_a_failed_card_write_keeps_the_run_in_the_ledger(self):
        push(self.fake)
        self.fake.refuse_native = True
        with wf.transport() as box:
            rec = wf.dispatch("t_2", self.fake)
            apply_ops(self.fake, box.ops, "mac-worker")  # refused, as from a delegate child
        self.assertFalse(rec["recorded_on_card"])
        self.assertEqual([c["body"][:9] for c in self.fake.shows["t_2"]["comments"]], [])
        with wf.transport():
            again = wf.dispatch("t_2", self.fake)  # recovery: the ledger knows the run
        self.assertTrue(again["resumed"])
        self.assertEqual(len(self.fake.peer_calls("run")), 1)
        self.fake.refuse_native = False
        nw.dispatch("t_2", self.fake)  # the next command mirrors it to the card
        self.assertEqual(wf.card_records(self.fake.shows["t_2"])[-1]["run_id"], "run_t_2_1")
        nw.dispatch("t_2", self.fake)
        self.assertEqual(len([r for r in wf.card_records(self.fake.shows["t_2"]) if r["type"] == "peer-run"]), 1)

    def test_a_crash_before_the_run_id_resends_the_same_key(self):
        push(self.fake)
        self.fake.fail_peer_run = True
        with self.assertRaises(wf.WorkflowError):
            nw.dispatch("t_2", self.fake)
        self.assertEqual(wf.read_ledger("t_2")[-1]["run_id"], "")
        self.fake.fail_peer_run = False
        rec = nw.dispatch("t_2", self.fake)
        self.assertEqual((rec["attempt"], rec["idempotency_key"]), (1, "kanban:t_2:1"))
        self.assertEqual([flag(c, "--idempotency-key") for c in self.fake.peer_calls("run")],
                         ["kanban:t_2:1", "kanban:t_2:1"])

    def test_env_is_never_authority(self):
        """A claimed or forged task id in the environment changes nothing: the card named on the
        command line is data and is read from the board; the env pins no identity."""
        push(self.fake)
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_3", "HERMES_KANBAN_RUN_ID": "99",
                                          "HERMES_PROFILE": "reviewer", "HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
            rec = nw.dispatch("t_2", self.fake)
        self.assertEqual((rec["attempt"], self.fake.of("comment")[-1][0]["task_id"]), (1, "t_2"))
        self.assertEqual(self.fake.of("comment")[-1][1], "mac-worker")

    def test_unblock_resumes_a_live_run_and_restarts_an_ended_one(self):
        push(self.fake)
        nw.dispatch("t_2", self.fake)
        self.fake.shows["t_2"]["events"].append({"kind": "unblocked", "payload": {}})
        self.fake.remote = {"status": "waiting_for_approval"}
        self.assertTrue(nw.dispatch("t_2", self.fake)["resumed"])
        self.fake.remote = {"status": "completed", "output": evidence("blocked", blocker="device: no iPhone")}
        rec = nw.dispatch("t_2", self.fake)
        self.assertEqual((rec["attempt"], rec["run_id"]), (2, "run_t_2_2"))
        self.assertIn("The operator cleared the earlier block", self.fake.runs[-1]["message"])
        self.assertIn(f".hermes-validate/t_2 checkout --detach {SHA}", self.fake.runs[-1]["message"])

    def test_fails_closed_without_a_peer(self):
        push(self.fake)
        self.fake.peers = {}
        with self.assertRaisesRegex(wf.WorkflowError, "no Hermes peer 'mac'.*PROFILE_PEER_URL"):
            nw.dispatch("t_2", self.fake)
        self.assertEqual(self.fake.runs, [])

    def test_only_validation_cards(self):
        push(self.fake)
        for tid in ("t_1", "t_4", "t_5"):
            with self.assertRaisesRegex(wf.WorkflowError, "not a validation card"):
                nw.dispatch(tid, self.fake)


class WaitTest(StateDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        graph(self.fake)
        push(self.fake)
        nw.dispatch("t_2", self.fake)

    def wait(self, remote=None, timeout=0, by_run=None):
        if remote is not None:
            self.fake.remote = remote
        self.fake.remote_by_run.update(by_run or {})
        return nw.wait("t_2", timeout, 30, self.fake, sleep=lambda s: None)

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
        bare = json.dumps({"task": "t_2", "status": "passed", "sha_tested": SHA[:10],
                           "repo_dir": "/Users/norkz/Documents/app"})
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

    def test_the_mac_lane_validates_in_its_own_clone(self):
        raw = json.loads(evidence().split("\n")[2])
        for repo_dir in (None, "~/hermes-work/t_2", "/opt/repos/app"):
            with self.assertRaisesRegex(wf.EvidenceError, "not a clone under /Users/norkz/Documents"):
                wf.normalize_evidence({**raw, "repo_dir": repo_dir}, "t_2", SHA, "mac-worker")
        self.assertEqual(wf.normalize_evidence(raw, "t_2", SHA, "mac-worker")["repo_dir"],
                         "/Users/norkz/Documents/.hermes-validate/t_2")
        self.assertEqual(wf.normalize_evidence({**raw, "repo_dir": None}, "t_2", SHA, "windows-worker")["status"],
                         "pass")  # Windows keeps its fresh clone

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
        code, _ = nw.wait("t_2", 600, 30, runner, sleep=sleeps.append)
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
                nw.main(["dispatch", "t_2"], self.fake)
                self.fake.remote = {"status": "completed", "output": evidence()}
                self.assertEqual(nw.main(["wait", "t_2", "--timeout", "0"], self.fake), 0)
        self.assertNotIn(PEER_KEY, out.getvalue())
        self.assertNotIn(PEER_KEY, json.dumps(self.fake.calls))
        self.assertNotIn(PEER_KEY, json.dumps(self.fake.shows))

    def test_wait_needs_a_dispatch_first(self):
        with self.assertRaisesRegex(wf.WorkflowError, "no peer run recorded"):
            nw.wait("t_3", 0, 30, self.fake)

    def test_cli_exit_codes(self):
        self.fake.remote = {"status": "running"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nw.main(["wait", "t_2", "--timeout", "0"], self.fake), 3)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(nw.main(["wait", "t_4", "--timeout", "0"], self.fake), 2)
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
        code, out = nw.advance("t_4", self.fake)
        self.assertEqual((code, out["state"]), (0, "VALIDATED"))
        (review,) = self.fake.by_title("Review: ")
        self.assertEqual(out["metadata"]["next"], {"review": review})
        h = wf.header_of(self.fake.shows[review])
        self.assertEqual((h["workflow"], h["commit"], h["round"], h["round-card"]), ("review", SHA, "1", "t_4"))
        self.assertEqual(self.fake.shows[review]["parents"], ["t_4"])
        self.assertEqual(self.fake.shows["t_5"]["parents"], ["t_4", review])
        self.assertEqual(self.fake.shows[review]["task"]["assignee"], "reviewer")
        self.assertEqual(self.fake.of("link")[-1][0], {"parent_id": review, "child_id": "t_5"})
        self.assertEqual(nw.advance("t_4", self.fake)[1]["metadata"]["next"], {"review": review})  # idempotent
        self.assertEqual(len(self.fake.by_title("Review: ")), 1)

    def test_a_failure_opens_a_fix_round_with_the_evidence(self):
        self.lanes(win="fail")
        code, out = nw.advance("t_4", self.fake)
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
        self.assertEqual(self.fake.cli_writes, [])  # subscriptions are inherited natively from the parents
        self.assertEqual(nw.advance("t_4", self.fake)[1]["metadata"]["next"], nxt)  # idempotent

    def test_lane_without_evidence_blocks(self):
        self.lanes()
        self.fake.shows["t_3"]["comments"] = []
        os.unlink(wf.ledger_path("t_3"))
        code, out = nw.advance("t_4", self.fake)
        self.assertEqual(code, 4)
        self.assertIn("t_3 (windows-worker) is done without evidence", out["blocker"])

    def test_fix_loop_limit_blocks_and_an_unblock_grants_a_round(self):
        self.lanes(win="fail")
        nxt = nw.advance("t_4", self.fake)[1]["metadata"]["next"]
        self.fake.finish("t_4")
        self.sha = SHA2
        push(self.fake, nxt["code"], sha=SHA2)
        self.lanes(win="fail", ids=list(nxt["lanes"].values()))
        code, out = nw.advance(nxt["round_card"], self.fake)
        self.assertEqual(code, 4)
        self.assertIn("fix-loop: round 2 of 2 failed", out["blocker"])
        self.fake.shows[nxt["round_card"]]["events"].append({"kind": "unblocked", "payload": {}})
        code, out = nw.advance(nxt["round_card"], self.fake)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        self.assertTrue(self.fake.by_title("[code 3]"))

    def test_cli_exit_codes(self):
        self.lanes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nw.main(["advance", "t_4"], self.fake), 0)
        self.assertIn("VALIDATED", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(nw.main(["advance", "t_2"], self.fake), 2)


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
        code, out = nw.advance(round_id, self.fake)
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
            code, out = nw.verdict(review, "approve", "", self.fake)
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
            self.assertEqual(nw.main(["gate", "t_5"], self.fake), 0)
            nw.main(["status", "t_5"], self.fake)
        self.assertIn("review gate: PASS", out.getvalue())
        self.assertIn("[peer mac run run_", out.getvalue())

    def test_changes_requested_opens_the_next_round(self):
        review = self.round("t_1", ["t_2", "t_3"], "t_4", SHA, ["pass", "pass"])["review"]
        with self.assertRaisesRegex(wf.WorkflowError, "needs --reason"):
            nw.verdict(review, "changes", "", self.fake)
        code, out = nw.verdict(review, "changes", "1. Add a test for the empty state", self.fake)
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
        nw.verdict(review, "approve", "", self.fake)
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
        self.fake.claim(review, "default")  # the board's running run, not HERMES_PROFILE, names the reviewer
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "reviewer"}):
            with self.assertRaisesRegex(wf.WorkflowError, "profile 'default' wrote or relayed"):
                nw.verdict(review, "approve", "", self.fake)

    def test_verdict_needs_the_review_cards_running_run(self):
        review = self.round("t_1", ["t_2", "t_3"], "t_4", SHA, ["pass", "pass"])["review"]
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "reviewer"}):
            with self.assertRaisesRegex(wf.WorkflowError, "running run"):
                wf.verdict(review, "approve", "", self.fake)

    def test_gate_needs_rounds(self):
        run = lambda argv: done(0, json.dumps({"task": {"id": "t_x", "status": "todo"}, "parents": []}))  # noqa: E731
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nw.main(["gate", "t_x"], run), 1)


def mobile_output(status="pass", task="t_2", sha=SHA, sims=("iphone", "ipad"), sim_over=None, **extra):
    """An iOS/iPad lane evidence block; `sim_over` patches one simulator entry by kind."""
    names = {"iphone": ("iPhone 17", "iOS 26.0"), "ipad": ("iPad Pro 13-inch (M5)", "iPadOS 26.0")}
    entries = []
    for kind in sims:
        entry = {"kind": kind, "name": names[kind][0], "os": names[kind][1], "udid": f"{kind.upper()}-UDID-1",
                 "destination": f"platform=iOS Simulator,id={kind.upper()}-UDID-1", "build_exit": 0,
                 "test_exit": 0, "install_exit": 0, "launch_exit": 0, "bundle_id": "com.acme.app",
                 "result_bundle": f"~/hermes-work/{task}/{kind}.xcresult",
                 "tests": {"passed": 20, "failed": 0, "skipped": 0},
                 "screenshots": [f"~/hermes-work/{task}/{kind}.png"], "log_excerpt": "App launched"}
        entry.update((sim_over or {}).get(kind, {}))
        entries.append(entry)
    fields = {"task": task, "status": status, "sha_tested": sha, "repo_dir": "/Users/norkz/Documents/app",
              "platform": "iOS simulator", "os": "macOS 16.1", "tools": "Xcode 27.0",
              "commands": [{"cmd": "xcodebuild test-without-building -scheme App", "exit_code": 0}],
              "tests": {"passed": 40, "failed": 0, "skipped": 0}, "failures": [], "artifacts": [],
              "simulators": entries, "dirty_tree": False, "blocker": "", "summary": "both simulators", **extra}
    return f"BEGIN-EVIDENCE\n{json.dumps(fields)}\nEND-EVIDENCE\n"


def mobile_raw(**kw):
    return json.loads(mobile_output(**kw).split("\n")[1])


class MobileTest(StateDirMixin, unittest.TestCase):
    """iOS/iPad-first staged validation: writer -> Mac iPhone + iPad simulators ->
    BrowserStack (optional, disabled) ; Windows deferred."""

    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        # t_1 code, t_2 mac, t_3 round, t_4 report
        nw.intake(intake_args("--lane", "mac", "--mobile", "ios", "--key", "m", "--body", "iPad checkout"), self.fake)
        os.environ.pop(wf.BROWSERSTACK_FLAG, None)

    def test_windows_is_deferred_and_the_mac_lane_is_required(self):
        for lanes, text in ((["--lane", "mac", "--lane", "windows"], "Windows lane is deferred"),
                            (["--lane", "windows"], "Windows lane is deferred"),
                            (["--lane", "qa", "--peer", "qa=mac"], "Mac lane only")):
            fake = FakeHermes()
            with self.assertRaisesRegex(wf.WorkflowError, text):
                nw.intake(intake_args(*lanes, "--mobile", "ios", "--key", "x"), fake)
            self.assertEqual(fake.calls, [])
        self.assertEqual(nw.intake(intake_args("--lane", "windows", "--key", "w", "--dry-run"), FakeHermes())
                         ["lanes"][0]["assignee"], "windows-worker")  # non-mobile Windows lanes still work

    def test_dry_run_shows_the_staged_order(self):
        fake = FakeHermes()
        plan = nw.intake(intake_args("--lane", "mac", "--mobile", "ios", "--key", "d", "--dry-run"), fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual([s["stage"] for s in plan["stages"]], [*wf.MOBILE_STAGES, "windows"])
        self.assertEqual(wf.MOBILE_STAGES, ("writer", "mac-iphone", "mac-ipad", "browserstack"))
        self.assertEqual((plan["stages"][3]["status"], plan["stages"][3]["reason"]), ("skipped", "disabled"))
        self.assertEqual(plan["stages"][4]["status"], "deferred")

    def test_cards_carry_the_mobile_header(self):
        self.assertEqual([wf.card_header(self.fake.body(t)).get("mobile") for t in ("t_1", "t_2", "t_3")],
                         ["ios"] * 3)
        self.assertIn("bundle id and the screens to check on iPhone and iPad", self.fake.body("t_1"))
        self.assertIn('"skipped: disabled"', self.fake.body("t_4"))
        fake = FakeHermes()
        graph(fake)
        self.assertNotIn("mobile", wf.card_header(fake.body("t_2")))

    def test_the_mac_gets_an_iphone_and_ipad_task_for_the_exact_sha(self):
        push(self.fake)
        nw.dispatch("t_2", self.fake)
        msg = self.fake.runs[-1]["message"]
        for text in (f"under {wf.MAC_REPO_ROOT}", "/opt/repos/...", "never use them",
                     "Never change that clone's own checkout",
                     f"git -C <clone> worktree add --detach /Users/norkz/Documents/.hermes-validate/t_2 {SHA}",
                     "git status --porcelain --untracked-files=no", f"checkout --detach {SHA}",
                     '"repo_dir": "/Users/norkz/Documents/.hermes-validate/t_2"',
                     f"git rev-parse HEAD       (must print {SHA} exactly", '"workspace: <dir> has uncommitted',
                     "xcrun simctl list devices available --json", "xcrun simctl boot <udid>",
                     "xcodebuild build-for-testing -scheme <scheme> -destination 'platform=iOS Simulator,id=<iphone-udid>' "
                     "-destination 'platform=iOS Simulator,id=<ipad-udid>'",
                     "xcodebuild test-without-building -scheme <scheme> -destination 'platform=iOS Simulator,id=<ipad-udid>'",
                     "-resultBundlePath ~/hermes-work/t_2/ipad.xcresult", "xcrun simctl install <udid> <app>",
                     "xcrun simctl launch <udid> <bundle-id>", "xcrun simctl io <udid> screenshot",
                     "BrowserStack or any paid service", '"kind": "iphone"', '"kind": "ipad"'):
            self.assertIn(text, msg)
        self.assertNotIn("git clone", msg)

    def test_a_pass_fails_closed_without_both_simulators_the_full_sha_or_the_mac_clone(self):
        bad = (({"sims": ("iphone",)}, "no ipad simulator evidence"),
               ({"sha": SHA[:12]}, "full SHA"),
               ({"repo_dir": "/opt/repos/app"}, "not a clone under /Users/norkz/Documents"),
               ({"repo_dir": "/Users/norkz/Documents/../tmp/app"}, "not a clone under"),
               ({"repo_dir": "~/hermes-work/t_2"}, "not a clone under"),
               ({"sim_over": {"ipad": {"screenshots": []}}}, "ipad simulator evidence lacks screenshot"),
               ({"sim_over": {"iphone": {"launch_exit": None, "udid": ""}}}, "lacks launch exit code, udid"))
        for kw, text in bad:
            with self.assertRaisesRegex(wf.EvidenceError, text):
                wf.normalize_evidence(mobile_raw(**kw), "t_2", SHA, "mac-worker", "ios")
        ev = wf.normalize_evidence(mobile_raw(), "t_2", SHA, "mac-worker", "ios")
        self.assertEqual((ev["status"], ev["repo_dir"], [s["kind"] for s in ev["simulators"]]),
                         ("pass", "/Users/norkz/Documents/app", ["iphone", "ipad"]))
        self.assertEqual(ev["device"], "iPhone 17 (iOS 26.0, IPHONE-UDID-1); "
                                       "iPad Pro 13-inch (M5) (iPadOS 26.0, IPAD-UDID-1)")
        self.assertEqual(ev["simulators"][1]["result_bundle"], "~/hermes-work/t_2/ipad.xcresult")

    def test_a_failing_simulator_step_is_a_fail_and_a_dirty_clone_blocks(self):
        ev = wf.normalize_evidence(mobile_raw(sim_over={"ipad": {"launch_exit": 1}}), "t_2", SHA, "mac-worker", "ios")
        self.assertEqual(ev["status"], "fail")
        self.assertIn("ipad (iPad Pro 13-inch (M5), iPadOS 26.0): launch exit 1", ev["failures"])
        ev = wf.normalize_evidence(mobile_raw(status="fail", sims=("iphone",),
                                              sim_over={"iphone": {"build_exit": 65}}), "t_2", SHA, "mac-worker", "ios")
        self.assertEqual(ev["status"], "fail")  # a failed build may stop before the iPad
        blocked = {"task": "t_2", "status": "blocked",
                   "blocker": "workspace: /Users/norkz/Documents/app has uncommitted tracked changes"}
        ev = wf.normalize_evidence(blocked, "t_2", SHA, "mac-worker", "ios")
        self.assertEqual((ev["status"], ev["blocker"][:10]), ("blocked", "workspace:"))

    def test_wait_retries_evidence_without_the_ipad(self):
        push(self.fake)
        nw.dispatch("t_2", self.fake)
        self.fake.remote_by_run = {"run_t_2_1": {"status": "completed", "output": mobile_output(sims=("iphone",))},
                                   "run_t_2_2": {"status": "completed", "output": mobile_output()}}
        code, out = nw.wait("t_2", 0, 30, self.fake, sleep=lambda s: None)
        self.assertEqual((code, out["run_id"], out["verdict"]), (0, "run_t_2_2", "pass"))
        self.assertIn("no ipad simulator evidence", self.fake.runs[-1]["message"])
        self.assertIn("iPhone 17 (iOS 26.0, IPHONE-UDID-1)", out["summary"])
        self.assertEqual(len(wf.latest(card(self.fake, "t_2"), "evidence")["simulators"]), 2)

    def test_round_records_the_stages_and_browserstack_stays_skipped(self):
        push(self.fake)
        self.assertEqual(validate(self.fake, "t_2", mobile_output())[0], 0)
        for env, reason in (({}, "disabled"), ({wf.BROWSERSTACK_FLAG: "true"}, "disabled")):  # env alone: off
            with mock.patch.dict(os.environ, env, clear=False):
                code, out = nw.advance("t_3", self.fake)
            self.assertEqual((code, out["state"]), (0, "VALIDATED"))
            stages = {s["stage"]: s for s in out["metadata"]["stages"]}
            self.assertEqual([s["stage"] for s in out["metadata"]["stages"]], [*wf.MOBILE_STAGES, "windows"])
            self.assertEqual((stages["writer"]["commit"], stages["mac-iphone"]["status"],
                              stages["mac-ipad"]["status"], stages["windows"]["status"]),
                             (SHA, "pass", "pass", "deferred"))
            self.assertEqual(stages["browserstack"]["status"], "skipped")  # never "pass": nothing ran
            self.assertIn(reason, stages["browserstack"]["reason"])
        self.assertIn("browserstack skipped: disabled; windows deferred", nw.advance("t_3", self.fake)[1]["summary"])
        (review,) = self.fake.by_title("Review: ")
        self.assertIn("on an iPhone and an iPad simulator", self.fake.body(review))

        self.fake.finish("t_3")
        nw.verdict(review, "approve", "", self.fake)
        self.fake.finish(review, profile="reviewer")
        ok, rows = wf.gate("t_4", self.fake)
        self.assertTrue(ok, rows)
        self.assertIn("browserstack skipped: disabled (not run: nothing was tested on real devices, never "
                      "counted as passed); windows deferred", [r["reason"] for r in rows])
        self.assertEqual(self.fake.by_title("[devices "), [])

    def test_a_failing_ipad_opens_a_fix_round_and_skips_browserstack(self):
        push(self.fake)
        validate(self.fake, "t_2", mobile_output(sim_over={"ipad": {"test_exit": 65,
                                                                    "tests": {"passed": 18, "failed": 2}}}))
        code, out = nw.advance("t_3", self.fake)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        stages = {s["stage"]: s for s in out["metadata"]["stages"]}
        self.assertEqual((stages["mac-iphone"]["status"], stages["mac-ipad"]["status"]), ("pass", "fail"))
        self.assertEqual(stages["browserstack"]["reason"], "the local simulators did not both pass")
        body = self.fake.body(out["metadata"]["next"]["code"])
        self.assertIn("ipad (iPad Pro 13-inch (M5), iPadOS 26.0): test exit 65, 2 test(s) failed", body)
        self.assertIn("ipad: iPad Pro 13-inch (M5) iPadOS 26.0 IPAD-UDID-1; build exit 0, test exit 65", body)
        self.assertEqual(wf.card_header(self.fake.body(out["metadata"]["next"]["code"]))["mobile"], "ios")


BS_USER, BS_KEY = "acme-ci", "bsKEY0123456789abcdefSECRET"  # the key must never leave browserstack.py
BUILD_ID = "a" * 40
DEVICES = "iPhone 15-17|iPad Air 13 2025-18"


def bs_activate(path, mode=0o600, **over):
    """Write the operator's activation file (what a human does once, by hand)."""
    values = {wf.bs.FLAG: "true", wf.bs.USER_VAR: BS_USER, wf.bs.KEY_VAR: BS_KEY, wf.bs.DEVICES_VAR: DEVICES, **over}
    Path(path).write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8")
    os.chmod(path, mode)


def api_build(build_id=BUILD_ID, sha=SHA, iphone="passed", ipad="passed", status="done", tag=None, failed=0):
    def device(name, osv, st, kind):
        return {"device": name, "os": "ios", "os_version": osv, "sessions": [
            {"id": f"{kind}session0123456789", "status": st, "duration": 61, "testcases": {
                "count": 12, "status": {"passed": 12 - (failed if st != "passed" else 0),
                                        "failed": failed if st != "passed" else 0, "skipped": 0,
                                        "timedout": 0, "error": 0}}}]}
    devices = [device("iPhone 15", "17", iphone, "iphone")] if iphone else []
    devices += [device("iPad Air 13 2025", "18", ipad, "ipad")] if ipad else []
    return {"id": build_id, "framework": "xcuitest", "status": status, "duration": 90,
            "input_capabilities": {"devices": DEVICES.split("|"), "project": "hermes-kanban",
                                   "buildTag": sha if tag is None else tag},
            "app_details": {"bundle_id": "com.acme.app"}, "devices": devices}


class FakeBrowserStack:
    """The BrowserStack REST API: GET builds/<id> answers from `builds`."""

    def __init__(self, builds=None):
        self.builds = builds or {}
        self.calls = []

    def __call__(self, method, url, headers):
        self.calls.append((method, url, headers))
        build_id = url.rsplit("/", 1)[-1]
        if headers.get("Authorization") != "Basic " + __import__("base64").b64encode(
                f"{BS_USER}:{BS_KEY}".encode()).decode():
            return 401, b'{"message": "Unauthorized"}'
        if build_id not in self.builds:
            return 404, b'{"message": "not found"}'
        return 200, json.dumps(self.builds[build_id]).encode()


def device_output(status="pass", task="t_5", sha=SHA, build_id=BUILD_ID, **extra):
    fields = {"task": task, "status": status, "sha_tested": sha, "repo_dir": f"/Users/norkz/Documents/.hermes-validate/{task}",
              "build_id": build_id, "app_url": "bs://app1", "test_suite_url": "bs://suite1",
              "devices": DEVICES.split("|"), "commands": [{"cmd": "curl ... /build", "exit_code": 0}],
              "failures": [], "dirty_tree": False, "blocker": "", "summary": "real devices", **extra}
    return f"BEGIN-EVIDENCE\n{json.dumps(fields)}\nEND-EVIDENCE\n"


class BrowserStackActivationTest(StateDirMixin, unittest.TestCase):
    """browserstack.py: disabled by default, fail-closed activation, API-only verification."""

    def setUp(self):
        super().setUp()
        self.path = os.environ["KANBAN_BROWSERSTACK_CONFIG"]

    def test_disabled_without_the_file_or_the_flag(self):
        self.assertEqual(wf.bs.activation()["state"], "disabled")
        bs_activate(self.path, **{wf.bs.FLAG: "false"})
        self.assertEqual(wf.bs.activation()["state"], "disabled")
        with mock.patch.dict(os.environ, {wf.bs.FLAG: "true", wf.bs.USER_VAR: BS_USER, wf.bs.KEY_VAR: BS_KEY}):
            self.assertEqual(wf.bs.activation()["state"], "disabled")  # the environment alone never activates

    def test_partial_or_unsafe_activation_is_misconfigured(self):
        cases = (({wf.bs.KEY_VAR: None}, {}, "BROWSERSTACK_ACCESS_KEY is not set"),
                 ({wf.bs.DEVICES_VAR: "iPhone 15-17"}, {}, "at least one ipad"),
                 ({wf.bs.DEVICES_VAR: "Pixel 8-14|iPad Air 13 2025-18"}, {}, "invalid entries"),
                 ({}, {"mode": 0o644}, "mode 0600"))
        for over, kw, text in cases:
            bs_activate(self.path, **kw, **over)
            act = wf.bs.activation()
            self.assertEqual(act["state"], "misconfigured", over)
            self.assertIn(text, act["reason"])
            self.assertNotIn(BS_KEY, json.dumps(act))
        bs_activate(self.path)
        act = wf.bs.activation()
        self.assertEqual((act["state"], act["devices"]), ("active", DEVICES.split("|")))
        self.assertNotIn(BS_KEY, json.dumps(act))

    def test_preflight_cli_exit_codes_and_no_secret_output(self):
        for setup, want in ((None, 3), ({wf.bs.USER_VAR: None}, 2), ({}, 0)):
            if setup is not None:
                bs_activate(self.path, **setup)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(wf.bs.main(["preflight"]), want)
            self.assertNotIn(BS_KEY, out.getvalue())

    def test_verify_reads_the_build_from_the_api(self):
        bs_activate(self.path)
        api = FakeBrowserStack({BUILD_ID: api_build()})
        out = wf.bs.verify(BUILD_ID, SHA, http=api)
        self.assertEqual((out["status"], out["verified_by"], out["build_status"]), ("pass", "browserstack-api", "done"))
        self.assertEqual([d["kind"] for d in out["devices"]], ["iphone", "ipad"])
        self.assertEqual(out["dashboard_url"], wf.bs.DASHBOARD + BUILD_ID)
        self.assertEqual(api.calls[0][:2], ("GET", f"{wf.bs.API}/builds/{BUILD_ID}"))
        self.assertNotIn(BS_KEY, json.dumps(out))

    def test_verify_fails_closed(self):
        bs_activate(self.path)
        raising = (({"tag": SHA2}, "not the exact SHA"), ({"tag": ""}, "tagged 'nothing'"),
                   ({"status": "running"}, "has not finished"))
        for kw, text in raising:
            with self.assertRaisesRegex(wf.bs.VerifyError, text):
                wf.bs.verify(BUILD_ID, SHA, http=FakeBrowserStack({BUILD_ID: api_build(**kw)}))
        for build_id, text in (("b" * 40, "no build"), ("x;rm -rf", "not a BrowserStack build id")):
            with self.assertRaisesRegex(wf.bs.VerifyError, text):
                wf.bs.verify(build_id, SHA, http=FakeBrowserStack({BUILD_ID: api_build()}))
        with mock.patch.object(wf.bs, "read_config", lambda p: {wf.bs.FLAG: "true", wf.bs.USER_VAR: "x",
                                                                wf.bs.KEY_VAR: "wrong", wf.bs.DEVICES_VAR: DEVICES}):
            with self.assertRaisesRegex(wf.bs.VerifyError, "401"):
                wf.bs.verify(BUILD_ID, SHA, http=FakeBrowserStack({BUILD_ID: api_build()}))
        failing = (({"ipad": "failed", "failed": 2}, "iPad Air 13 2025 (iOS 18) session ipadsession0123456789: "
                                                     "status failed, 10 passed, 2 failed"),
                   ({"ipad": None}, "no real ipad session"),
                   ({"status": "failed"}, "ended with status failed"))
        for kw, text in failing:
            out = wf.bs.verify(BUILD_ID, SHA, http=FakeBrowserStack({BUILD_ID: api_build(**kw)}))
            self.assertEqual(out["status"], "fail", kw)
            self.assertIn(text, " | ".join(out["failures"]))

    def test_verify_needs_an_active_stage(self):
        with self.assertRaisesRegex(wf.bs.VerifyError, "disabled"):
            wf.bs.verify(BUILD_ID, SHA, http=FakeBrowserStack({BUILD_ID: api_build()}))


class BrowserStackStageTest(StateDirMixin, unittest.TestCase):
    """Mac simulators pass -> [devices N] on BrowserStack -> [devices-round N] -> review or fix round."""

    def setUp(self):
        super().setUp()
        self.path = os.environ["KANBAN_BROWSERSTACK_CONFIG"]
        self.api = FakeBrowserStack({BUILD_ID: api_build()})
        patcher = mock.patch.object(wf, "BROWSERSTACK_HTTP", self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeHermes()
        # t_1 code, t_2 mac, t_3 round, t_4 report
        nw.intake(intake_args("--lane", "mac", "--mobile", "ios", "--key", "m", "--body", "iPad checkout"), self.fake)
        push(self.fake)
        self.assertEqual(validate(self.fake, "t_2", mobile_output())[0], 0)

    def open_devices(self):
        bs_activate(self.path)
        code, out = nw.advance("t_3", self.fake)
        self.assertEqual((code, out["state"]), (0, "SIMULATORS_PASSED"), out)
        self.fake.finish("t_3")
        return out

    def test_a_partial_activation_blocks_instead_of_skipping(self):
        bs_activate(self.path, **{wf.bs.KEY_VAR: None})
        code, out = nw.advance("t_3", self.fake)
        self.assertEqual(code, 4)
        self.assertIn("browserstack:", out["blocker"])
        self.assertIn("BROWSERSTACK_ACCESS_KEY is not set", out["blocker"])
        self.assertEqual((self.fake.by_title("Review: "), self.fake.by_title("[devices ")), ([], []))

    def test_active_stage_opens_the_device_cards_for_the_same_sha(self):
        out = self.open_devices()
        dev, drnd = out["metadata"]["next"]["devices"], out["metadata"]["next"]["devices_round"]
        self.assertEqual((dev, drnd), ("t_5", "t_6"))
        stages = {s["stage"]: s for s in out["metadata"]["stages"]}
        self.assertEqual(stages["browserstack"]["status"], "queued")
        h = wf.header_of(self.fake.shows[dev])
        self.assertEqual((h["workflow"], h["stage"], h["devices"], h["code"], h["peer"]),
                         ("validate", "devices", DEVICES, "t_1", "mac"))
        self.assertEqual(self.fake.shows[dev]["task"]["assignee"], "mac-worker")
        self.assertEqual(self.fake.shows[dev]["parents"], ["t_3"])
        self.assertEqual(self.fake.shows[drnd]["parents"], [dev])
        self.assertIn(drnd, self.fake.shows["t_4"]["parents"])
        self.assertEqual(self.fake.by_title("Review: "), [])
        nw.dispatch(dev, self.fake)
        msg = self.fake.runs[-1]["message"]
        for text in (f'"buildTag": "{SHA}"', f"worktree add --detach /Users/norkz/Documents/.hermes-validate/{dev} {SHA}",
                     '. ~/.hermes/browserstack.env', 'curl -sS -u "$BROWSERSTACK_USERNAME:$BROWSERSTACK_ACCESS_KEY"',
                     f"{wf.bs.API}/build", "never `set -x`", "human-approval: signing",
                     "browserstack: credentials not provisioned on the Mac", '"build_id"',
                     "iPhone 15-17, iPad Air 13 2025-18"):
            self.assertIn(text, msg)
        self.assertNotIn(BS_KEY, msg)

    def test_verified_pass_reaches_review_and_gate(self):
        dev, drnd = self.open_devices()["metadata"]["next"].values()
        code, out = validate(self.fake, dev, device_output(task=dev))
        self.assertEqual((code, out["metadata"]["state"]), (0, "DEVICES_PASSED"), out)
        self.assertEqual(out["metadata"]["verified_by"], "browserstack-api")
        ev = wf.latest(card(self.fake, dev), "evidence")
        self.assertEqual((ev["status"], ev["build_id"], ev["tests"]["passed"]), ("pass", BUILD_ID, 24))
        code, out = nw.advance(drnd, self.fake)
        self.assertEqual((code, out["state"]), (0, "VALIDATED"))
        self.assertIn("browserstack pass: build " + BUILD_ID, out["summary"])
        review = out["metadata"]["next"]["review"]
        self.assertIn("BrowserStack real devices passed: build " + BUILD_ID, self.fake.body(review))
        self.fake.finish(drnd)
        nw.verdict(review, "approve", "", self.fake)
        self.fake.finish(review, profile="reviewer")
        ok, rows = wf.gate("t_4", self.fake)
        self.assertTrue(ok, rows)
        reason = " ".join(r["reason"] for r in rows)
        self.assertIn(f"browserstack passed {SHA[:12]} on real devices: build {BUILD_ID} on iPhone 15 (iOS 17), "
                      "iPad Air 13 2025 (iOS 18), verified via the BrowserStack API", reason)
        states = [(r["kind"], r["round"], r.get("stage"), r["state"]) for r in wf.status("t_4", self.fake)]
        self.assertEqual(states[-3:], [("validate", 1, "devices", "DEVICES_PASSED"), ("round", 1, "devices", "VALIDATED"),
                                       ("report", None, None, "WAITING")])
        board = json.dumps(self.fake.shows) + json.dumps(self.fake.calls)
        board += "".join(Path(self._state, f).read_text() for f in os.listdir(self._state) if f.endswith(".jsonl"))
        self.assertNotIn(BS_KEY, board)
        self.assertNotIn(BS_USER, board)

    def test_a_failing_real_device_opens_a_fix_round(self):
        self.api.builds[BUILD_ID] = api_build(ipad="failed", failed=2, status="failed")
        dev, drnd = self.open_devices()["metadata"]["next"].values()
        code, out = validate(self.fake, dev, device_output(task=dev))  # the Mac says pass; the API decides
        self.assertEqual((code, out["metadata"]["state"]), (0, "DEVICES_FAILED"))
        code, out = nw.advance(drnd, self.fake)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        self.assertEqual({s["stage"]: s["status"] for s in out["metadata"]["stages"]}["browserstack"], "fail")
        nxt = out["metadata"]["next"]
        body = self.fake.body(nxt["code"])
        self.assertIn("iPad Air 13 2025 (iOS 18) session ipadsession0123456789: status failed", body)
        self.assertIn(wf.bs.DASHBOARD + BUILD_ID, body)
        self.assertEqual(self.fake.shows[nxt["code"]]["parents"], [drnd])
        self.assertEqual(list(nxt["lanes"]), ["mac-worker"])  # the fix goes back through the Mac simulators
        self.fake.finish(drnd)
        self.assertFalse(wf.gate("t_4", self.fake)[0])

    def test_no_pass_without_a_real_verified_build(self):
        dev, _ = self.open_devices()["metadata"]["next"].values()
        for output, text in ((device_output(task=dev, build_id=""), "needs the BrowserStack build_id"),
                             (device_output(task=dev, build_id="f" * 40), "no build"),
                             (device_output(task=dev, sha=SHA[:12]), "full SHA"),
                             (device_output(task=dev, repo_dir="/opt/repos/app"), "not a clone under")):
            raw = wf.extract_report(output)
            with self.assertRaisesRegex(wf.EvidenceError, text):
                wf.normalize_evidence(raw, dev, SHA, "mac-worker", "ios", "devices", DEVICES.split("|"))
        nw.dispatch(dev, self.fake)
        for run_id in ("run_t_5_1", "run_t_5_2", "run_t_5_3"):
            self.fake.remote_by_run[run_id] = {"status": "completed", "output": device_output(task=dev, build_id="f" * 40)}
        code, out = nw.wait(dev, 0, 30, self.fake, sleep=lambda s: None)
        self.assertEqual(code, 1)
        self.assertIn("no build", out["error"])
        self.assertIsNone(wf.latest(card(self.fake, dev), "evidence"))

    def test_device_result_blocks_when_the_coordinator_cannot_verify(self):
        dev, _ = self.open_devices()["metadata"]["next"].values()
        os.unlink(self.path)  # the operator turned the stage off mid-run
        code, out = validate(self.fake, dev, device_output(task=dev))
        self.assertEqual(code, 4)
        self.assertIn("cannot be verified, thus it is not a pass", out["blocker"])
        self.assertEqual(self.api.calls, [])

    def test_gate_refuses_a_queued_stage_without_its_result(self):
        self.open_devices()
        h = self.fake.shows["t_4"]
        h["parents"] = [p for p in h["parents"] if p != "t_6"]  # a report that lost the device round
        rows = wf.gate("t_4", self.fake)[1]
        self.assertIn("did not finish", " ".join(r["reason"] for r in rows))


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


UPSTREAM_SRC = os.environ.get("HERMES_UPSTREAM_SRC", "")
UPSTREAM_PYTHON = os.environ.get("HERMES_UPSTREAM_PYTHON", "")
HAS_UPSTREAM = bool(UPSTREAM_SRC and UPSTREAM_PYTHON and Path(UPSTREAM_SRC, "tools", "kanban_tools.py").is_file()
                    and Path(UPSTREAM_PYTHON).is_file() and Path(UPSTREAM_PYTHON).with_name("hermes").is_file())
NO_UPSTREAM = ("set HERMES_UPSTREAM_SRC (a pinned upstream checkout) and HERMES_UPSTREAM_PYTHON (its venv's "
               "python, with bin/hermes) to run against the real runtime in a throwaway HERMES_HOME")


class UpstreamBoard(StateDirMixin):
    """A throwaway HERMES_HOME on the pinned upstream runtime: the real `hermes kanban` CLI for
    reads (and for the dispatcher's own claim/complete, which this harness stands in for) and
    the real kanban_create / kanban_comment / kanban_link handlers for the kit's native ops."""

    def setUp(self):
        super().setUp()
        self.peer_fake = FakeHermes()
        self.home = tempfile.mkdtemp(prefix="kanban-upstream-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = str(Path(UPSTREAM_PYTHON).with_name("hermes"))
        self.env = {"PATH": f"{Path(UPSTREAM_PYTHON).parent}:/usr/bin:/bin", "HOME": self.home,
                    "HERMES_HOME": os.path.join(self.home, ".hermes"), "TMPDIR": tempfile.gettempdir(),
                    "PYTHONPATH": UPSTREAM_SRC, "PYTHONDONTWRITEBYTECODE": "1"}
        os.makedirs(self.env["HERMES_HOME"])
        self.assertEqual(self.hermes(["kanban", "init"]).returncode, 0)

    def hermes(self, argv, env=None):
        return subprocess.run([self.bin, *argv], capture_output=True, text=True, env={**self.env, **(env or {})},
                              timeout=180, cwd=UPSTREAM_SRC)

    def run_cli(self, argv):
        # Profiles and peers do not exist in the temp home: answer them like a configured relay.
        return self.peer_fake(argv) if argv[0] == "-p" else self.hermes(argv)

    def native(self, ops, profile, env=None):
        r = subprocess.run([UPSTREAM_PYTHON, str(Path(__file__).with_name("upstream_native.py")), "apply"],
                           input=json.dumps({"ops": ops}), capture_output=True, text=True, timeout=180,
                           cwd=UPSTREAM_SRC, env={**self.env, "HERMES_PROFILE": profile, **(env or {})})
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def show(self, tid):
        return json.loads(self.hermes(["kanban", "show", tid, "--json"]).stdout)

    def bind(self, tid, pid):
        """The runtime's worker_bound event for the claimed card's current run (upstream code)."""
        r = subprocess.run([UPSTREAM_PYTHON, str(Path(__file__).with_name("upstream_native.py")), "bind", tid,
                            str(pid)], capture_output=True, text=True, timeout=180, cwd=UPSTREAM_SRC, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def drive(self, fn, *a, profile, task=None, native_env=None, **kw):
        """converge() on the real board: apply each op batch with the real handlers as `profile`."""
        for _ in range(8):
            with wf.transport() as box:
                try:
                    result, pending = fn(*a, **kw), False
                except wf.Pending:
                    result, pending = None, True
                ops = list(box.ops)
            if not ops:
                return result
            out = self.native(ops, profile, native_env)
            self.assertFalse([r for r in out if not r.get("ok")], out)
            if pending:
                roots = [r["task_id"] for op, r in zip(ops, out) if op["tool"] == "kanban_create"
                         and not op["args"].get("parents")]
                if roots:
                    a[0].bind = roots[0]
                continue
            for _ in range(4):
                with wf.transport() as box:
                    wf.sync(task, self.run_cli)
                    ops = list(box.ops)
                if not ops:
                    return result
                self.native(ops, profile, native_env)
            self.fail("ops did not read back")
        self.fail("did not converge")

    def complete(self, tid):
        """The dispatcher claims the card and its worker completes it (harness stand-in)."""
        self.assertEqual(self.hermes(["kanban", "claim", tid]).returncode, 0, tid)
        r = self.hermes(["kanban", "complete", tid, "--summary", "ok"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def validate(self, tid, sha, verdict):
        self.drive(wf.dispatch, tid, self.run_cli, profile=self.show(tid)["task"]["assignee"], task=tid)
        run_id = wf.peer_runs(self.show(tid))[-1]["run_id"]
        self.peer_fake.remote_by_run[run_id] = {"status": "completed", "output": evidence(verdict, task=tid, sha=sha)}
        code, _ = self.drive(wf.wait, tid, 0, 30, self.run_cli, profile=self.show(tid)["task"]["assignee"], task=tid)
        self.assertEqual(code, 0)
        self.complete(tid)


@unittest.skipUnless(HAS_UPSTREAM, NO_UPSTREAM)
class UpstreamCompatTest(UpstreamBoard, unittest.TestCase):
    """The kit's native ops through the real upstream handlers and `hermes kanban show --json`."""

    def test_native_schema_is_the_upstream_schema(self):
        r = subprocess.run([UPSTREAM_PYTHON, str(Path(__file__).with_name("upstream_native.py")), "schema"],
                           capture_output=True, text=True, timeout=180, cwd=UPSTREAM_SRC, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        real = {k: set(v) for k, v in json.loads(r.stdout.strip().splitlines()[-1]).items()}
        self.assertEqual(real, UPSTREAM_SCHEMA_KEYS)
        self.assertEqual(real, {k: set(v) for k, v in wf.NATIVE_SCHEMA.items()})

    def test_full_loop_round_trips_every_record_and_card(self):
        discord = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_ID": "111",
                   "HERMES_SESSION_THREAD_ID": "222", "HERMES_SESSION_CHAT_TYPE": "thread"}
        args = intake_args("--lane", "mac", "--lane", "windows", "--key", "it", "--body", "full ask")
        plan = self.drive(wf.intake, args, self.run_cli, profile="default", native_env=discord)
        req, code1, rnd1 = plan["request"]["id"], plan["code"]["id"], plan["round"]["id"]
        mac1, win1 = [lane["id"] for lane in plan["lanes"]]
        with wf.transport() as box:  # discovery on the real board: a rerun resolves every card
            again = wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "it", "--body", "full ask",
                                          "--bind", code1), self.run_cli)
        self.assertEqual((box.ops, again["request"]["id"]), ([], req))
        for tid in (req, code1, rnd1, mac1, win1):
            show = self.show(tid)
            self.assertEqual((show["task"]["created_by"], wf.native_v1(show)), ("default", True), tid)
        self.assertEqual(self.show(req)["parents"], [rnd1])
        self.assertEqual(sorted(self.show(rnd1)["parents"]), sorted([mac1, win1]))
        self.assertEqual(self.show(code1)["task"]["max_runtime_seconds"], 6 * 3600)
        subs = json.loads(self.hermes(["kanban", "notify-list", req, "--json"]).stdout or "[]")
        self.assertEqual([(s["chat_id"], s["thread_id"], s["delivery_mode"]) for s in subs],
                         [("111", "222", "notify+wake")], "upstream default for the creating session")

        self.drive(wf.put_record, self.run_cli, code1, "code-sha",
                   {"rid": f"code-sha:{SHA}", "sha": SHA, "notes": "token=abcdef123456 [REDACTED] sha256:" + "a" * 64},
                   profile="default", task=code1)
        self.complete(code1)
        self.validate(mac1, SHA, "pass")
        self.validate(win1, SHA, "fail")
        code, out = self.drive(wf.advance, rnd1, self.run_cli, profile="default", task=rnd1)
        self.assertEqual((code, out["state"]), (0, "FIX_REQUIRED"))
        nxt = out["metadata"]["next"]
        self.assertEqual(sorted(self.show(req)["parents"]), sorted([rnd1, nxt["round_card"]]))
        self.complete(rnd1)
        self.assertIn("LoginTests.testRotation", self.show(nxt["code"])["task"]["body"])
        inherited = json.loads(self.hermes(["kanban", "notify-list", nxt["code"], "--json"]).stdout or "[]")
        self.assertEqual([s["chat_id"] for s in inherited], ["111"], "child cards inherit the thread natively")

        self.drive(wf.put_record, self.run_cli, nxt["code"], "code-sha", {"rid": f"code-sha:{SHA2}", "sha": SHA2},
                   profile="default", task=nxt["code"])
        self.complete(nxt["code"])
        for tid in nxt["lanes"].values():
            self.validate(tid, SHA2, "pass")
        code, out = self.drive(wf.advance, nxt["round_card"], self.run_cli, profile="default", task=nxt["round_card"])
        self.assertEqual((code, out["state"]), (0, "VALIDATED"))
        review = out["metadata"]["next"]["review"]
        self.complete(nxt["round_card"])
        self.assertEqual(self.hermes(["kanban", "claim", review]).returncode, 0)
        self.assertEqual(self.show(review)["runs"][-1]["profile"], "reviewer")
        with wf.transport() as box:  # a claim alone binds no worker: this shell is nobody's reviewer
            with self.assertRaisesRegex(wf.CapabilityError, "no worker_bound event"):
                wf.verdict(review, "approve", "", self.run_cli)
        self.assertEqual((box.ops, wf.read_ledger(review)), ([], []))
        self.bind(review, os.getppid())  # the reviewer's managed worker: this test's parent
        with bound_adaptor():
            code, _ = self.drive(wf.verdict, review, "approve", "", self.run_cli, profile="reviewer", task=review)
        self.assertEqual(code, 0)
        self.assertEqual(self.hermes(["kanban", "complete", review, "--summary", "ok"]).returncode, 0)
        ok, rows = wf.gate(req, self.run_cli)
        self.assertTrue(ok, rows)

        # Every ledger record reached its card byte-for-byte through the runtime's redaction,
        # authored by its role's runtime profile; no CLI board write was ever issued.
        for tid in [code1, mac1, win1, rnd1, nxt["code"], nxt["round_card"], *nxt["lanes"].values(), review]:
            show = self.show(tid)
            on_card = {r["rid"]: r for r in wf.card_records(show)}
            for rec in wf.read_ledger(tid):
                if rec.get("type") == "peer-run" and not rec.get("run_id"):
                    continue
                self.assertIn(rec["rid"], on_card, (tid, rec["rid"]))
            for c in show["comments"]:
                self.assertIn(c["author"], ("default", "mac-worker", "windows-worker", "reviewer"), c)
        self.assertEqual(self.peer_fake.cli_writes, [])

    def test_delegate_fence_reads_work_and_writes_are_refused(self):
        plan = self.drive(wf.intake, intake_args("--lane", "mac", "--key", "fence"), self.run_cli, profile="default")
        code1 = plan["code"]["id"]
        root = subprocess.run([UPSTREAM_PYTHON, "-c", "from hermes_cli.kanban_db import kanban_home; print(kanban_home())"],
                              capture_output=True, text=True, env=self.env, cwd=UPSTREAM_SRC).stdout.strip()
        fence = {"HERMES_DELEGATED_CHILD_CONTEXT": root}
        self.assertEqual(self.hermes(["kanban", "show", code1, "--json"], env=fence).returncode, 0)
        self.assertEqual(self.hermes(["kanban", "notify-list", code1, "--json"], env=fence).returncode, 0)
        self.assertNotEqual(self.hermes(["kanban", "comment", code1, "x"], env=fence).returncode, 0)
        refused = self.native([{"tool": "kanban_comment", "args": {"task_id": code1, "body": "code-sha {}"}}],
                              "default", fence)
        self.assertIn("refused", refused[0].get("error", ""))
        self.assertEqual(self.show(code1)["comments"], [])


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


@unittest.skipUnless(HAS_UPSTREAM, NO_UPSTREAM)
class RealPeerTest(UpstreamBoard, unittest.TestCase):
    """The real `hermes peer` CLI, profile-scoped peer key, and a temp board (pinned runtime)."""

    def setUp(self):
        super().setUp()
        FakePeerGateway.key, FakePeerGateway.runs, FakePeerGateway.seen = PEER_KEY, {}, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePeerGateway)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for argv in (["profile", "create", "mac-worker"], ["profile", "create", "reviewer"]):
            self.assertEqual(self.hermes(argv).returncode, 0, argv)

    def test_dispatch_and_wait_over_the_peer_api(self):
        args = intake_args("--lane", "mac", "--key", "peer-it", "--body", "Fix the thing")
        with self.assertRaisesRegex(wf.WorkflowError, "no Hermes peer 'mac'"):
            wf.intake(args, self.hermes)

        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.assertEqual(self.hermes(["-p", "mac-worker", "peer", "add", "mac", "--url", url]).returncode, 0)
        with self.assertRaisesRegex(wf.WorkflowError, "peer 'mac' has no API key"):
            wf.intake(args, self.hermes)
        with open(Path(self.home) / ".hermes" / "profiles" / "mac-worker" / ".env", "a") as env:
            env.write(f"HERMES_PEER_MAC_KEY={PEER_KEY}\n")

        plan = self.drive(wf.intake, args, self.hermes, profile="default")
        code_id, lane = plan["code"]["id"], plan["lanes"][0]["id"]
        self.drive(wf.put_record, self.hermes, code_id, "code-sha", {"rid": f"code-sha:{SHA}", "sha": SHA, "notes": "n"},
                   profile="default", task=code_id)
        rec = self.drive(wf.dispatch, lane, self.hermes, profile="mac-worker", task=lane)
        self.assertEqual((rec["peer"], rec["attempt"], rec["idempotency_key"]), ("mac", 1, f"kanban:{lane}:1"))
        self.assertTrue(self.drive(wf.dispatch, lane, self.hermes, profile="mac-worker", task=lane)["resumed"])
        (sent,) = FakePeerGateway.runs.values()
        for text in (f"Kanban card {lane}", f"Commit: {SHA}", "Fix the thing", "you never change source code"):
            self.assertIn(text, sent["input"])
        self.assertNotIn("Relay steps", sent["input"])

        code, out = self.drive(wf.wait, lane, 0, 30, self.hermes, profile="mac-worker", task=lane)
        self.assertEqual((code, out["metadata"]["run_id"], out["metadata"]["commit"]), (0, rec["run_id"], SHA))
        self.assertTrue(all(ok for _, _, ok in FakePeerGateway.seen), FakePeerGateway.seen)
        board = self.hermes(["kanban", "show", lane, "--json"]).stdout
        self.assertIn(rec["run_id"], board)
        self.assertIn("evidence ", board)
        self.assertNotIn(PEER_KEY, board + json.dumps(out) + wf.ledger_path(lane).read_text())


if __name__ == "__main__":
    unittest.main()


class NativeTransportTest(StateDirMixin, unittest.TestCase):
    """The board transport: reads through `hermes kanban show`, every write a native op with the
    exact upstream schema keys, read back before anything counts; records trusted by the
    runtime author of their role; explicit ids are data; the environment is never authority."""

    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()

    def test_no_cli_board_write_anywhere(self):
        source = SCRIPT.read_text() + (SCRIPT.parent / "repo_lease.py").read_text()
        for verb in sorted(BOARD_WRITE_VERBS | {"notify-list"}):
            self.assertNotIn(f'"kanban", "{verb}"', source, verb)
        for argv in (["kanban", "comment", "t_1", "x"], ["kanban", "create", "t"], ["-p", "x", "kanban", "link"]):
            with self.assertRaisesRegex(wf.WorkflowError, "board writes are native ops"):
                wf.invoke(self.fake, argv)
        graph(self.fake)
        push(self.fake)
        validate(self.fake, "t_2", evidence("pass"))
        self.assertEqual({tuple(c[:2]) for c in self.fake.calls if c[0] == "kanban"}, {("kanban", "show")})
        self.assertEqual(self.fake.cli_writes, [])

    def test_native_schema_keys_are_exact(self):
        self.assertEqual({k: set(v) for k, v in wf.NATIVE_SCHEMA.items()}, UPSTREAM_SCHEMA_KEYS)
        graph(self.fake)
        push(self.fake)
        for tool, args, _ in self.fake.native_calls:
            self.assertLessEqual(set(args), UPSTREAM_SCHEMA_KEYS[tool])
            self.assertFalse({"author", "created_by"} & set(args))
        with self.assertRaisesRegex(wf.WorkflowError, "non-native argument"):
            wf.Outbox().add("kanban_comment", {"task_id": "t_1", "body": "x", "author": "hermes-system"}, "forged")

    def test_readback_makes_every_op_idempotent(self):
        first = graph(self.fake)
        calls = len(self.fake.native_calls)
        with wf.transport() as box:
            again = wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "k", "--body", "full ask",
                                          "--bind", first["code"]["id"]), self.fake)
        self.assertEqual((box.ops, again["request"]["id"], len(self.fake.shows)), ([], "t_5", 5))
        push(self.fake)
        with wf.transport() as box:  # a deterministic record re-put is not re-sent
            wf.put_record(self.fake, "t_1", "code-sha", wf.latest(self.fake.shows["t_1"], "code-sha"))
            wf.link(self.fake, "t_4", "t_5")
        self.assertEqual(box.ops, [])
        self.assertEqual(len(self.fake.native_calls), calls + 1)  # only push's own comment

    def test_main_prints_ops_and_the_exact_rerun_and_binds_the_root(self):
        argv = ["intake", "--title", "Build the iPad app", "--repo", REPO_URL, "--lane", "mac", "--key", "k"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(argv, self.fake), wf.EXIT_PENDING)
        env = json.loads(out.getvalue())
        self.assertEqual((env["converged"], env["bind_root"], env["rerun"]),
                         (False, True, [sys.executable, str(SCRIPT), *argv]))
        self.assertEqual([(o["tool"], o["args"]["title"]) for o in env["ops"]],
                         [("kanban_create", "[code 1] Build the iPad app")])
        self.assertEqual(self.fake.shows, {})  # the script wrote nothing
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nw.main(argv, self.fake), 0)
        self.assertEqual(json.loads(out.getvalue())["request"]["id"], "t_4")

    def test_a_finished_command_reads_back_through_sync_and_only_then_prints(self):
        graph(self.fake)
        push(self.fake)
        for tid in ("t_2", "t_3"):
            validate(self.fake, tid, evidence("pass"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(["advance", "t_4"], self.fake), wf.EXIT_PENDING)
        env = json.loads(out.getvalue())
        self.assertNotIn('"metadata": {', out.getvalue())  # no kanban_complete metadata before convergence
        apply_ops(self.fake, env["ops"], "default")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(["advance", "t_4"], self.fake), wf.EXIT_PENDING)  # review exists: link + record
        env = json.loads(out.getvalue())
        self.assertEqual(env["rerun"][:4], [sys.executable, str(SCRIPT), "sync", "t_4"])
        self.assertEqual(env["rerun"][4], "--op")
        self.assertEqual(sorted(o["tool"] for o in env["ops"]), ["kanban_comment", "kanban_link"])
        apply_ops(self.fake, env["ops"], "default")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(env["rerun"][2:], self.fake), 0)
        self.assertEqual(json.loads(out.getvalue())["metadata"]["state"], "VALIDATED")

    def test_an_op_that_never_lands_blocks_on_the_third_emission(self):
        graph(self.fake)
        push(self.fake)
        argv = ["dispatch", "t_2"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(argv, self.fake), wf.EXIT_PENDING)
        rerun = json.loads(out.getvalue())["rerun"][2:]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(rerun, self.fake), wf.EXIT_PENDING)
        self.assertEqual(json.loads(out.getvalue())["rerun"][2:], rerun)  # the same operation, read back again
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wf.main(rerun, self.fake), 4)
        self.assertIn("native apply did not land: kanban_comment", json.loads(out.getvalue())["blocker"])
        self.assertEqual(len(self.fake.peer_calls("run")), 1)

    def test_a_wrong_or_forged_bind_is_refused(self):
        graph(self.fake)
        other = FakeHermes()
        for bind, text in (("t_2", "header workflow"), ("t_4", "header workflow"), ("t_9", "no such task")):
            with wf.transport() as box:
                args = intake_args("--lane", "mac", "--lane", "windows", "--key", "k", "--body", "full ask",
                                   "--bind", bind)
                with self.assertRaisesRegex(wf.WorkflowError, text):
                    wf.intake(args, self.fake)
            self.assertEqual(box.ops, [])
        # a card of another request (another key) is not this request's root
        with wf.transport() as box:
            with self.assertRaisesRegex(wf.Mismatch, "header key"):
                wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "other", "--body", "full ask",
                                      "--bind", "t_1"), self.fake)
        self.assertEqual(box.ops, [])
        del other

    def test_a_changed_native_body_or_creator_is_a_mismatch_never_a_second_create(self):
        graph(self.fake)
        self.fake.shows["t_3"]["task"]["body"] += "\ninjected"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = wf.main(["intake", "--title", "Build the iPad app", "--repo", REPO_URL, "--lane", "mac", "--lane",
                            "windows", "--key", "k", "--body", "full ask", "--bind", "t_1"], self.fake)
        self.assertEqual(code, 4)
        self.assertIn("board card t_3 is not planned card '[win 1] Build the iPad app'", out.getvalue())
        self.assertNotIn('"ops"', out.getvalue())
        fake = FakeHermes()
        graph(fake)
        fake.shows["t_2"]["task"]["created_by"] = "mac-worker"
        with self.assertRaisesRegex(wf.Mismatch, "created_by"):
            with wf.transport():
                wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "k", "--body", "full ask",
                                      "--bind", "t_1"), fake)
        fake = FakeHermes()
        graph(fake)
        fake.native("kanban_create", {"title": "[mac 1] Build the iPad app", "assignee": "mac-worker",
                                      "body": fake.body("t_2"), "parents": ["t_1"]}, "default")
        with self.assertRaisesRegex(wf.Mismatch, "2 board cards claim"):
            with wf.transport():
                wf.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "k", "--body", "full ask",
                                      "--bind", "t_1"), fake)

    def test_records_are_trusted_by_their_roles_runtime_author(self):
        graph(self.fake)
        rec = {"rid": f"code-sha:{SHA}", "sha": SHA, "type": "code-sha", "ts": 1.0}
        body = wf.record_body(rec)
        for author, trusted in (("default", True), ("mac-worker", False), ("reviewer", False), ("", False)):
            self.fake.shows["t_1"]["comments"] = [{"author": author, "body": body, "created_at": 5}]
            self.assertEqual(bool(wf.card_records(self.fake.shows["t_1"])), trusted, author)
        verdict = wf.record_body({"rid": f"verdict:1:{SHA}", "verdict": "approve", "commit": SHA, "type": "verdict"})
        review = {"task": {"id": "t_r", "assignee": "reviewer", "body": wf.review_body(
            wf.Ctx.from_header(wf.header_of(self.fake.shows["t_4"])), 1, "t_1", "t_4", SHA, "x")},
                  "comments": [], "parents": [], "events": [], "runs": []}
        for author, trusted in (("reviewer", True), ("default", False), ("mac-worker", False)):
            review["comments"] = [{"author": author, "body": verdict, "created_at": 5}]
            self.assertEqual(bool(wf.card_records(review)), trusted, author)
        evid = wf.record_body({"rid": "evidence:r1", "status": "pass", "sha": SHA, "type": "evidence"})
        self.fake.shows["t_2"]["comments"] = [{"author": "windows-worker", "body": evid, "created_at": 5}]
        self.assertEqual(wf.card_records(self.fake.shows["t_2"]), [])  # another lane's word is not this lane's

    def test_a_verdict_authored_by_the_coder_fails_the_gate(self):
        graph(self.fake)
        push(self.fake)
        for tid in ("t_2", "t_3"):
            validate(self.fake, tid, evidence("pass"))
        review = nw.advance("t_4", self.fake)[1]["metadata"]["next"]["review"]
        self.fake.finish("t_4")
        rec = {"rid": f"verdict:1:{SHA}", "verdict": "approve", "round": 1, "commit": SHA, "type": "verdict", "ts": 2}
        self.fake.native("kanban_comment", {"task_id": review, "body": wf.record_body(rec)}, "default")
        self.fake.finish(review, profile="reviewer")
        ok, rows = wf.gate("t_5", self.fake)
        self.assertFalse(ok)
        self.assertIn("no approve verdict", " ".join(r["reason"] for r in rows))

    def test_legacy_records_count_only_when_preserved_from_before_the_migration(self):
        legacy = {"task": {"id": "t_old", "assignee": "default", "created_by": "kanban-workflow",
                           "body": wf.code_body(wf.Ctx(key="k", repo=REPO_URL, branch="feat/x", lanes={"mac-worker": "mac"},
                                                       coder="default", reviewer="reviewer"), 1, "x", "")
                           .replace("Board-transport: native-v1\n", "")},
                  "comments": [], "parents": [], "events": [], "runs": []}
        self.assertFalse(wf.native_v1(legacy))
        body = wf.record_body({"rid": f"code-sha:{SHA}", "sha": SHA, "type": "code-sha", "ts": 1.0})
        migrated = wf.migration_at()
        legacy["comments"] = [{"author": "kanban-workflow", "body": body, "created_at": int(migrated) - 3600}]
        self.assertEqual(wf.card_records(legacy)[0]["sha"], SHA)  # preserved pre-migration record
        legacy["comments"] = [{"author": "kanban-workflow", "body": body, "created_at": int(migrated) + 1}]
        self.assertEqual(wf.card_records(legacy), [])  # a new comment with the old author
        legacy["comments"] = [{"author": "kanban-workflow", "body": body}]
        self.assertEqual(wf.card_records(legacy), [])  # no board timestamp: not provably preserved
        graph(self.fake)  # a native-v1 card never trusts the legacy author
        self.fake.shows["t_1"]["comments"] = [{"author": "kanban-workflow", "body": body, "created_at": 1}]
        self.assertEqual(wf.card_records(self.fake.shows["t_1"]), [])
        self.assertEqual(wf.migration_at(), migrated)  # written once

    def test_legacy_graph_resolves_without_duplicates(self):
        """A pre-migration graph (CLI-era bodies without the transport header, created_by
        kanban-workflow, legacy-authored records) is discovered by header identity: a rerun of
        advance resolves the existing round 2 cards instead of creating them again."""
        graph(self.fake)
        push(self.fake)
        validate(self.fake, "t_2", evidence("pass"))
        validate(self.fake, "t_3", evidence("fail"))
        nxt = nw.advance("t_4", self.fake)[1]["metadata"]["next"]
        for show in self.fake.shows.values():  # turn the whole graph into a legacy one
            show["task"]["body"] = show["task"]["body"].replace("Board-transport: native-v1\n", "")
            show["task"]["created_by"] = "kanban-workflow"
            for c in show["comments"]:
                c.update(author="kanban-workflow", created_at=1)
        count = len(self.fake.shows)
        with wf.transport() as box:
            again = wf.advance("t_4", self.fake)
        self.assertEqual((again[1]["metadata"]["next"], len(self.fake.shows)), (nxt, count))
        self.assertEqual([o["tool"] for o in box.ops], [])


def main_out(argv, fake):
    """(exit code, printed JSON or None) of one `workflow.py` invocation."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = wf.main(argv, fake)
    return code, (json.loads(buf.getvalue()) if buf.getvalue().strip() else None)


class VerdictAuthorityTest(StateDirMixin, unittest.TestCase):
    """B1: only the review card's own bound worker records a verdict, and the gate counts only a
    landed card verdict authored by the reviewer's runtime profile, never the local ledger."""

    def setUp(self):
        super().setUp()
        self.fake = FakeHermes()
        graph(self.fake)
        push(self.fake)
        for tid in ("t_2", "t_3"):
            validate(self.fake, tid, evidence("pass"))
        self.review = nw.advance("t_4", self.fake)[1]["metadata"]["next"]["review"]
        self.fake.finish("t_4")

    def gate_reason(self):
        ok, rows = wf.gate("t_5", self.fake)
        return ok, " ".join(r["reason"] for r in rows if r["id"] == self.review)

    def test_an_unbound_shell_cannot_record_a_verdict(self):
        stranger = subprocess.Popen(["sleep", "30"])  # the reviewer's worker: alive, not our ancestor
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        self.fake.claim(self.review, "reviewer", pid=stranger.pid)
        for decision, reason in (("approve", ""), ("changes", "1. forged")):
            with bound_adaptor(), wf.transport() as box:
                with self.assertRaisesRegex(wf.CapabilityError, "ancestor of this process"):
                    wf.verdict(self.review, decision, reason, self.fake)
            self.assertEqual(box.ops, [])
        with bound_adaptor():
            self.assertEqual(main_out(["verdict", self.review, "approve"], self.fake), (3, None))
        self.assertEqual(wf.read_ledger(self.review), [])
        self.fake.finish(self.review, profile="reviewer")  # the reviewer completes without its own verdict
        ok, reason = self.gate_reason()
        self.assertFalse(ok)
        self.assertIn("no approve verdict", reason)

    def test_an_unsent_or_ledger_only_approve_is_no_approval(self):
        self.fake.claim(self.review, "reviewer")
        with bound_adaptor(), wf.transport() as box:
            code, out = wf.verdict(self.review, "approve", "", self.fake)
        self.assertEqual((code, out["recorded_on_card"], [o["tool"] for o in box.ops]), (0, False, ["kanban_comment"]))
        self.assertEqual([r["verdict"] for r in wf.read_ledger(self.review)], ["approve"])
        self.fake.finish(self.review, profile="reviewer")
        ok, reason = self.gate_reason()
        self.assertFalse(ok)
        self.assertIn("no approve verdict", reason)  # ledger-only: never sent
        apply_ops(self.fake, box.ops, "reviewer")  # the legitimate reviewer's native comment lands
        ok, reason = self.gate_reason()
        self.assertTrue(ok, reason)
        self.assertEqual(reason, f"approved by reviewer at {SHA[:12]}")

    def test_a_forged_ledger_line_never_approves_or_overrides_the_card(self):
        rid = f"verdict:1:{SHA}"
        wf.append_ledger(self.review, {"rid": rid, "verdict": "approve", "round": 1, "commit": SHA,
                                       "reviewer": "reviewer", "type": "verdict", "ts": 9e9})
        self.fake.claim(self.review, "reviewer")
        self.fake.finish(self.review, profile="reviewer")
        self.assertFalse(self.gate_reason()[0])  # a same-UID shell wrote the ledger; nothing on the card
        changes = {"rid": rid, "verdict": "changes", "round": 1, "commit": SHA, "reviewer": "reviewer",
                   "reason": "1. fix", "type": "verdict", "ts": 1.0}
        self.fake.native("kanban_comment", {"task_id": self.review, "body": wf.record_body(changes)}, "reviewer")
        ok, reason = self.gate_reason()
        self.assertFalse(ok)  # the later-ts ledger approve of the same rid does not override the card
        self.assertIn("verdict: changes", reason)

    def test_the_bound_reviewer_records_and_reads_back_through_main(self):
        self.fake.claim(self.review, "reviewer")
        with bound_adaptor():
            code, env = main_out(["verdict", self.review, "approve"], self.fake)
            self.assertEqual(code, wf.EXIT_PENDING)
            apply_ops(self.fake, env["ops"], "reviewer")
            code, out = main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["metadata"]["state"]), (0, "REVIEW_APPROVED"))
        self.fake.finish(self.review)
        self.assertTrue(self.gate_reason()[0])

    def test_a_verdict_read_back_after_the_review_was_reclaimed_is_stale(self):
        self.fake.claim(self.review, "reviewer")
        with bound_adaptor():
            code, env = main_out(["verdict", self.review, "approve"], self.fake)
            apply_ops(self.fake, env["ops"], "reviewer")
            self.fake.shows[self.review]["runs"][-1].update(status="crashed", ended_at=1)
            self.fake.claim(self.review, "reviewer")  # another run of the reviewer took the card
            code, out = main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["stale"]), (4, True))
        self.assertNotIn("metadata", out)


class StaleSyncTest(GitCard, unittest.TestCase):
    """B2: `sync` re-evaluates a finished command once its ops landed; anything that moved after
    the output (source, HEAD, remote, the card's run, a gate) is stale (exit 4), never success."""

    def emit(self, push=True):
        if push:
            self.git("push", "origin", "feat/build-the-ipad-app", cwd=self.work)
        code, env = main_out(["record-sha", "t_1", "--repo-dir", self.work], self.fake)
        self.assertEqual(code, wf.EXIT_PENDING)
        apply_ops(self.fake, env["ops"], "default")
        return env

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True, text=True).stdout.strip()

    def commit(self, text):
        Path(self.work, "a.txt").write_text(text)
        self.git("commit", "-am", "feat: more", cwd=self.work)

    def assertStale(self, env):
        code, out = main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out.get("stale")), (4, True), out)
        self.assertNotIn("metadata", out)
        self.assertEqual(out["retry"][2:], ["record-sha", "t_1", "--repo-dir", self.work])
        return out

    def test_unchanged_state_certifies_after_the_recheck(self):
        env = self.emit()
        code, out = main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["metadata"]["state"], out["metadata"]["commit"]), (0, "SHA_PUSHED", self.head()))

    def test_remote_advanced_after_the_output(self):
        env = self.emit()
        self.commit("3\n")
        self.git("push", "origin", "feat/build-the-ipad-app", cwd=self.work)
        self.assertStale(env)
        env = self.emit(push=False)  # the explicit retry certifies the new SHA
        code, out = main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["metadata"]["commit"]), (0, self.head()))

    def test_head_moved_after_the_output(self):
        env = self.emit()
        self.commit("3\n")
        self.assertIn("push first", self.assertStale(env)["blocker"])

    def test_source_changed_after_the_output(self):
        env = self.emit()
        Path(self.work, "a.txt").write_text("edited\n")
        self.assertIn("uncommitted", self.assertStale(env)["blocker"])

    def test_card_reclaimed_after_the_output(self):
        self.fake.claim("t_1", "default")
        env = self.emit()
        self.fake.shows["t_1"]["runs"][-1].update(status="crashed", ended_at=1)
        self.fake.claim("t_1", "default")
        self.assertIn("reclaimed", self.assertStale(env)["blocker"])

    def test_a_cleared_superseded_or_unknown_operation_never_reports_success(self):
        env = self.emit()
        self.assertEqual(main_out(["status", "t_1", "--json"], self.fake)[0], 0)  # converges on t_1: clears the counts
        code, out = main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["metadata"]["commit"]), (0, self.head()))  # still re-evaluated, not dropped
        for argv in (env["rerun"][2:], ["sync", "t_1"], ["sync", "t_1", "--op", "0123456789abcdef"]):
            code, out = main_out(argv, self.fake)  # consumed, missing or never issued
            self.assertEqual((code, out["stale"]), (4, True), argv)
        self.commit("3\n")
        first = self.emit()
        x = self.head()
        self.commit("4\n")
        second = self.emit()  # a second command on the same card, a newer SHA
        self.assertNotEqual(first["rerun"], second["rerun"])
        self.assertStale(first)  # never the second command's result as the first's
        code, out = main_out(second["rerun"][2:], self.fake)
        self.assertEqual((code, out["metadata"]["commit"]), (0, self.head()))
        self.assertNotEqual(x, self.head())

    def test_a_gate_that_moved_after_advance_is_stale(self):
        fake = self.fake = FakeHermes()
        graph(fake)
        push(fake)
        for tid in ("t_2", "t_3"):
            validate(fake, tid, evidence("pass"))
        code, env = main_out(["advance", "t_4"], fake)
        apply_ops(fake, env["ops"], "default")  # the review card
        code, env = main_out(["advance", "t_4"], fake)
        self.assertEqual(code, wf.EXIT_PENDING)
        apply_ops(fake, env["ops"], "default")
        push(fake, sha=SHA2)  # the code card's recorded SHA changed: no lane evidence for it
        code, out = main_out(env["rerun"][2:], fake)
        self.assertEqual((code, out["stale"]), (4, True))
        self.assertNotIn("VALIDATED", json.dumps(out.get("metadata")))


class GoldenGraphTest(StateDirMixin, unittest.TestCase):
    """The native transport builds exactly the graph the CLI-era transport built (golden file
    `kanban_golden_graph.json`, captured from workflow.py before the migration with its own
    FakeHermes): card ids, titles, assignees, parents, header identity and record type + rid,
    for a fail -> fix -> changes -> approve -> gate loop, the fix-loop limit and the mobile +
    BrowserStack device stage."""

    GOLDEN = json.loads(Path(__file__).with_name("kanban_golden_graph.json").read_text())

    def graph_of(self, fake):
        out = []
        for tid in sorted(fake.shows, key=lambda t: int(t.split("_")[1])):
            show = card(fake, tid)
            h = wf.header_of(show)
            recs = [[rtype, r.get("rid")] for rtype in wf.RECORD_TYPES if rtype != wf.cp.RTYPE
                    for r in wf.records(show, rtype) if not (rtype == "peer-run" and not r.get("run_id"))]
            out.append({"id": tid, "title": show["task"]["title"], "assignee": show["task"]["assignee"],
                        "parents": sorted(show.get("parents") or []),
                        "header": {k: h.get(k, "") for k in ("workflow", "key", "round", "lane", "stage", "code",
                                                             "commit", "round-card")},
                        "records": sorted(recs)})
        return out

    def fresh_state(self):
        os.environ["HERMES_WORKFLOW_STATE"] = tempfile.mkdtemp(dir=self._state)

    def review(self, fake, review, decision, reason=""):
        fake.claim(review, "reviewer")
        out = nw.verdict(review, decision, reason, fake)[1]
        fake.finish(review, profile="reviewer")
        return out

    def test_fix_changes_approve_loop(self):
        fake = FakeHermes()
        nw.intake(intake_args("--lane", "mac", "--lane", "windows", "--key", "g", "--body", "ask"), fake)
        push(fake, "t_1", SHA, notes="n")
        validate(fake, "t_2", evidence("pass"))
        validate(fake, "t_3", evidence("fail"))
        n2 = nw.advance("t_4", fake)[1]["metadata"]["next"]
        fake.finish("t_4")
        push(fake, n2["code"], SHA2, notes="n")
        for tid in n2["lanes"].values():
            validate(fake, tid, evidence("pass", sha=SHA2))
        review = nw.advance(n2["round_card"], fake)[1]["metadata"]["next"]["review"]
        fake.finish(n2["round_card"])
        n3 = self.review(fake, review, "changes", "1. add a test")["metadata"]["next"]
        push(fake, n3["code"], "c" * 40, notes="n")
        for tid in n3["lanes"].values():
            validate(fake, tid, evidence("pass", sha="c" * 40))
        review3 = nw.advance(n3["round_card"], fake)[1]["metadata"]["next"]["review"]
        fake.finish(n3["round_card"])
        self.review(fake, review3, "approve")
        self.assertEqual({"graph": self.graph_of(fake), "gate": wf.gate("t_5", fake)[0]}, self.GOLDEN["loop"])

    def test_fix_loop_limit(self):
        self.fresh_state()
        fake = FakeHermes()
        nw.intake(intake_args("--lane", "mac", "--key", "lim", "--max-rounds", "1"), fake)
        push(fake, "t_1", SHA, notes="n")
        validate(fake, "t_2", evidence("fail"))
        code, out = nw.advance("t_3", fake)
        self.assertEqual({"graph": self.graph_of(fake), "code": code, "blocker": out.get("blocker")}, self.GOLDEN["limit"])

    def test_mobile_and_browserstack_stage(self):
        self.fresh_state()
        bs_activate(os.environ["KANBAN_BROWSERSTACK_CONFIG"])
        with mock.patch.object(wf, "BROWSERSTACK_HTTP", FakeBrowserStack({BUILD_ID: api_build()})):
            fake = FakeHermes()
            nw.intake(intake_args("--lane", "mac", "--mobile", "ios", "--key", "m", "--body", "iPad"), fake)
            push(fake, "t_1", SHA, notes="n")
            validate(fake, "t_2", mobile_output())
            nxt = nw.advance("t_3", fake)[1]["metadata"]["next"]
            fake.finish("t_3")
            validate(fake, nxt["devices"], device_output(task=nxt["devices"]))
            review = nw.advance(nxt["devices_round"], fake)[1]["metadata"]["next"]["review"]
            fake.finish(nxt["devices_round"])
            self.review(fake, review, "approve")
            self.assertEqual({"graph": self.graph_of(fake), "gate": wf.gate("t_4", fake)[0]}, self.GOLDEN["mobile"])


class WorkerPolicyTextTest(unittest.TestCase):
    """The coder SOUL and the generated code card agree: verification is fixed until it passes
    within budget (an unchanged command is retried once, for a transient failure only), pushing
    the card's own branch is authorized, merges/deploys/secrets are not, and a pre-created
    review or validation child is released by completing, never by a second review request."""

    def test_coder_soul_has_no_one_retry_or_no_network_rule(self):
        soul = (REPO / "hermes" / "profiles" / "coder" / "SOUL.md").read_text()
        self.assertNotIn("One bounded retry", soul)
        self.assertNotIn("No network calls that change external state", soul)
        self.assertIn("until it passes or the task's budget", soul)
        self.assertIn("Retry an unchanged command only once, and only for a transient failure", soul)
        self.assertIn("`git push origin <feature branch>` of your commits is authorized", soul)
        self.assertIn("Never merge, deploy, publish, release, rotate or read secrets", soul)
        self.assertIn("A card that already has its review or validation child does not need `kanban_request_review`", soul)
        self.assertIn("no `Co-Authored-By` trailer", soul)

    def test_code_card_text_matches(self):
        ctx = wf.Ctx(key="k", title="T", repo=REPO_URL, branch="feat/x", base="main", lanes={}, coder="coder",
                     reviewer="reviewer", orchestrator="orchestrator", checkout="/opt/repos/app")
        text = wf.code_body(ctx, 1, "the ask", "")
        self.assertIn("diagnosed and fixed until it passes within this card's budget", text)
        self.assertIn("retry an unchanged\n   command once only for a transient failure", text)
        self.assertIn("git push origin feat/x. Never force push.", text)
        self.assertIn("known\n   editors, language servers, dev servers, idle shells and read-only git are not writers", text)
        self.assertIn("any other program or script", text)
        self.assertIn("records only what changed since that passing preflight, under the same lease", text)
        self.assertIn("never adopted\n   this way", text)
        self.assertIn("After a commit, run preflight again before the next checkpoint", text)

    def test_code_card_carries_the_scanner_recovery_policy(self):
        ctx = wf.Ctx(key="k", title="T", repo=REPO_URL, branch="feat/x", base="main", lanes={}, coder="coder",
                     reviewer="reviewer", orchestrator="orchestrator", checkout="/opt/repos/app")
        text = wf.code_body(ctx, 1, "the ask", "")
        self.assertIn("run only through the\n   dev_test_run tool", text)
        self.assertIn("never a terminal command carrying a\n   database URL or a credential file", text)
        self.assertIn("7. A command the security scanner rejects is not a blocker", text)
        self.assertIn("make that call once as a new request, verify its result and\n   continue", text)
        self.assertIn("Never retry the rejected command, wrap it in a script, encode it", text)
        self.assertIn('kanban_block(reason="capability: <the exact command>; <the scanner finding>; missing <the '
                      'capability>")', text)
        self.assertNotIn("single_query_mode: approve", text)
        skill = (REPO / "hermes" / "skills" / "kanban-workflow" / "SKILL.md").read_text()
        self.assertIn("call the `dev_test_run` tool", skill)
        self.assertIn("`kanban_block` refuses until you tried it", skill)
        self.assertNotIn("PGPASSFILE=<path> MATCHING_TEST_DATABASE_URL", skill)
        for soul in ("hermes/SOUL.md", "hermes/profiles/coder/SOUL.md"):
            self.assertIn("dev_test_run", (REPO / soul).read_text(), soul)


class DesignatedCheckoutLeaseTest(CheckoutRepo, unittest.TestCase):
    """preflight / checkpoint / release on a --checkout code card, and record-sha's
    revalidation (lease, writers, clean tree incl. untracked) right before it records."""

    def setUp(self):
        super().setUp()
        self.intake("--checkout", self.co)
        self.branch = "feat/build-the-ipad-app"
        self.git("switch", "-c", self.branch, cwd=self.co)
        self.git("push", "-u", "origin", self.branch, cwd=self.co)

    def run_cmd(self, cmd, task="t_1", repo_dir=None, **kw):
        return nw.checkout_command(cmd, task, repo_dir or self.co, run=self.fake, scan_runner=passing_scan, **kw)

    def test_preflight_checkpoint_release_round_trip(self):
        code, result = self.run_cmd("preflight")
        self.assertEqual((code, result["ok"], result["lease"]["status"]), (0, True, "acquired"), result["checks"])
        self.assertEqual(result["lease"]["owner"]["task"], "t_1")
        Path(self.co, "a.txt").write_text("edited by the subagent\n")
        Path(self.co, "new.swift").write_text("let x = 1\n")
        code, result = self.run_cmd("preflight")
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertIn("unknown dirty paths", next(c["detail"] for c in result["checks"] if c["check"] == "dirty"))
        code, manifest = self.run_cmd("checkpoint")
        self.assertEqual((code, sorted(manifest["entries"])), (0, ["a.txt", "new.swift"]))
        code, result = self.run_cmd("preflight")
        self.assertEqual((code, result["lease"]["status"]), (0, "renewed"))
        self.assertIn("2 task-owned path(s) resume", next(c["detail"] for c in result["checks"] if c["check"] == "dirty"))
        self.assertEqual(Path(self.co, "a.txt").read_text(), "edited by the subagent\n")
        code, result = self.run_cmd("release")
        self.assertEqual((code, result["status"]), (0, "released"))
        with self.assertRaises(wf.WorkflowError):  # another code card of the same request must say so
            self.run_cmd("checkpoint", task="t_4")
        with self.assertRaises(wf.WorkflowError) as ctx:
            self.run_cmd("preflight", repo_dir=self.tmp)
        self.assertIn("designated checkout", str(ctx.exception))

    def test_without_a_task_scoped_worker_identity_every_repo_step_fails_closed(self):
        """Forged or ambiguous bindings: the repository steps stop with exit 3 (capability) and
        take no lease; the native board transport keeps working (advance, status)."""
        show = self.fake.shows["t_1"]
        good = json.loads(json.dumps(show))
        bound = lambda: next(e for e in show["events"] if e["kind"] == "worker_bound")["payload"]  # noqa: E731
        sleeper = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))

        def not_running():
            show["task"]["status"] = "ready"

        def wrong_run():
            next(e for e in show["events"] if e["kind"] == "worker_bound")["run_id"] = 99

        def not_ancestor():
            bound().update(pid=sleeper.pid, started_at=f"|{wf.rl.proc_start_ticks(sleeper.pid)}")

        def ticks():
            bound()["started_at"] = "|1"

        def unverified():
            bound()["started_at"] = "unverified"

        def two_runs():
            show["runs"].append({"id": 7, "profile": "default", "status": "running", "ended_at": None})

        def owner_hosted():
            show["events"].append({"kind": "owner_admitted", "run_id": show["runs"][-1]["id"],
                                   "payload": {"pid": os.getppid()}})

        def sibling():
            self.fake.claim("t_4")  # another running card of the request bound to the same process

        cases = {not_running: "is ready, not running", wrong_run: "no worker_bound event",
                 not_ancestor: "is an ancestor of this process", ticks: "another start time",
                 unverified: "no verifiable fingerprint", two_runs: "2 running runs",
                 owner_hosted: "owner process that admitted it", sibling: "bound to this process chain too"}
        for forge, text in cases.items():
            self.fake.shows["t_1"] = show = json.loads(json.dumps(good))
            self.fake.shows["t_4"]["task"]["status"], self.fake.shows["t_4"]["runs"] = "todo", []
            forge()
            for cmd in ("preflight", "checkpoint", "release"):
                with self.assertRaises(wf.CapabilityError, msg=(forge.__name__, cmd)) as ctx:
                    self.run_cmd(cmd)
                self.assertIn("capability: task-scoped worker identity", str(ctx.exception))
                self.assertIn(text, str(ctx.exception), forge.__name__)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(nw.main(["preflight", "t_1", "--repo-dir", self.co], self.fake), 3)
        with mock.patch.object(wf.rl, "is_managed_worker", lambda cmd: False):  # a shared owner, not one card's
            self.fake.shows["t_1"] = json.loads(json.dumps(good))
            with self.assertRaisesRegex(wf.CapabilityError, "not a per-attempt managed worker"):
                self.run_cmd("preflight")
        self.assertIsNone(wf.rl.read_lease(wf.rl.repo_identity(self.co, wf.rl.default_git)), "no lease was taken")
        self.assertEqual(wf.status("t_1", self.fake)[0]["id"], "t_1")  # reads and the transport still work

    def test_adopt_bootstraps_only_matching_provenance(self):
        Path(self.co, "a.txt").write_text("preserved work\n")
        src = Path(self.tmp, "preserved.json")
        src.write_text(json.dumps({"a.txt": wf.rl.hash_worktree_path(self.co, "a.txt")}))
        code, adopted = self.run_cmd("adopt", manifest=str(src))
        self.assertEqual((code, list(adopted["entries"])), (0, ["a.txt"]))
        self.assertTrue(self.run_cmd("preflight")[1]["ok"])
        Path(self.co, "a.txt").write_text("someone else edited\n")
        self.assertEqual(self.run_cmd("preflight")[0], 2)

    def test_record_sha_rejects_untracked_and_revalidates_the_lease(self):
        Path(self.co, "scratch.log").write_text("x")
        with self.assertRaises(wf.WorkflowError) as ctx:
            nw.record_sha("t_1", self.co, "notes", self.fake, scan_runner=passing_scan)
        self.assertIn("untracked", str(ctx.exception))
        self.assertIn("scratch.log", str(ctx.exception))
        Path(self.co, "scratch.log").unlink()
        Path(self.co, "a.txt").write_text("done\n")
        self.git("commit", "-am", "feat: done", cwd=self.co)
        self.git("push", "origin", self.branch, cwd=self.co)
        sleeper = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        ident = wf.rl.repo_identity(self.co, wf.rl.default_git)
        holder = {"board": "default", "task": "t_other", "run_id": "1", "pid": sleeper.pid,
                  "pid_start": wf.rl.proc_start_ticks(sleeper.pid), "host": wf.rl.socket.gethostname()}
        wf.rl.acquire_lease(ident, holder)
        with self.assertRaises(wf.WorkflowError) as ctx:
            nw.record_sha("t_1", self.co, "notes", self.fake, scan_runner=passing_scan)
        self.assertIn("lease", str(ctx.exception))
        self.assertEqual(wf.records(self.fake.shows["t_1"], "code-sha"), [])
        wf.rl.release_lease(ident, holder)
        foreign = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, cwd=self.co)
        self.addCleanup(lambda: (foreign.kill(), foreign.wait()))
        bindir = Path(self.tmp, "bin"); bindir.mkdir()
        (bindir / "aider").symlink_to(shutil.which("sleep"))
        agent = subprocess.Popen([str(bindir / "aider"), "60"], stdin=subprocess.DEVNULL, cwd=self.co)
        self.addCleanup(lambda: (agent.kill(), agent.wait()))
        hits = lambda argv: done(1, f"MATCH: PID={agent.pid} CWD={self.co}\nBLOCKED: 1 matching processes.\n")  # noqa: E731
        real = wf.rl.ancestors
        with mock.patch.object(wf.rl, "ancestors",  # the agent is nobody's child here
                               lambda pid, limit=64: [] if pid == agent.pid else real(pid, limit)):
            with self.assertRaises(wf.WorkflowError) as ctx:
                nw.record_sha("t_1", self.co, "notes", self.fake, scan_runner=hits)
        self.assertIn("agent-writer", str(ctx.exception))
        result = nw.record_sha("t_1", self.co, "notes", self.fake, scan_runner=passing_scan)
        self.assertEqual(result["metadata"]["state"], "SHA_PUSHED")
        self.assertEqual(result["metadata"]["commit"], self.git("rev-parse", "HEAD", cwd=self.co).stdout.decode().strip())

    def test_cli_subcommands(self):
        with mock.patch.object(wf.rl, "default_scan_runner", passing_scan):
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(nw.main(["preflight", "t_1", "--repo-dir", self.co], self.fake), 0)
            self.assertTrue(json.loads(buf.getvalue())["ok"])
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(nw.main(["checkpoint", "t_1", "--repo-dir", self.co], self.fake), 0)
            self.assertEqual(json.loads(buf.getvalue())["entries"], {})
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(nw.main(["release", "t_1", "--repo-dir", self.co], self.fake), 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "released")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(nw.main(["preflight", "t_2", "--repo-dir", self.co], self.fake), 2)
            self.assertIn("not a code card", err.getvalue())
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_RUN_ID": "1"}):
                self.fake.finish("t_1")  # the env claims the card; the board says its run ended
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(nw.main(["preflight", "t_1", "--repo-dir", self.co], self.fake), 3)
                self.assertIn("capability: task-scoped worker identity", err.getvalue())


class OwnGitFsmonitorTest(unittest.TestCase):
    def test_workflow_git_reads_never_run_a_repository_fsmonitor_hook(self):
        tmp = Path(tempfile.mkdtemp(prefix="kanban-workflow-fsmon-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        co, sentinel, hook = tmp / "co", tmp / "fsmonitor-ran", tmp / "hook"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n"); hook.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(co)], check=True)
        subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=co, check=True)
        subprocess.run(["git", "status", "--porcelain"], cwd=co, capture_output=True)
        self.assertTrue(sentinel.exists(), "the fixture hook really runs under plain git")
        sentinel.unlink()
        self.assertEqual(wf.default_git(["status", "--porcelain"], str(co)).returncode, 0)
        self.assertFalse(sentinel.exists(), "workflow git read ran the fsmonitor hook")

    def test_workflow_git_status_keeps_repository_filters_with_and_without_the_policy(self):
        tmp = Path(tempfile.mkdtemp(prefix="kanban-workflow-filter-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        co, sentinel = tmp / "co", tmp / "filter-ran"
        subprocess.run(["git", "init", "-q", str(co)], check=True)
        subprocess.run(["git", "config", "filter.x.clean", f"sh -c 'touch {sentinel}; cat'"], cwd=co, check=True)
        Path(co, ".gitattributes").write_text("* filter=x\n")
        Path(co, "a.txt").write_text("1\n")
        subprocess.run(["git", "add", "-f", "a.txt"], cwd=co, check=True,
                       env={**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.x.clean",
                            "GIT_CONFIG_VALUE_0": "cat"})
        Path(co, "a.txt").write_text("2\n")
        subprocess.run(["git", "status", "--porcelain"], cwd=co, capture_output=True)
        self.assertTrue(sentinel.exists(), "the fixture filter really runs under plain git")
        sentinel.unlink()
        # No observer grant: git applies the repository's filters as before PR28.
        state = tmp / "state"
        state.mkdir()
        with mock.patch.dict(os.environ, {"HERMES_WORKFLOW_STATE": str(state)}):
            r = wf.default_git(["status", "--porcelain", "--untracked-files=all"], str(co))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(sentinel.exists(), "no grant: workflow git status keeps pre-PR28 filter behaviour")
            sentinel.unlink()
            # Host-wide observer policy: ordinary repository filtering is preserved.
            wf.rl.observer_policy_command("enable", str(tmp / ".vscode-server"),
                                          "I accept my own VS Code sessions on this host in any repository.")
            r = wf.default_git(["status", "--porcelain", "--untracked-files=all"], str(co))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(sentinel.exists(), "host-wide policy: workflow git status keeps filter behaviour")
