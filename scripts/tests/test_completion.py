"""Tests for the coordinator completion policy (hermes/skills/kanban-workflow/scripts/completion.py
and `workflow.py completion ...`).

Synthetic fixtures only: the board is test_kanban_workflow's FakeHermes (no `hermes` CLI, no
live board), the remote is a local bare git repository (file://), the state dir is a temp
directory, and the registered scanner is replaced by a passing fake scan. Nothing touches
~/.hermes, the live runtime or a network.

Run: python3 -m unittest discover -s scripts/tests -p 'test_completion.py'
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_kanban_workflow as tk  # noqa: E402

wf = tk.wf
nw = tk.nw
cp = wf.cp
rl = wf.rl
BRANCH = "feat/build-the-ipad-app"
SCAN = tk.passing_scan


def head(path: str) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()


class Args:
    """argparse-shaped arguments for completion_write."""

    def __init__(self, task_id="t_1", **kw):
        self.task_id = task_id
        self.repo_dir = kw.pop("repo_dir", "")
        defaults = {"item": [], "check": "", "source": [], "step": [], "id": "", "state": "", "summary": "",
                    "remaining": "", "kind": "", "result": "", "command": "", "exit": None, "passed": None,
                    "failed": None, "skipped": None, "log": "", "stage": "", "signature": "", "diagnosis": "",
                    "action": "", "area": "", "reason": "", "stop": ""}
        for k, v in {**defaults, **kw}.items():
            setattr(self, k.rstrip("_"), v)


class Coordinator(tk.CheckoutRepo):
    """A --checkout request (synthetic board + bare remote) whose code card t_1 holds this run's lease."""

    def setUp(self):
        super().setUp()
        self.intake("--checkout", self.co)
        self.git("checkout", "-b", BRANCH, cwd=self.co)
        code, pre = nw.checkout_command("preflight", "t_1", self.co, run=self.fake, scan_runner=SCAN)
        self.assertEqual(code, 0, pre)

    def w(self, cmd, task_id="t_1", **kw):
        if self.fake.refuse_native:  # the card copy never lands; the ledger keeps the record
            with wf.transport() as box:
                out = wf.completion_write(cmd, Args(task_id, **kw), self.fake)
                tk.apply_ops(self.fake, box.ops, "default")
            return out
        return nw.completion_write(cmd, Args(task_id, **kw), self.fake)

    def owner(self):
        """This run's lease owner: the code card's board-bound worker (the lease adaptor)."""
        return rl.bound_worker("t_1", wf.card(self.fake, "t_1"))

    def ev(self, result="pass", check="unit", kind="test", exit_code=None, **kw):
        exit_code = (0 if result == "pass" else 1) if exit_code is None else exit_code
        return self.w("evidence", repo_dir=self.co, kind=kind, check=check, result=result,
                      command="python3 -m unittest" if kind == "test" else "", exit=exit_code if kind == "test" else None,
                      **kw)

    def view(self, repo_dir=None, task_id="t_1"):
        return wf.completion_view(self.fake, task_id, self.co if repo_dir is None else repo_dir)

    def stage(self, name, view=None):
        return next(r for r in (view or self.view())["stages"] if r["stage"] == name)

    def checkpoint(self):
        code, out = nw.checkout_command("checkpoint", "t_1", self.co, run=self.fake, scan_runner=SCAN)
        self.assertEqual(code, 0, out)

    def understand_and_plan(self):
        self.w("criteria", item=["login screen rotates on iPad"], check=["unit", "lint"], source=["card body"])
        self.w("plan", step=["fix rotation handler", "add a unit test"])

    def implement(self, text="2\n"):
        self.w("delegate", id="dt_1", state="started")
        Path(self.co, "a.txt").write_text(text)
        self.checkpoint()
        self.w("delegate", id="dt_1", state="complete", summary="changed a.txt")

    def commit_and_push(self):
        self.git("commit", "-am", "fix: rotation", cwd=self.co)
        code, pre = nw.checkout_command("preflight", "t_1", self.co, run=self.fake, scan_runner=SCAN)
        self.assertEqual(code, 0, pre)
        self.git("push", "origin", BRANCH, cwd=self.co)
        out = nw.record_sha("t_1", self.co, "rotation fix", self.fake, scan_runner=SCAN)
        self.assertEqual(out["metadata"]["commit"], head(self.co))


