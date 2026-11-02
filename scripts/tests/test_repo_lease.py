"""Tests for hermes/skills/kanban-workflow/scripts/repo_lease.py.

Repository lease (two real processes racing, same-owner renewal, dead/PID-reused owner
recovery, alive owner never taken over, symlink safety), competing-writer classification
(self chain, own descendants, foreign agent, mutating git, interactive, gone, unknown), the
privileged scanner integration (exit 0/1/2 contract, registration rules, unprivileged
fallback), task-owned dirty manifests (checkpoint, adopt, changed/unknown) and the
preflight that ties them together. Real git on temp repositories; no live board, no
~/.hermes (HERMES_WORKFLOW_STATE points at a temp dir).

Run: python3 -m unittest discover -s scripts/tests -p 'test_repo_lease.py'
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "hermes" / "skills" / "kanban-workflow" / "scripts"
SCRIPT = SCRIPTS / "repo_lease.py"

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("repo_lease", SCRIPT)
rl = importlib.util.module_from_spec(spec)
sys.modules["repo_lease"] = rl
spec.loader.exec_module(rl)

REPO_URL = "https://github.com/acme/app.git"


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def scan_runner(returncode, stdout):
    calls = []

    def run(argv):
        calls.append(argv)
        return done(returncode, stdout)

    run.calls = calls
    return run


class TempState(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.state = Path(tempfile.mkdtemp(prefix="repo-lease-state-")).resolve()
        patcher = mock.patch.dict(os.environ, {"HERMES_WORKFLOW_STATE": str(self.state)}, clear=False)
        patcher.start()
        for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD",
                    "HERMES_KANBAN_SUPERVISED_WORKER_PID", "HERMES_DELEGATED_CHILD_CONTEXT"):
            os.environ.pop(key, None)
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.state, True)
        self.procs = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def spawn(self, argv, **kw):
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.procs.append(p)
        return p


class TempRepo(TempState):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="repo-lease-repo-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / "remote.git"
        self.url = f"file://{self.remote}"
        self.co = str(self.tmp / "validate")
        self.g("init", "--bare", "-b", "main", str(self.remote), cwd=str(self.tmp))
        self.g("clone", self.url, self.co, cwd=str(self.tmp))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            self.g("config", k, v)
        Path(self.co, "a.txt").write_text("1\n")
        self.g("add", "a.txt")
        self.g("commit", "-m", "chore: init")
        self.g("push", "origin", "main")
        self.g("switch", "-c", "feat/x")
        self.g("push", "-u", "origin", "feat/x")

    def g(self, *argv, cwd=None):
        return subprocess.run(["git", *argv], cwd=cwd or self.co, check=True, capture_output=True, text=True).stdout

    def identity(self):
        return rl.repo_identity(self.co, rl.default_git)

    def owner(self, task="t_1", run="7", pid=None):
        return {"board": "default", "task": task, "run_id": run, "pid": pid or os.getpid(),
                "pid_start": rl.proc_start_ticks(pid or os.getpid()), "host": rl.socket.gethostname()}


# ----------------------------------------------------------------- lease ---

class LeaseTest(TempRepo):
    def test_acquire_renew_release(self):
        ident, owner = self.identity(), self.owner()
        first = rl.acquire_lease(ident, owner)
        self.assertEqual(first["status"], "acquired")
        path = rl.lease_path(ident)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        again = rl.acquire_lease(ident, owner)
        self.assertEqual(again["status"], "renewed")
        self.assertEqual(again["lease"]["acquired_at"], first["lease"]["acquired_at"])
        self.assertEqual(rl.release_lease(ident, self.owner(task="t_other"))["status"], "conflict")
        self.assertTrue(path.exists())
        self.assertEqual(rl.release_lease(ident, owner)["status"], "released")
        self.assertFalse(path.exists())
        self.assertEqual(rl.release_lease(ident, owner)["status"], "none")

    def test_alive_foreign_owner_is_a_conflict_even_when_expired(self):
        ident = self.identity()
        sleeper = self.spawn(["sleep", "60"])
        holder = self.owner(task="t_other", run="3", pid=sleeper.pid)
        rl.acquire_lease(ident, holder, ttl=1, now=time.time() - 100)
        result = rl.acquire_lease(ident, self.owner())
        self.assertEqual(result["status"], "conflict")
        self.assertIn("stale heartbeat, process alive", result["reason"])
        self.assertIn("never taken over", result["reason"])
        self.assertEqual(rl.read_lease(ident)["owner"]["task"], "t_other")
        fresh = rl.acquire_lease(ident, holder)
        self.assertEqual(fresh["status"], "renewed")
        self.assertIn("active", rl.acquire_lease(ident, self.owner())["reason"])

    def test_dead_owner_is_recovered_and_pid_reuse_is_detected(self):
        ident = self.identity()
        sleeper = self.spawn(["sleep", "60"])
        holder = self.owner(task="t_old", run="2", pid=sleeper.pid)
        rl.acquire_lease(ident, holder)
        sleeper.kill(); sleeper.wait()
        result = rl.acquire_lease(ident, self.owner())
        self.assertEqual(result["status"], "recovered")
        self.assertIn("is gone", result["reason"])
        # Same pid as a live process, different kernel start time: not the same incarnation.
        reused = {**self.owner(task="t_reuse", run="5"), "pid_start": (rl.proc_start_ticks(os.getpid()) or 0) + 1}
        rl.release_lease(ident, self.owner())
        rl.acquire_lease(ident, reused)
        result = rl.acquire_lease(ident, self.owner(task="t_new", run="9"))
        self.assertEqual(result["status"], "recovered", result)
        self.assertFalse(list(rl.leases_dir().glob("*.stale.*")))

    def test_owner_on_another_host_is_not_decidable(self):
        ident = self.identity()
        rl.acquire_lease(ident, {**self.owner(task="t_mac"), "host": "someone-elses-host"})
        result = rl.acquire_lease(ident, self.owner())
        self.assertEqual(result["status"], "conflict")
        self.assertIn("not decidable", result["reason"])

    def test_symlinked_lease_is_refused(self):
        ident = self.identity()
        path = rl.lease_path(ident)
        target = self.state / "elsewhere.json"
        target.write_text("{}")
        path.symlink_to(target)
        with self.assertRaises(rl.LeaseError):
            rl.read_lease(ident)
        with self.assertRaises(rl.LeaseError):
            rl.acquire_lease(ident, self.owner())
        self.assertEqual(target.read_text(), "{}")

    def test_two_processes_racing_only_one_acquires(self):
        ident = self.identity()
        start = time.time() + 0.8
        code = (
            "import sys, time, json, importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('rl', {str(SCRIPT)!r}); rl = importlib.util.module_from_spec(spec); spec.loader.exec_module(rl)\n"
            f"ident = json.loads({json.dumps(ident)!r})\n"
            "task = sys.argv[1]\n"
            "import os\n"
            "owner = {'board': 'default', 'task': task, 'run_id': task[-1], 'pid': os.getpid(), 'pid_start': rl.proc_start_ticks(os.getpid()), 'host': rl.socket.gethostname()}\n"
            f"while time.time() < {start}: pass\n"
            "res = [rl.acquire_lease(ident, owner)['status'] for _ in range(20)]\n"
            "time.sleep(1.0)\n"
            "print(json.dumps({'task': task, 'statuses': sorted(set(res))}))\n"
        )
        env = {**os.environ}
        procs = [subprocess.Popen([sys.executable, "-c", code, f"t_racer{i}"], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for i in (1, 2)]
        raw = [p.communicate(timeout=60) for p in procs]
        outs = []
        for stdout, stderr in raw:
            self.assertTrue(stdout.strip(), stderr)
            outs.append(json.loads(stdout))
        statuses = {o["task"]: o["statuses"] for o in outs}
        winners = [t for t, s in statuses.items() if "acquired" in s or "renewed" in s]
        losers = [t for t, s in statuses.items() if s == ["conflict"]]
        self.assertEqual(len(winners), 1, statuses)
        self.assertEqual(len(losers), 1, statuses)
        self.assertNotIn("recovered", statuses[winners[0]])
        # Both racers have exited: the lease is now recoverable by this process (bounded recovery).
        self.assertEqual(rl.acquire_lease(ident, self.owner())["status"], "recovered")


# ------------------------------------------------------------- processes ---

class ProcessClassificationTest(TempRepo):
    def test_self_chain_and_own_descendants_are_not_writers(self):
        child = self.spawn(["sleep", "60"], cwd=self.co)
        rows = rl.classify_scan({"status": "complete", "matches": [
            {"pid": os.getpid(), "cwd": self.co}, {"pid": os.getppid(), "cwd": self.co}, {"pid": child.pid, "cwd": self.co}],
            "scanner": "x"})
        kinds = {r["pid"]: r["kind"] for r in rows["processes"]}
        self.assertEqual(kinds[os.getpid()], "self-chain")
        self.assertEqual(kinds[os.getppid()], "self-chain")
        self.assertEqual(kinds[child.pid], "own-descendant")
        self.assertTrue(rows["ok"])

    def test_descendant_of_the_worker_anchor_is_ours_even_from_another_shell(self):
        child = self.spawn(["sleep", "60"], cwd=self.co)
        row = rl.classify_process(child.pid, self_pid=10 ** 6, anchors=[os.getpid()], self_chain=set())
        self.assertEqual(row["kind"], "own-descendant")

    def test_foreign_agent_and_mutating_git_block(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name in ("claude", "node"):
            (bindir / name).symlink_to(shutil.which("sleep"))
        agent = self.spawn([str(bindir / "claude"), "60"], cwd=self.co)
        # A real `git commit` parked in its editor: a mutation in progress (index.lock held).
        editor = bindir / "park-editor"
        editor.write_text("#!/bin/sh\nexec sleep 60\n"); editor.chmod(0o755)
        git = self.spawn(["git", "commit", "--allow-empty"], cwd=self.co,
                         env={**os.environ, "GIT_EDITOR": str(editor), "GIT_TERMINAL_PROMPT": "0"})
        node = self.spawn([str(bindir / "node"), "60"], cwd=self.co)
        time.sleep(0.5)
        rows = {p.pid: rl.classify_process(p.pid, self_pid=10 ** 6, anchors=[], self_chain=set())
                for p in (agent, git, node)}
        self.assertEqual((rows[agent.pid]["kind"], rows[agent.pid]["blocking"]), ("agent-writer", True))
        self.assertEqual((rows[git.pid]["kind"], rows[git.pid]["blocking"]), ("git-mutation", True))
        self.assertIn("git commit", rows[git.pid]["detail"])
        self.assertEqual((rows[node.pid]["kind"], rows[node.pid]["blocking"]), ("service", False))
        with mock.patch.object(rl, "proc_has_tty", return_value=True):
            row = rl.classify_process(node.pid, self_pid=10 ** 6, anchors=[], self_chain=set())
        self.assertEqual((row["kind"], row["blocking"]), ("interactive", False))
        self.assertIsNone(rl._git_mutation(["git", "-C", self.co, "status", "--porcelain"]))
        self.assertIsNone(rl._git_mutation(["/usr/bin/git", "log", "--oneline"]))
        self.assertEqual(rl._git_mutation(["git", "-c", "x=y", "rebase", "main"]), "rebase")

    def test_gone_and_unknown_are_distinct(self):
        gone = self.spawn(["sleep", "60"]); gone.kill(); gone.wait()
        self.assertEqual(rl.classify_process(gone.pid, 10 ** 6, [], set())["kind"], "gone")
        with mock.patch.object(rl, "_proc_read", side_effect=rl.ProcVisibilityError(13, "denied")):
            row = rl.classify_process(os.getpid() + 1, 10 ** 6, [], set())
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True))
        incomplete = rl.classify_scan({"status": "incomplete", "matches": [], "scanner": "sudo", "tail": "FAIL: 3 errors"})
        self.assertFalse(incomplete["ok"])
        self.assertEqual(incomplete["blocking"][0]["kind"], "unknown")

    def test_unprivileged_fallback_sees_own_processes(self):
        child = self.spawn(["sleep", "60"], cwd=self.co)
        time.sleep(0.1)
        scan = rl.run_scan(self.co, registry={})
        self.assertEqual(scan["scanner"], "fallback:/proc")
        self.assertIn(child.pid, [m["pid"] for m in scan["matches"]])
        if scan["status"] == "incomplete":  # other users' processes exist on this host
            self.assertFalse(rl.classify_scan(scan)["ok"])
        else:
            self.assertTrue(rl.classify_scan(scan)["ok"])

    def test_anchor_pid_prefers_a_verified_supervised_worker(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_SUPERVISED_WORKER_PID": str(os.getppid())}):
            self.assertEqual(rl.anchor_pid(), os.getppid())
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_SUPERVISED_WORKER_PID": "999999999"}):
            self.assertNotEqual(rl.anchor_pid(), 999999999)  # not an ancestor: ignored
        self.assertEqual(rl.anchor_pid(4242), 4242)


# ---------------------------------------------------------------- scanner ---

class ScannerTest(TempRepo):
    def test_exit_code_contract(self):
        ok = rl.parse_scan_output(0, "PASS: Inspected 500 process directories.\nPASS: No repository CWD matches found.\n")
        self.assertEqual((ok["status"], ok["matches"]), ("complete", []))
        hit = rl.parse_scan_output(1, "MATCH: PID=4321 CWD=/opt/repos/validate/apps\nBLOCKED: 1 matching processes.\n")
        self.assertEqual((hit["status"], hit["matches"]), ("complete", [{"pid": 4321, "cwd": "/opt/repos/validate/apps"}]))
        bad = rl.parse_scan_output(2, "ERROR: PID=7 [Errno 13]\nFAIL: 1 inspection errors.\n")
        self.assertEqual(bad["status"], "incomplete")
        self.assertIn("FAIL: 1 inspection errors", bad["tail"])

    def test_registration_rules(self):
        with self.assertRaises(rl.LeaseError):
            rl.validate_scanner_argv(["sudo", "/bin/true"])
        with self.assertRaises(rl.LeaseError):
            rl.validate_scanner_argv(["sudo", "-n", "/bin/true", "--extra"])
        with self.assertRaises(rl.LeaseError):
            rl.validate_scanner_argv(["sudo", "-n", "bin/true"])
        mine = self.tmp / "scan.sh"
        mine.write_text("#!/bin/sh\nexit 0\n"); mine.chmod(0o755)
        with self.assertRaises(rl.LeaseError):  # user-owned helper can never become a privileged observer
            rl.validate_scanner_argv(["sudo", "-n", str(mine)])
        self.assertEqual(rl.validate_scanner_argv(["sudo", "-n", "/bin/true"]), ["sudo", "-n", "/bin/true"])
        reg = rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        self.assertEqual(reg["scanner"], ["sudo", "-n", "/bin/true"])
        self.assertEqual(rl.scanner_registry()[self.co], ["sudo", "-n", "/bin/true"])
        self.assertEqual(oct(rl.scanner_registry_path().stat().st_mode & 0o777), "0o600")

    def test_registered_scanner_runs_with_exact_argv_and_no_arguments(self):
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        runner = scan_runner(0, "PASS: Inspected 1 process directories.\n")
        scan = rl.run_scan(self.co, runner)
        self.assertEqual(runner.calls, [["sudo", "-n", "/bin/true"]])
        self.assertEqual((scan["status"], scan["scanner"]), ("complete", "sudo -n /bin/true"))
        failing = scan_runner(2, "FAIL: Cannot enumerate /proc")
        self.assertEqual(rl.run_scan(self.co, failing)["status"], "incomplete")

        def boom(argv):
            raise OSError("sudo: a password is required")
        self.assertEqual(rl.run_scan(self.co, boom)["status"], "incomplete")


# -------------------------------------------------------------- manifests ---

class ManifestTest(TempRepo):
    def test_checkpoint_resume_changed_unknown(self):
        ident = self.identity()
        self.assertTrue(rl.verify_dirty("t_1", ident, rl.default_git)["clean"])
        Path(self.co, "a.txt").write_text("2\n")                  # unstaged
        Path(self.co, "new.go").write_text("package x\n")         # untracked
        Path(self.co, "staged.txt").write_text("s\n"); self.g("add", "staged.txt")
        before = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((before["ok"], sorted(before["unknown"])), (False, ["a.txt", "new.go", "staged.txt"]))
        manifest = rl.checkpoint("t_1", ident, rl.default_git)
        self.assertEqual(sorted(manifest["entries"]), ["a.txt", "new.go", "staged.txt"])
        self.assertEqual(manifest["entries"]["staged.txt"]["status"], "A ")
        self.assertTrue(manifest["entries"]["staged.txt"]["index_oid"])
        text = rl.manifest_path("t_1").read_text()
        self.assertNotIn("package x", text)                        # hashes only, never contents
        self.assertEqual(oct(rl.manifest_path("t_1").stat().st_mode & 0o777), "0o600")
        after = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((after["ok"], sorted(after["known"])), (True, ["a.txt", "new.go", "staged.txt"]))
        Path(self.co, "a.txt").write_text("3\n")
        Path(self.co, "other.txt").write_text("?\n")
        drift = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((drift["ok"], drift["changed"], drift["unknown"]), (False, ["a.txt"], ["other.txt"]))
        self.assertFalse(rl.verify_dirty("t_2", ident, rl.default_git)["ok"])  # another task owns nothing here
        # Same files on another branch are no longer provably this task's.
        self.g("stash", "-u"); self.g("switch", "-c", "feat/y"); self.g("stash", "pop")
        moved = rl.verify_dirty("t_1", self.identity(), rl.default_git)
        self.assertFalse(moved["ok"]); self.assertEqual(moved["known"], [])

    def test_adopt_requires_exact_provenance(self):
        ident = self.identity()
        Path(self.co, "a.txt").write_text("2\n")
        Path(self.co, "new.go").write_text("package x\n")
        good = {"a.txt": rl.hash_worktree_path(self.co, "a.txt"), "new.go": rl.hash_worktree_path(self.co, "new.go")}
        src = self.tmp / "preserved-sha256.json"
        src.write_text(json.dumps({**good, "new.go": "0" * 64}))
        with self.assertRaises(rl.LeaseError) as ctx:
            rl.adopt_manifest("t_1", ident, rl.default_git, str(src))
        self.assertIn("content differs: new.go", str(ctx.exception))
        self.assertIsNone(rl.read_manifest("t_1"))
        src.write_text(json.dumps({**good, "ghost.txt": "1" * 64}))
        with self.assertRaises(rl.LeaseError) as ctx:
            rl.adopt_manifest("t_1", ident, rl.default_git, str(src))
        self.assertIn("not dirty here: ghost.txt", str(ctx.exception))
        src.write_text(json.dumps({"a.txt": good["a.txt"]}))
        adopted = rl.adopt_manifest("t_1", ident, rl.default_git, str(src))
        self.assertEqual(adopted["unlisted_dirty"], ["new.go"])
        self.assertEqual(adopted["source"], "adopt:preserved-sha256.json")
        verify = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((verify["known"], verify["unknown"]), (["a.txt"], ["new.go"]))
        with self.assertRaises(rl.LeaseError):
            src.write_text("[]"); rl.adopt_manifest("t_1", ident, rl.default_git, str(src))


# -------------------------------------------------------------- preflight ---

class PreflightTest(TempRepo):
    def setUp(self):
        super().setUp()
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        self.pass_scan = scan_runner(0, "PASS: Inspected 1 process directories.\nPASS: No repository CWD matches found.\n")

    def pf(self, task="t_1", runner=None, **kw):
        return rl.preflight(task, self.co, self.url, "feat/x", "main", git=rl.default_git,
                            scan_runner=runner or self.pass_scan, owner=self.owner(task=task), **kw)

    def names(self, result, ok):
        return sorted(c["check"] for c in result["checks"] if c["ok"] is ok)

    def test_clean_checkout_passes_and_holds_the_lease(self):
        result = self.pf()
        self.assertEqual((result["ok"], result["exit"]), (True, 0), result["checks"])
        self.assertEqual(result["lease"]["status"], "acquired")
        self.assertEqual(self.pf()["lease"]["status"], "renewed")
        self.assertEqual(rl.read_lease(self.identity())["owner"]["task"], "t_1")

    def test_other_task_holding_the_lease_blocks(self):
        sleeper = self.spawn(["sleep", "60"])
        rl.acquire_lease(self.identity(), self.owner(task="t_other", run="1", pid=sleeper.pid))
        result = self.pf()
        self.assertEqual((result["ok"], result["exit"]), (False, 2))
        self.assertEqual(self.names(result, False), ["lease"])

    def test_scan_classification_decides(self):
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "codex").symlink_to(shutil.which("sleep"))
        foreign = self.spawn([str(bindir / "codex"), "60"], cwd=self.co)
        mine = self.spawn(["sleep", "60"], cwd=self.co)
        time.sleep(0.2)
        hits = scan_runner(1, f"MATCH: PID={mine.pid} CWD={self.co}\nMATCH: PID={foreign.pid} CWD={self.co}\nBLOCKED: 2 matching processes.\n")
        # The run's anchor is an unrelated live process and self_pid is far away, so neither match
        # descends from "us": `sleep` is a plain background process, `codex` a foreign agent.
        anchor = self.spawn(["sleep", "60"])
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=hits,
                              owner=self.owner(pid=anchor.pid), self_pid=10 ** 6)
        self.assertEqual((result["ok"], result["exit"]), (False, 2))
        kinds = {p["pid"]: p["kind"] for p in result["scan"]["processes"]}
        self.assertEqual((kinds[mine.pid], kinds[foreign.pid]), ("service", "agent-writer"))
        self.assertIn(f"pid {foreign.pid}: agent-writer", next(c["detail"] for c in result["checks"] if c["check"] == "writers"))
        foreign.kill(); foreign.wait()
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=hits,
                              owner=self.owner(pid=anchor.pid), self_pid=10 ** 6)
        self.assertTrue(result["ok"], "a vanished match is not a writer")
        self.assertEqual({p["kind"] for p in result["scan"]["processes"]}, {"service", "gone"})
        self.assertEqual(self.pf(task="t_2", runner=hits)["lease"]["status"], "conflict",
                         "the anchor's run (alive) still holds the lease for another task")
        anchor.kill(); anchor.wait()
        self.assertEqual(self.pf(task="t_2", runner=hits)["lease"]["status"], "recovered")
        incomplete = scan_runner(2, "ERROR: PID=5 [Errno 13] Permission denied\nFAIL: 1 inspection errors.\n")
        result = self.pf(runner=incomplete)
        self.assertEqual((result["ok"], result["exit"]), (False, 3))

    def test_git_state_checks(self):
        Path(self.identity()["gitdir"], "index.lock").write_text("")
        self.assertEqual(self.names(self.pf(), False), ["index.lock"])
        Path(self.identity()["gitdir"], "index.lock").unlink()
        self.g("switch", "main")
        result = self.pf()
        self.assertEqual(self.names(result, False), ["branch"])
        self.assertIn("checkout is on 'main'", next(c["detail"] for c in result["checks"] if c["check"] == "branch"))
        self.g("switch", "feat/x")
        self.assertFalse(rl.preflight("t_1", self.co, self.url, "main", git=rl.default_git, scan_runner=self.pass_scan,
                                      owner=self.owner())["ok"])
        wt = str(self.tmp / "wt")
        self.g("worktree", "add", wt, "feat/x", "--force")
        self.assertEqual(self.names(self.pf(), False), ["worktrees"])
        self.g("worktree", "remove", "--force", wt)
        self.g("commit", "--allow-empty", "-m", "local"); self.g("push", "origin", "feat/x")
        self.g("reset", "--hard", "HEAD~1"); self.g("commit", "--allow-empty", "-m", "mine")
        result = self.pf()
        self.assertEqual(self.names(result, False), ["diverged"])
        self.assertFalse(rl.preflight("t_1", self.co, "https://github.com/other/x.git", "feat/x", git=rl.default_git,
                                      scan_runner=self.pass_scan, owner=self.owner())["ok"])

    def test_dirty_provenance_decides_resume(self):
        Path(self.co, "a.txt").write_text("2\n")
        result = self.pf()
        self.assertEqual(self.names(result, False), ["dirty"])
        self.assertIn("unknown dirty paths", next(c["detail"] for c in result["checks"] if c["check"] == "dirty"))
        rl.checkpoint("t_1", self.identity(), rl.default_git)
        self.assertTrue(self.pf()["ok"])
        self.assertFalse(self.pf(task="t_2")["ok"], "another task's run never resumes these files")
        strict = self.pf(require_clean=True)
        self.assertEqual(self.names(strict, False), ["dirty"])
        self.assertEqual(Path(self.co, "a.txt").read_text(), "2\n", "nothing is ever reset or stashed")

    def test_cli_round_trip(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "status", "--checkout", self.co], capture_output=True, text=True,
                             env={**os.environ, "HERMES_WORKFLOW_STATE": str(self.state)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["scanner"], ["sudo", "-n", "/bin/true"])
        with mock.patch.object(rl, "default_scan_runner", self.pass_scan):
            with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as buf:
                code = rl.main(["preflight", "--task", "t_1", "--checkout", self.co, "--repo", self.url, "--branch", "feat/x"])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertTrue(json.loads(buf.getvalue())["ok"])
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_zzz"}):
            with mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
                code = rl.main(["checkpoint", "--task", "t_1", "--checkout", self.co])
            self.assertEqual(code, 2)
            self.assertIn("belongs to card t_zzz", err.getvalue())
        with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as buf:
            self.assertEqual(rl.main(["release", "--task", "t_1", "--checkout", self.co]), 0)
        self.assertEqual(json.loads(buf.getvalue())["status"], "released")


if __name__ == "__main__":
    unittest.main()