class LifecycleTest(Coordinator, unittest.TestCase):
    def test_fail_diagnose_fix_evidence_review_push_validate_complete(self):
        v = self.view()
        self.assertEqual(v["next"][0]["stage"], "understand")
        self.assertFalse(v["handoff_ready"])
        self.understand_and_plan()
        self.assertEqual(self.view()["next"][0]["stage"], "implement")
        self.implement(text="broken\n")
        # Test fails: routine, recovered, not blocked.
        self.ev("fail", check="unit", failed=2)
        self.assertIn("fails on the current source", " ".join(self.stage("test")["unmet"]))
        code, out = self.w("recover", stage="test", class_="routine", signature="unittest: test_rotation AssertionError",
                           diagnosis="handler ignores iPad size class", action="read size class in handler", area="code")
        self.assertEqual((code, out["record"]["decision"]), (0, "proceed"))
        Path(self.co, "a.txt").write_text("2\n")
        self.checkpoint()
        self.ev("pass", check="unit", passed=3)
        self.ev("pass", check="lint")
        self.assertTrue(self.stage("test")["met"])
        self.assertEqual(self.view()["next"][0]["stage"], "review")
        self.ev("pass", kind="review", check="diff", summary="full diff read")
        v = self.view()
        self.assertEqual(v["next"][0]["stage"], "commit")
        self.assertIn("1 uncommitted", v["next"][0]["unmet"][0])
        # Handoff refuses before the push; it records the resume point.
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 4)
        self.assertFalse(out["handoff"])
        self.commit_and_push()
        sha = head(self.co)
        v = self.view()
        self.assertTrue(v["handoff_ready"], v["next"])
        self.assertTrue(self.stage("test", v)["met"], "a commit of the same bytes keeps the evidence valid")
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["metadata"]["state"], "IMPLEMENTATION_HANDOFF")
        self.assertFalse(out["metadata"]["final"])
        self.assertEqual(out["metadata"]["remaining"], ["validate", "complete"])
        self.fake.finish("t_1")
        # Implementation handoff is not completion.
        v = wf.completion_view(self.fake, "t_1")
        self.assertEqual(v["source_basis"], "handoff")
        self.assertTrue(v["handoff_ready"])
        self.assertFalse(v["complete"])
        self.assertEqual(v["next"][0]["stage"], "validate")
        # Synthetic lanes validate the exact remote SHA, then the independent review approves it.
        for tid in ("t_2", "t_3"):
            self.assertEqual(tk.validate(self.fake, tid, tk.evidence("pass", sha=sha))[0], 0)
        self.assertEqual(nw.advance("t_4", self.fake)[0], 0)
        self.fake.finish("t_4")
        (review,) = self.fake.by_title("Review: ")
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "reviewer"}):
            self.assertEqual(nw.verdict(review, "approve", "", self.fake)[0], 0)
        self.fake.finish(review, profile="reviewer")
        v = wf.completion_view(self.fake, "t_5")
        self.assertTrue(self.stage("validate", v)["met"])
        code, out = nw.completion_final("t_5", self.fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["commit"], sha)
        self.assertTrue(wf.completion_view(self.fake, "t_5")["complete"])
        # Metrics come from the ledger only, with denominators.
        m = wf.completion_metrics()
        self.assertEqual(m["requests_tracked"], 1)
        self.assertEqual((m["completed"]["count"], m["completed"]["of"]), (1, 1))
        self.assertEqual((m["recovery_after_failure"]["count"], m["recovery_after_failure"]["of"]), (1, 1))
        self.assertEqual(m["exact_sha_validation_handoffs"]["count"], 1)
        self.assertEqual(m["pushes_recorded"], 1)
        self.assertEqual(m["recovery_attempts_by_area"], {"code": 1})
        self.assertIn("unknown", m["time_implementation_vs_infrastructure"])
        self.assertEqual(m["completed_without_recorded_block"]["count"], 1)
        # The engine never unblocks, promotes or edits board state beyond comments it owns.
        verbs = {tuple(c[:2]) for c in self.fake.calls if c[0] == "kanban"}
        self.assertFalse({("kanban", "unblock"), ("kanban", "promote"), ("kanban", "block")} & verbs)

    def test_final_refuses_without_a_remote_verified_handoff(self):
        self.understand_and_plan()
        self.implement()
        self.ev("pass")
        self.ev("pass", check="lint")
        self.ev("pass", kind="review", check="diff")
        self.commit_and_push()
        sha = head(self.co)
        self.fake.finish("t_1")  # completed without `completion handoff`
        for tid in ("t_2", "t_3"):
            tk.validate(self.fake, tid, tk.evidence("pass", sha=sha))
        nw.advance("t_4", self.fake)
        self.fake.finish("t_4")
        (review,) = self.fake.by_title("Review: ")
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "reviewer"}):
            nw.verdict(review, "approve", "", self.fake)
        self.fake.finish(review, profile="reviewer")
        code, out = nw.completion_final("t_5", self.fake)
        self.assertEqual(code, 4)
        self.assertIn("no remote-verified implementation handoff", out["problems"][0])


class SourceStateTest(Coordinator, unittest.TestCase):
    def ready(self):
        self.understand_and_plan()
        self.implement()
        self.ev("pass")
        self.ev("pass", check="lint")
        self.ev("pass", kind="review", check="diff")

    def test_edit_after_evidence_makes_it_stale_and_reverting_restores_it(self):
        self.ready()
        before = self.view()["source"]["content_id"]
        Path(self.co, "a.txt").write_text("3\n")
        v = self.view()
        self.assertNotEqual(v["source"]["content_id"], before)
        self.assertFalse(self.stage("test", v)["met"])
        self.assertIn("stale", " ".join(self.stage("test", v)["unmet"]))
        self.assertFalse(self.stage("review", v)["met"])
        Path(self.co, "a.txt").write_text("2\n")
        self.assertTrue(self.stage("test")["met"])

    def test_identity_covers_untracked_mode_and_symlink_not_ignored_files_or_contents(self):
        ident = lambda: cp.source_identity(self.co, rl.default_git)  # noqa: E731
        base = ident()
        Path(self.co, ".gitignore").write_text("build/\n")
        with_ignore = ident()
        self.assertNotEqual(base["content_id"], with_ignore["content_id"])
        Path(self.co, "build").mkdir()
        Path(self.co, "build", "out.bin").write_text("generated")
        self.assertEqual(ident()["content_id"], with_ignore["content_id"], "ignored files are outside the identity")
        Path(self.co, "new.txt").write_text("SECRET_CANARY_VALUE")
        untracked = ident()
        self.assertNotEqual(untracked["content_id"], with_ignore["content_id"])
        os.chmod(Path(self.co, "a.txt"), 0o755)
        moded = ident()
        self.assertNotEqual(moded["content_id"], untracked["content_id"])
        os.symlink("a.txt", Path(self.co, "link"))
        linked = ident()
        os.unlink(Path(self.co, "link"))
        os.symlink("new.txt", Path(self.co, "link"))
        self.assertNotEqual(ident()["content_id"], linked["content_id"])
        outside = Path(tempfile.mkdtemp(prefix="completion-outside-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(outside)]))
        (outside / "f").write_text("outside")
        self.assertEqual(cp._path_digest(os.path.realpath(self.co), "escape/f")[0], "000000")
        os.symlink(outside, Path(self.co, "escape"))
        self.assertEqual(cp._path_digest(os.path.realpath(self.co), "escape/f")[0], "outside")
        os.unlink(Path(self.co, "escape"))
        self.checkpoint()
        rec = self.ev("pass")[1]["record"]
        self.assertNotIn("SECRET_CANARY_VALUE", json.dumps(rec))
        self.assertNotIn("new.txt", json.dumps(rec["source"]))
        ledger = wf.ledger_path("t_1").read_text()
        self.assertNotIn("SECRET_CANARY_VALUE", ledger)

    def test_handoff_needs_the_remote_at_the_exact_sha_not_the_local_claim(self):
        self.ready()
        self.commit_and_push()
        other = os.path.join(self.tmp, "other")
        self.git("clone", "-b", BRANCH, self.url, other)
        for k, v in (("user.email", "o@example.com"), ("user.name", "o"), ("commit.gpgsign", "false")):
            self.git("config", k, v, cwd=other)
        Path(other, "b.txt").write_text("x\n")
        self.git("add", "b.txt", cwd=other)
        self.git("commit", "-m", "chore: elsewhere", cwd=other)
        self.git("push", "origin", BRANCH, cwd=other)
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 4)
        self.assertIn("the remote branch is at", json.dumps(out["next"]))

    def flagged_change_never_hands_off(self, flag):
        self.ready()
        self.commit_and_push()
        self.git("update-index", flag, "a.txt", cwd=self.co)
        Path(self.co, "a.txt").write_text(f"hidden from status by {flag}\n")
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=self.co, capture_output=True,
                                        text=True).stdout, "", "git status cannot see the change")
        self.ev("pass")
        self.ev("pass", check="lint")
        self.ev("pass", kind="review", check="diff")
        v = self.view()
        self.assertFalse(self.stage("commit", v)["met"])
        self.assertIn("assume-unchanged or skip-worktree", self.stage("commit", v)["unmet"][0])
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 4, out)
        self.assertEqual(out["next"][0]["stage"], "commit")
        # Clearing the flag shows the change; restoring HEAD's bytes makes the commit valid again.
        self.git("update-index", flag.replace("--", "--no-"), "a.txt", cwd=self.co)
        Path(self.co, "a.txt").write_text("2\n")
        self.assertTrue(self.stage("commit")["met"])

    def test_assume_unchanged_bytes_never_hand_off(self):
        self.flagged_change_never_hands_off("--assume-unchanged")

    def test_skip_worktree_bytes_never_hand_off(self):
        self.flagged_change_never_hands_off("--skip-worktree")

    def test_a_commit_of_the_same_bytes_keeps_the_evidence_valid_for_handoff(self):
        self.ready()
        self.commit_and_push()
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 0, out)

    def test_handoff_refuses_a_stale_review(self):
        self.understand_and_plan()
        self.implement()
        self.ev("pass", kind="review", check="diff")
        Path(self.co, "a.txt").write_text("4\n")
        self.checkpoint()
        self.ev("pass")
        self.ev("pass", check="lint")
        self.commit_and_push()
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 4)
        self.assertEqual(out["next"][0]["stage"], "review")
        self.assertIn("stale", out["next"][0]["unmet"][0])


class HandoffSyncTest(Coordinator, unittest.TestCase):
    """B2: a handoff read back by `sync` is re-evaluated on the source and the remote first."""

    def emit(self):
        self.understand_and_plan()
        self.implement()
        self.ev("pass")
        self.ev("pass", check="lint")
        self.ev("pass", kind="review", check="diff")
        self.commit_and_push()
        code, env = tk.main_out(["completion", "handoff", "t_1", "--repo-dir", self.co], self.fake)
        self.assertEqual(code, wf.EXIT_PENDING)
        tk.apply_ops(self.fake, env["ops"], "default")
        return env

    def test_unchanged_handoff_certifies(self):
        env = self.emit()
        code, out = tk.main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["metadata"]["state"], out["metadata"]["commit"]),
                         (0, "IMPLEMENTATION_HANDOFF", head(self.co)))

    def test_remote_advanced_after_the_handoff_output(self):
        env = self.emit()
        other = os.path.join(self.tmp, "other")
        self.git("clone", "-b", BRANCH, self.url, other)
        for k, v in (("user.email", "o@example.com"), ("user.name", "o"), ("commit.gpgsign", "false")):
            self.git("config", k, v, cwd=other)
        Path(other, "b.txt").write_text("x\n")
        self.git("add", "b.txt", cwd=other)
        self.git("commit", "-m", "chore: elsewhere", cwd=other)
        self.git("push", "origin", BRANCH, cwd=other)
        code, out = tk.main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["stale"]), (4, True), out)
        self.assertNotIn("IMPLEMENTATION_HANDOFF", json.dumps(out))

    def test_source_changed_after_the_handoff_output(self):
        env = self.emit()
        Path(self.co, "a.txt").write_text("3\n")
        code, out = tk.main_out(env["rerun"][2:], self.fake)
        self.assertEqual((code, out["stale"]), (4, True), out)


class ResumeTest(Coordinator, unittest.TestCase):
    def test_interrupted_run_resumes_from_ledger_records_with_the_next_unmet_criteria(self):
        self.understand_and_plan()
        self.fake.refuse_native = True  # the card copy fails; the ledger keeps everything
        self.w("delegate", id="dt_1", state="started")
        Path(self.co, "a.txt").write_text("2\n")
        self.checkpoint()
        self.w("delegate", id="dt_1", state="interrupted", summary="gateway restart")
        # A new run of the same card: state comes from durable records, not memory.
        self.fake.refuse_native = False
        self.assertFalse([r for r in wf.card_records(self.fake.shows["t_1"]) if r.get("event") == "delegate"])
        v = self.view()
        self.assertEqual(v["next"][0]["stage"], "implement")
        self.assertIn("interrupted", v["next"][0]["unmet"][0])
        self.assertIn("does not release the lease", v["next"][0]["unmet"][0])
        resumes = cp.of(wf.completion_records(wf.request_cards(self.fake, "t_1")), "resume")
        self.assertEqual(resumes[-1]["next"][0]["stage"], "implement")
        # The interrupted delegate left the lease with this run.
        lease = rl.read_lease(rl.repo_identity(self.co, rl.default_git))
        self.assertEqual(lease["owner"]["task"], "t_1")
        code, pre = nw.checkout_command("preflight", "t_1", self.co, run=self.fake, scan_runner=SCAN)
        self.assertEqual(code, 0, pre)  # the checkpointed work is this card's own: resumable
        self.w("delegate", id="dt_2", state="complete")
        self.assertEqual(self.view()["next"][0]["stage"], "test")

    def test_partial_delegate_is_not_completion(self):
        self.understand_and_plan()
        self.w("delegate", id="dt_1", state="partial", remaining="the iPad layout test")
        v = self.view()
        self.assertEqual(v["next"][0]["stage"], "implement")
        self.assertIn("the iPad layout test", v["next"][0]["unmet"][0])
        with self.assertRaisesRegex(wf.WorkflowError, "--remaining"):
            self.w("delegate", id="dt_2", state="partial")

    def test_running_delegate_is_collected_not_duplicated(self):
        self.understand_and_plan()
        self.w("delegate", id="dt_1", state="started")
        self.assertIn("never start a second writer", self.view()["next"][0]["unmet"][0])


class OwnerTest(Coordinator, unittest.TestCase):
    def test_a_live_foreign_owner_is_never_taken_over(self):
        self.understand_and_plan()
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        identity = rl.repo_identity(self.co, rl.default_git)
        mine = self.owner()
        self.assertEqual(rl.release_lease(identity, mine)["status"], "released")
        other = {"board": "default", "task": "t_other", "run_id": "r9", "pid": sleeper.pid,
                 "pid_start": rl.proc_start_ticks(sleeper.pid), "host": mine["host"]}
        # Its heartbeat expired long ago (a timed-out run): alive is what counts, never the clock.
        self.assertEqual(rl.acquire_lease(identity, other, now=time.time() - 10 * rl.DEFAULT_TTL)["status"], "acquired")
        with self.assertRaisesRegex(wf.WorkflowError, "running owner.*never start a second writer"):
            self.ev("pass")
        with self.assertRaisesRegex(wf.WorkflowError, "running owner"):
            nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(rl.read_lease(identity)["owner"]["task"], "t_other")
        recs = wf.completion_records(wf.request_cards(self.fake, "t_1"))
        self.assertEqual(len(cp.of(recs, "owner-conflict")), 2)
        v = self.view()
        self.assertEqual(v["owner"]["holder"]["task"], "t_other")
        self.assertTrue(v["owner"]["alive"])
        self.assertIn("not OS isolation", v["owner"]["containment"])
        self.assertEqual(wf.completion_metrics()["duplicate_writer_refusals"], 2)

    def test_no_lease_means_preflight_first(self):
        identity = rl.repo_identity(self.co, rl.default_git)
        rl.release_lease(identity, self.owner())
        with self.assertRaisesRegex(wf.WorkflowError, "run preflight first"):
            self.w("criteria", item=["x"])

    def test_refused_for_another_card_and_without_the_cards_bound_worker(self):
        """The environment is not identity: an env claiming the card changes nothing; a
        completion write on a --checkout card needs this shell under the card's bound worker."""
        with self.assertRaisesRegex(wf.WorkflowError, "not a code card"):
            self.w("criteria", task_id="t_4", item=["x"])
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_RUN_ID": "1",
                                          "HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
            self.assertEqual(self.w("criteria", item=["x"])[0], 0)  # the board binding decides, not the env
            bound = next(e for e in self.fake.shows["t_1"]["events"] if e["kind"] == "worker_bound")
            bound["payload"]["pid"] = 1  # the run is bound to a process this shell does not descend from
            with self.assertRaisesRegex(wf.CapabilityError, "task-scoped worker identity"):
                self.w("criteria", item=["y"])
        recs = wf.completion_records(wf.request_cards(self.fake, "t_1"))
        self.assertEqual([r["items"] for r in cp.of(recs, "criteria")][-1:], [["x"]])

    def test_resolve_records_a_decision_but_never_grants_an_approval(self):
        """`completion resolve` is the coordinator's own record: it closes a recorded stop, and
        nothing it says counts as the operator's approval of a merge, release or production step."""
        self.understand_and_plan()
        code, out = self.w("block-check", reason="human-approval: merge feat/x into main")
        self.assertEqual(code, 0)
        stop = out["record"]["rid"]
        code, out = self.w("resolve", stop=stop, reason="operator approved the merge in Discord")
        self.assertEqual((code, out["record"]["event"]), (0, "resolve"))
        self.assertNotIn("approved", json.dumps({k: v for k, v in out["record"].items() if k != "reason"}))
        v = self.view()
        self.assertFalse(v["complete"])
        self.assertFalse(v["handoff_ready"])


class RecoveryTest(Coordinator, unittest.TestCase):
    def recover(self, cls="routine", sig="go test ./x: nil deref", diag="nil map", act="init the map", stage="test"):
        return self.w("recover", stage=stage, class_=cls, signature=sig, diagnosis=diag, action=act)

    def test_identical_repeat_is_refused_and_a_changed_diagnosis_proceeds(self):
        self.assertEqual(self.recover()[0], 0)
        code, out = self.recover(diag="  NIL   map ")
        self.assertEqual((code, out["record"]["decision"]), (2, "repeat-refused"))
        self.assertIn("change the diagnosis", out["record"]["detail"])
        self.assertEqual(self.recover(diag="map shared across goroutines", act="guard with a mutex")[0], 0)

    def test_routine_recovery_is_bounded_then_the_exhausted_block_is_allowed(self):
        self.assertEqual(self.w("block-check", reason="recovery-exhausted: test")[0], 2)
        for i in range(cp.MAX_RECOVERY_PER_STAGE):
            self.assertEqual(self.recover(diag=f"cause {i}")[0], 0)
        code, out = self.recover(diag="yet another cause", act="ask for the missing fixture")
        self.assertEqual(code, 4)
        self.assertTrue(out["block_reason"].startswith("recovery-exhausted: test after 8 distinct attempts"))
        self.assertIn("work is preserved", out["block_reason"])
        self.assertEqual(self.w("block-check", reason=out["block_reason"])[0], 0)

    def test_routine_failures_cannot_block_and_genuine_stops_can(self):
        for reason in ("tests fail in matching", "go: command not found", "docker build failed",
                       "lint: 3 errors", "checkout:", "delegate timed out"):
            code, out = self.w("block-check", reason=reason)
            self.assertEqual(code, 2, reason)
            self.assertEqual(out["record"]["event"], "block-refused")
        code, out = self.recover(cls="approval", sig="production deploy", diag="needs operator approval",
                                 act="ask the operator")
        self.assertEqual(code, 4)
        self.assertTrue(out["block_reason"].startswith("human-approval: production deploy"))
        self.assertEqual(self.w("block-check", reason=out["block_reason"])[0], 0)
        for reason in ("capability: dev_test_run not listed; needs an approved dev-test profile",
                       "checkout: lease held by t_x run r1 (alive)", "dependency: t_abc must finish first",
                       "device: Mac simulator runtime missing"):
            self.assertEqual(self.w("block-check", reason=reason)[0], 0, reason)
        v = self.view()
        self.assertIn("last recorded stop", v["next"][0]["unmet"][0])
        m = wf.completion_metrics()
        self.assertEqual(m["avoidable_blocks_refused"], 6)
        self.assertEqual(m["blocks_by_class"], {"approval": 1, "capability": 1, "conflict": 1, "dependency": 1, "device": 1})

    def test_an_unrelated_pass_resets_neither_the_repeat_refusal_nor_the_bound(self):
        self.understand_and_plan()  # required checks: unit, lint
        for i in range(20):
            self.ev("pass", check="unit")
            self.ev("fail", check="lint")
            code, out = self.recover(sig="lint: E501", diag="long line", act="wrap it")
            self.assertEqual((code, out["record"]["decision"]), (0, "proceed") if i == 0 else (2, "repeat-refused"), i)
        for i in range(1, cp.MAX_RECOVERY_PER_STAGE):
            self.ev("pass", check="unit")
            self.assertEqual(self.recover(sig="lint: E501", diag=f"cause {i}", act=f"approach {i}")[0], 0, i)
        self.ev("pass", check="unit")
        code, out = self.recover(sig="lint: E501", diag="one more cause", act="one more approach")
        self.assertEqual((code, out["record"]["decision"]), (4, "exhausted"))
        # Only every required check passing on one source content is progress for the test stage.
        self.ev("pass", check="lint")
        self.assertEqual(self.recover(sig="lint: E501", diag="long line", act="wrap it")[0], 0)

    def test_other_stages_reset_only_on_their_own_progress(self):
        self.understand_and_plan()
        self.ev("pass", check="unit")
        self.ev("pass", check="lint")
        for i in range(cp.MAX_RECOVERY_PER_STAGE):
            self.assertEqual(self.recover(stage="review", diag=f"cause {i}")[0], 0)
        self.ev("pass", check="unit")
        self.ev("pass", check="lint")
        self.assertEqual(self.recover(stage="review", diag="after tests passed")[0], 4, "a test pass is not review")
        self.ev("pass", kind="review", check="diff")
        self.assertEqual(self.recover(stage="review", diag="after the review passed")[0], 0)
        for i in range(cp.MAX_RECOVERY_PER_STAGE):
            self.assertEqual(self.recover(stage="push", diag=f"push cause {i}")[0], 0)
        self.ev("pass", kind="review", check="diff")
        self.ev("pass", check="unit")
        self.ev("pass", check="lint")
        self.assertEqual(self.recover(stage="push", diag="after a review")[0], 4, "only a handoff resets push")

    def test_an_unrelated_pass_never_hides_a_genuine_stop_and_approval_needs_an_explicit_resolve(self):
        self.understand_and_plan()
        code, out = self.recover(cls="approval", sig="production deploy", diag="needs operator approval",
                                 act="ask the operator")
        self.assertEqual(code, 4)
        stop_rid = out["record"]["rid"]
        self.ev("pass", check="unit")
        self.ev("pass", check="lint")
        self.ev("pass", kind="review", check="diff")
        v = self.view()
        self.assertIn("last recorded stop: human-approval: production deploy", v["next"][0]["unmet"][0])
        self.assertIn(stop_rid, v["next"][0]["unmet"][0])
        with self.assertRaisesRegex(wf.WorkflowError, "--reason"):
            self.w("resolve", stop=stop_rid)
        with self.assertRaisesRegex(wf.WorkflowError, "no recorded stop"):
            self.w("resolve", stop="completion:block:0.0", reason="x")
        code, out = self.w("resolve", stop=stop_rid, reason="operator approved the deploy in #ops (msg 123)")
        self.assertEqual(code, 0)
        self.assertNotIn("last recorded stop", json.dumps(self.view()["next"]))
        # An exhausted test stage stays open on a unit pass while lint still fails.
        for i in range(cp.MAX_RECOVERY_PER_STAGE + 1):
            self.recover(diag=f"cause {i}")
        self.ev("fail", check="lint")
        self.ev("pass", check="unit")
        self.assertIn("recovery-exhausted: test", self.view()["next"][0]["unmet"][0])
        self.ev("pass", check="lint")
        self.assertNotIn("last recorded stop", json.dumps(self.view()["next"]))

    def test_recovery_success_counts_only_the_matching_check_or_the_stage(self):
        self.understand_and_plan()
        self.ev("fail", check="lint")
        self.recover(sig="lint: E501", diag="long line", act="wrap it")
        self.ev("pass", check="unit")
        m = wf.completion_metrics()["recovery_after_failure"]
        self.assertEqual((m["count"], m["of"], m["unknown"]), (0, 1, 1), "an unrelated unit pass is not a recovery")
        self.ev("fail", check="lint")
        m = wf.completion_metrics()["recovery_after_failure"]
        self.assertEqual((m["count"], m["of"], m["unknown"]), (0, 1, 0))
        self.ev("pass", check="lint")
        m = wf.completion_metrics()["recovery_after_failure"]
        self.assertEqual((m["count"], m["of"], m["unknown"]), (1, 1, 0))
        self.recover(stage="push", sig="push rejected", diag="non-fast-forward", act="merge --ff-only")
        self.ev("pass", check="unit")
        m = wf.completion_metrics()["recovery_after_failure"]
        self.assertEqual((m["count"], m["of"], m["unknown"]), (1, 2, 1), "no handoff yet: unknown, not success")

    def test_held_cards_are_reported_never_promoted(self):
        self.fake.shows["t_2"]["task"]["status"] = "blocked"
        v = self.view()
        self.assertEqual([h["id"] for h in v["held"]], ["t_2"])
        self.assertIn("never unblocked or promoted automatically", v["held"][0]["rule"])
        self.assertFalse([c for c in self.fake.calls if c[:2] in (["kanban", "unblock"], ["kanban", "promote"])])


class CapabilityAndCliTest(Coordinator, unittest.TestCase):
    def test_detection_reads_allowlisted_names_and_executes_nothing(self):
        root = Path(tempfile.mkdtemp(prefix="completion-caps-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(root)]))
        marker = root / "EXECUTED"
        (root / "go.mod").write_text("module x\n")
        (root / "migrations").mkdir()
        (root / "Dockerfile").write_text(f"RUN touch {marker}\n")
        (root / "App.xcodeproj").mkdir()
        (root / "package.json").write_text(json.dumps({"scripts": {"test": f"touch {marker}", "lint": "x"}}))
        (root / "Makefile").write_text(f"test:\n\ttouch {marker}\n")
        caps = cp.detect_capabilities(str(root))
        self.assertFalse(marker.exists())
        self.assertFalse(caps["hints_executed"])
        for stack in ("go", "node", "docker", "database:migrations", "apple:xcode", "make"):
            self.assertIn(stack, caps["stacks"])
        self.assertEqual(caps["package_scripts"], ["lint", "test"])
        needs = " ".join(caps["approved_capabilities_needed"])
        for text in ("dev_test_run", "unrestricted Docker access is not granted", "Mac lane"):
            self.assertIn(text, needs)

    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nw.main(["completion", "status", "t_1", "--repo-dir", self.co], self.fake), 0)
        self.assertEqual(json.loads(out.getvalue())["next"][0]["stage"], "understand")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nw.main(["completion", "block-check", "t_1", "--reason", "tests fail"], self.fake), 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nw.main(["completion", "metrics", "--json"], self.fake), 0)
        self.assertEqual(json.loads(out.getvalue())["avoidable_blocks_refused"], 1)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(nw.main(["completion", "evidence", "t_1", "--repo-dir", self.co, "--kind", "test",
                                      "--check", "unit", "--result", "pass", "--command", "make test", "--exit", "1"],
                                     self.fake), 2)
        self.assertIn("a pass needs exit 0", err.getvalue())

    def test_first_run_order_and_scanner_block_prefix(self):
        ctx = wf.Ctx(key="k", title="T", repo=tk.REPO_URL, branch="feat/x", base="main", lanes={}, coder="coder",
                     reviewer="reviewer", orchestrator="orchestrator")
        text = wf.code_body(ctx, 1, "the ask", "")
        self.assertLess(text.index("git clone"), text.index("completion status"))
        co = wf.code_body(wf.Ctx(**{**ctx.__dict__, "checkout": "/opt/repos/app"}), 1, "the ask", "")
        self.assertLess(co.index("preflight {TASK_TOKEN} --repo-dir".replace("{TASK_TOKEN}", wf.TASK_TOKEN)),
                        co.index("completion status"))
        self.assertLess(co.index("exit 0: proceed"), co.index("completion criteria"))
        for body in (text, co):
            self.assertIn('kanban_block(reason="capability: <the exact command>; <the scanner finding>; '
                          'missing <the capability>")', body)

    def test_dev_workflow_rollout_allowlists_the_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("adw", tk.REPO / "scripts" / "apply-dev-workflow.py")
        adw = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"adw": adw}):
            spec.loader.exec_module(adw)
        self.assertIn("scripts/completion.py", adw.SKILL_FILES)


if __name__ == "__main__":
    unittest.main()
