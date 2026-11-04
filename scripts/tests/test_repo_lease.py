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


def bound_show(task="t_1", pid=None, run=7, status="running", started_at=None, extra_events=(), runs=None):
    """`hermes kanban show --json` of a card whose run `run` was bound by the managed worker `pid`."""
    pid = pid or os.getppid()
    if started_at is None:
        started_at = f"{rl.instantiation_epoch()}|{rl.proc_start_ticks(pid)}"
    return {"task": {"id": task, "status": status},
            "runs": runs if runs is not None else [{"id": run, "profile": "default", "status": "running", "ended_at": None}],
            "events": [{"kind": "worker_bound", "run_id": run, "payload": {"pid": pid, "claim_lock": "l",
                                                                         "started_at": started_at}},
                       *extra_events]}


def show_runner(*shows):
    by_id = {s["task"]["id"]: s for s in shows}

    def hermes(argv):
        if argv[:2] == ["kanban", "show"] and argv[2] in by_id:
            return done(0, json.dumps(by_id[argv[2]]))
        return done(1, err=f"no such task: {argv[2:3]}")
    return hermes


# A genuine bound interpreter: `python -m agent.managed_worker` (the module name upstream's managed
# worker runs under) that starts PROBE as its child, the way the worker's terminal tool does.
MANAGED_WORKER = (
    "import os, subprocess, sys\n"
    "r = subprocess.run([sys.executable, '-c', os.environ['PROBE']], capture_output=True, text=True, timeout=120)\n"
    "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)\n")


class TempState(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.state = Path(tempfile.mkdtemp(prefix="repo-lease-state-")).resolve()
        patcher = mock.patch.dict(os.environ, {"HERMES_WORKFLOW_STATE": str(self.state)}, clear=False)
        patcher.start()
        for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD",
                    "HERMES_KANBAN_SUPERVISED_WORKER_PID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_BIN"):
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

    PASS_SCAN = "PASS: Inspected 1 process directories.\nPASS: No repository CWD matches found.\n"

    def passing_runner(self):
        return scan_runner(0, self.PASS_SCAN)

    def baseline(self, task="t_1", **kw):
        """A passing preflight by this run: takes the lease and records the edit-round baseline."""
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        result = rl.preflight(task, self.co, self.url, self.identity()["branch"], "main", git=rl.default_git,
                              scan_runner=self.passing_runner(), owner=self.owner(task=task), **kw)
        self.assertTrue(result["ok"], result["checks"])
        return result

    def checkpoint(self, task="t_1"):
        return rl.checkpoint(task, self.identity(), rl.default_git, owner=self.owner(task=task), scan_runner=self.passing_runner())


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

    def test_same_owner_needs_the_process_identity_not_the_run_id_alone(self):
        ident = self.identity()
        rl.acquire_lease(ident, self.owner())
        copied = {**self.owner(), "pid": os.getpid() + 1}          # same run id, another process
        self.assertEqual(rl.acquire_lease(ident, copied)["status"], "conflict")
        reused = {**self.owner(), "pid_start": (rl.proc_start_ticks(os.getpid()) or 0) + 1}  # pid reused
        self.assertEqual(rl.acquire_lease(ident, reused)["status"], "conflict")
        self.assertEqual(rl.release_lease(ident, copied)["status"], "conflict")
        self.assertEqual(rl.release_lease(ident, {**self.owner(), "host": "elsewhere"})["status"], "conflict")
        self.assertEqual(rl.acquire_lease(ident, self.owner())["status"], "renewed")
        self.assertEqual(rl.acquire_lease(ident, self.owner(), renew_only=True)["status"], "renewed")
        self.assertEqual(rl.release_lease(ident, self.owner())["status"], "released")
        self.assertEqual(rl.acquire_lease(ident, self.owner(), renew_only=True)["status"], "none")
        self.assertIsNone(rl.read_lease(ident), "renew_only never takes a lease")

    def test_concurrent_stale_recovery_has_exactly_one_winner(self):
        """Four processes find the same dead-owner lease at once: one recovers it, the others
        see that live recoverer and conflict; the lease never flips between two recoverers."""
        ident = self.identity()
        dead = self.spawn(["sleep", "60"])
        holder = self.owner(task="t_dead", run="1", pid=dead.pid)
        rl.acquire_lease(ident, holder)
        dead.kill(); dead.wait()
        start = time.time() + 1.0
        code = (
            "import sys, time, json, os, importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('rl', {str(SCRIPT)!r}); rl = importlib.util.module_from_spec(spec); spec.loader.exec_module(rl)\n"
            f"ident = json.loads({json.dumps(ident)!r})\n"
            "task = sys.argv[1]\n"
            "owner = {'board': 'default', 'task': task, 'run_id': '9', 'pid': os.getpid(), 'pid_start': rl.proc_start_ticks(os.getpid()), 'host': rl.socket.gethostname()}\n"
            f"while time.time() < {start}: pass\n"
            "res = [rl.acquire_lease(ident, owner)['status'] for _ in range(10)]\n"
            "time.sleep(1.5)\n"
            "print(json.dumps({'task': task, 'statuses': sorted(set(res)), 'holder': rl.read_lease(ident)['owner']['task']}))\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code, f"t_rec{i}"], env={**os.environ}, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for i in range(4)]
        outs = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=60)
            self.assertTrue(stdout.strip(), stderr)
            outs.append(json.loads(stdout))
        statuses = {o["task"]: o["statuses"] for o in outs}
        winners = [t for t, st in statuses.items() if "recovered" in st]
        self.assertEqual(len(winners), 1, statuses)
        self.assertEqual(statuses[winners[0]], ["recovered", "renewed"], statuses)
        for t, st in statuses.items():
            if t != winners[0]:
                self.assertEqual(st, ["conflict"], statuses)
        self.assertEqual({o["holder"] for o in outs}, {winners[0]}, "every racer saw the same final holder")
        self.assertFalse(list(rl.leases_dir().glob(".tmp-*")))

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
    def test_self_chain_stops_at_the_anchor_and_own_descendants_are_not_writers(self):
        child = self.spawn(["sleep", "60"], cwd=self.co)
        time.sleep(0.2)
        parent = os.getppid()
        rows = rl.classify_scan({"status": "complete", "matches": [
            {"pid": os.getpid(), "cwd": self.co}, {"pid": parent, "cwd": self.co}, {"pid": child.pid, "cwd": self.co}],
            "scanner": "x"}, anchors=[parent])
        kinds = {r["pid"]: r["kind"] for r in rows["processes"]}
        self.assertEqual(kinds[os.getpid()], "self-chain")
        self.assertEqual(kinds[parent], "self-chain")
        self.assertEqual(kinds[child.pid], "own-descendant")
        self.assertTrue(rows["ok"])
        # The chain ends at the anchor: a shared ancestor above it (gateway, terminal) is not "us",
        # and without an anchor among our ancestors nothing but this process is.
        self.assertEqual(rl.self_chain(os.getpid(), [parent]), {parent})
        self.assertEqual(rl.self_chain(os.getpid(), []), set())
        grand = rl.proc_parent(parent)
        if grand and grand > 1:
            self.assertEqual(rl.self_chain(os.getpid(), [grand]), {parent, grand})
            row = rl.classify_process(grand, os.getpid(), anchors=[parent], self_chain=rl.self_chain(os.getpid(), [parent]))
            self.assertNotEqual(row["kind"], "self-chain")
        rows = rl.classify_scan({"status": "complete", "matches": [{"pid": parent, "cwd": self.co}], "scanner": "x"})
        self.assertNotEqual(rows["processes"][0]["kind"], "self-chain", "no anchor: the parent is a foreign process")

    def test_descendant_of_the_worker_anchor_is_ours_even_from_another_shell(self):
        child = self.spawn(["sleep", "60"], cwd=self.co)
        time.sleep(0.2)
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
        (bindir / "nvim").symlink_to(shutil.which("sleep"))
        editor_proc = self.spawn([str(bindir / "nvim"), "60"], cwd=self.co)
        plain = self.spawn(["sleep", "60"], cwd=self.co)
        time.sleep(0.5)
        rows = {p.pid: rl.classify_process(p.pid, self_pid=10 ** 6, anchors=[], self_chain=set())
                for p in (agent, git, node, editor_proc, plain)}
        self.assertEqual((rows[agent.pid]["kind"], rows[agent.pid]["blocking"]), ("agent-writer", True))
        self.assertEqual((rows[git.pid]["kind"], rows[git.pid]["blocking"]), ("git-mutation", True))
        self.assertIn("git commit", rows[git.pid]["detail"])
        # An arbitrary background program is not a service to wave through: unknown, blocks.
        self.assertEqual((rows[node.pid]["kind"], rows[node.pid]["blocking"]), ("unknown", True))
        self.assertEqual((rows[plain.pid]["kind"], rows[plain.pid]["blocking"]), ("unknown", True))
        self.assertEqual((rows[editor_proc.pid]["kind"], rows[editor_proc.pid]["blocking"]), ("editor", False))
        self.assertIsNone(rl._git_mutation(["git", "-C", self.co, "status", "--porcelain"]))
        self.assertIsNone(rl._git_mutation(["/usr/bin/git", "log", "--oneline"]))
        self.assertEqual(rl._git_mutation(["git", "-c", "x=y", "rebase", "main"]), "rebase")

    def test_only_explicit_passive_tools_are_non_blocking(self):
        """Classification by command line alone (no tty heuristics): a small documented set of
        editors, language servers, dev servers, idle shells and read-only git are observations;
        every other executable or script, including arbitrary python/node, is unknown and blocks."""
        cases = {
            ("nvim", "main.go"): ("editor", False), ("/usr/bin/vim", "x"): ("editor", False),
            ("/usr/share/code/code", "--type=renderer"): ("editor", False),
            ("gopls", "serve"): ("language-server", False), ("node", "/x/node_modules/.bin/typescript-language-server", "--stdio"): ("language-server", False),
            ("node", "/x/node_modules/typescript-language-server/lib/cli.mjs", "--stdio"): ("unknown", True),
            ("node", "/repo/node_modules/vite/bin/vite.js", "--port", "5173"): ("dev-server", False),
            ("node", "/repo/node_modules/.bin/next", "dev"): ("dev-server", False),
            ("-bash",): ("shell", False), ("zsh", "-l", "-i"): ("shell", False),
            ("bash",): ("shell", False), ("bash", "-i"): ("shell", False), ("bash", "--login"): ("shell", False),
            ("bash", "-s"): ("unknown", True), ("sh", "--stdin"): ("unknown", True),
            ("bash", "-lc", "make deploy"): ("unknown", True), ("bash", "-sc", "x"): ("unknown", True),
            ("bash", "-i", "./scripts/build.sh"): ("unknown", True), ("bash", "--rcfile", "/tmp/rc"): ("unknown", True),
            ("bash", "-c", "while true; do ./deploy.sh; done"): ("unknown", True),
            ("bash", "./scripts/build.sh"): ("unknown", True),
            ("git", "status", "--porcelain"): ("git-readonly", False), ("/usr/bin/git", "-C", "/x", "log", "-1"): ("git-readonly", False),
            ("git", "rebase", "main"): ("git-mutation", True), ("git", "push", "origin", "x"): ("git-mutation", True),
            ("git", "gc"): ("unknown", True), ("git",): ("unknown", True),
            ("python3", "tools/codemod.py"): ("unknown", True), ("python3.12", "-c", "open('a','w')"): ("unknown", True),
            ("node", "scripts/write-files.js"): ("unknown", True), ("node",): ("unknown", True),
            ("/opt/bin/some-tool", "--watch"): ("unknown", True), ("sleep", "60"): ("unknown", True),
            ("claude", "--dangerously-skip-permissions"): ("agent-writer", True),
            ("python3", "-m", "hermes_cli.main", "chat", "-q", "x"): ("agent-writer", True),
            ("nvim-agent-writer-lookalike",): ("unknown", True),
        }
        for cmd, want in cases.items():
            kind, blocking, _ = rl.classify_cmdline(list(cmd))
            self.assertEqual((kind, blocking), want, cmd)
            self.assertEqual(kind in rl.NONBLOCKING_KINDS, not blocking, cmd)
        self.assertEqual(rl.program_name(["node", "/repo/node_modules/vite/bin/vite.js"]), "vite")
        self.assertEqual(rl.program_name(["python3", "-u", "/x/serve.py"]), "serve")
        self.assertEqual(rl.program_name(["python3", "-c", "print(1)"]), "python3 -c")

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
        self.baseline()                                            # clean tree: empty baseline under our lease
        Path(self.co, "a.txt").write_text("2\n")                  # unstaged
        Path(self.co, "new.go").write_text("package x\n")         # untracked
        Path(self.co, "staged.txt").write_text("s\n"); self.g("add", "staged.txt")
        before = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((before["ok"], sorted(before["unknown"])), (False, ["a.txt", "new.go", "staged.txt"]))
        manifest = self.checkpoint()
        self.assertEqual(sorted(manifest["entries"]), ["a.txt", "new.go", "staged.txt"])
        self.assertEqual(sorted(manifest["evidence"]["new_since_baseline"]), ["a.txt", "new.go", "staged.txt"])
        self.assertEqual(manifest["entries"]["staged.txt"]["status"], "A ")
        self.assertTrue(manifest["entries"]["staged.txt"]["index_oid"])
        self.assertEqual(manifest["entries"]["staged.txt"]["index_mode"], "100644")
        self.assertEqual(manifest["entries"]["new.go"]["worktree_mode"], "100644")
        self.assertIsNone(manifest["entries"]["new.go"]["index_oid"])
        text = rl.manifest_path("t_1").read_text()
        self.assertNotIn("package x", text)                        # hashes only, never contents
        self.assertEqual(oct(rl.manifest_path("t_1").stat().st_mode & 0o777), "0o600")
        after = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((after["ok"], sorted(after["known"])), (True, ["a.txt", "new.go", "staged.txt"]))
        Path(self.co, "a.txt").write_text("3\n")
        Path(self.co, "other.txt").write_text("?\n")
        drift = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((drift["ok"], drift["changed"], drift["unknown"]), (False, ["a.txt"], ["other.txt"]))
        self.assertEqual(drift["changed_fields"], {"a.txt": ["worktree_sha256"]})
        self.assertFalse(rl.verify_dirty("t_2", ident, rl.default_git)["ok"])  # another task owns nothing here
        # Same files on another branch are no longer provably this task's.
        self.g("stash", "-u"); self.g("switch", "-c", "feat/y"); self.g("stash", "pop")
        moved = rl.verify_dirty("t_1", self.identity(), rl.default_git)
        self.assertFalse(moved["ok"]); self.assertEqual(moved["known"], [])

    def test_foreign_stage_mode_and_symlink_changes_on_recorded_paths_are_detected(self):
        ident = self.identity()
        self.baseline()
        Path(self.co, "a.txt").write_text("2\n")
        Path(self.co, "tool.sh").write_text("#!/bin/sh\n")
        Path(self.co, "link.txt").write_text("same bytes\n")
        self.checkpoint()
        self.assertTrue(rl.verify_dirty("t_1", ident, rl.default_git)["ok"])
        # Someone stages the recorded unstaged edit: same bytes in the tree, different change.
        self.g("add", "a.txt")
        v = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((v["ok"], v["changed"]), (False, ["a.txt"]))
        self.assertEqual(set(v["changed_fields"]["a.txt"]), {"status", "index_oid"})
        self.g("restore", "--staged", "a.txt")
        self.assertTrue(rl.verify_dirty("t_1", ident, rl.default_git)["ok"])
        # Mode-only change on a recorded untracked file.
        Path(self.co, "tool.sh").chmod(0o755)
        v = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((v["changed"], v["changed_fields"]["tool.sh"]), (["tool.sh"], ["worktree_mode"]))
        Path(self.co, "tool.sh").chmod(0o644)
        # A recorded regular file swapped for a symlink.
        Path(self.co, "target.txt").write_text("same bytes\n")
        Path(self.co, "link.txt").unlink(); Path(self.co, "link.txt").symlink_to("target.txt")
        v = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertIn("link.txt", v["changed"])
        self.assertEqual(set(v["changed_fields"]["link.txt"]), {"worktree_sha256", "worktree_mode"})
        self.assertIn("target.txt", v["unknown"])
        # Staged-then-modified: the staged object is part of the identity too.
        Path(self.co, "link.txt").unlink(); Path(self.co, "target.txt").unlink()
        Path(self.co, "s.txt").write_text("v1\n"); self.g("add", "s.txt")
        self.checkpoint()
        self.g("rm", "--cached", "-q", "s.txt"); Path(self.co, "s.txt").write_text("v1\n"); self.g("add", "s.txt")
        self.assertTrue(rl.verify_dirty("t_1", ident, rl.default_git)["ok"], "identical re-stage is the same change")
        Path(self.co, "s.txt").write_text("v2\n"); self.g("add", "s.txt")
        v = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual(set(v["changed_fields"]["s.txt"]), {"worktree_sha256", "index_oid"})

    def test_checkpoint_is_gated_and_never_launders_foreign_dirty_files(self):
        ident = self.identity()
        Path(self.co, "operator.txt").write_text("the operator's half-done work\n")
        # No lease, no baseline: nothing to checkpoint from.
        with self.assertRaises(rl.LeaseError) as ctx:
            self.checkpoint()
        self.assertIn("live lease", str(ctx.exception))
        self.assertIsNone(rl.read_manifest("t_1"))
        # Preflight cannot pass while a foreign path is dirty, so no baseline exists ...
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=self.passing_runner(), owner=self.owner())
        self.assertFalse(result["ok"]); self.assertEqual(result["lease"]["status"], "acquired")
        self.assertIsNone(rl.read_baseline("t_1"))
        # ... and checkpoint, even with the lease held, refuses instead of adopting it.
        with self.assertRaises(rl.LeaseError) as ctx:
            self.checkpoint()
        self.assertIn("no baseline", str(ctx.exception))
        self.assertIsNone(rl.read_manifest("t_1"))
        # A competing writer at checkpoint time refuses too (lease + baseline present).
        Path(self.co, "operator.txt").unlink()
        self.baseline()
        Path(self.co, "mine.txt").write_text("mine\n")
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "codex").symlink_to(shutil.which("sleep"))
        foreign = self.spawn([str(bindir / "codex"), "60"], cwd=self.co); time.sleep(0.2)
        hits = scan_runner(1, f"MATCH: PID={foreign.pid} CWD={self.co}\nBLOCKED: 1 matching processes.\n")
        with mock.patch.object(rl, "ancestors", lambda pid, limit=64: []):
            with self.assertRaises(rl.LeaseError) as ctx:
                rl.checkpoint("t_1", ident, rl.default_git, owner=self.owner(), scan_runner=hits)
        self.assertIn("agent-writer", str(ctx.exception))
        self.assertIsNone(rl.read_manifest("t_1"))
        # Another run of the same card (another process) holds no lease here: refused.
        other = {**self.owner(), "pid": os.getpid() + 1}
        with self.assertRaises(rl.LeaseError):
            rl.checkpoint("t_1", ident, rl.default_git, owner=other, scan_runner=self.passing_runner())
        # Our own edit since the baseline is adopted with its evidence.
        manifest = self.checkpoint()
        self.assertEqual(list(manifest["entries"]), ["mine.txt"])
        self.assertEqual(manifest["evidence"]["new_since_baseline"], ["mine.txt"])
        self.assertIn("not proof of who wrote", manifest["evidence"]["limits"])
        # A commit moves HEAD: the baseline is bound to it, so a fresh preflight is required;
        # the manifest is not, so a partial commit still resumes the rest (no deadlock).
        Path(self.co, "keep.txt").write_text("still dirty\n")
        self.checkpoint()
        self.g("add", "mine.txt"); self.g("commit", "-qm", "feat: mine")
        Path(self.co, "later.txt").write_text("later\n")
        with self.assertRaises(rl.LeaseError) as ctx:
            self.checkpoint()
        self.assertIn("run preflight again", str(ctx.exception))
        v = rl.verify_dirty("t_1", self.identity(), rl.default_git)
        self.assertEqual((v["known"], v["unknown"]), (["keep.txt"], ["later.txt"]))
        Path(self.co, "later.txt").unlink()
        self.baseline()                                            # keep.txt resumes at the new HEAD
        Path(self.co, "later.txt").write_text("later\n")
        self.assertEqual(sorted(self.checkpoint()["entries"]), ["keep.txt", "later.txt"])
        # The lease tenure changed (released, re-acquired): the old baseline is void.
        rl.release_lease(ident, self.owner())
        rl.acquire_lease(ident, self.owner())
        with self.assertRaises(rl.LeaseError) as ctx:
            self.checkpoint()
        self.assertIn("no baseline", str(ctx.exception))

    def test_adopt_requires_exact_provenance_and_keeps_paths_literal(self):
        ident = self.identity()
        Path(self.co, "a.txt").write_text("2\n")
        Path(self.co, "new.go").write_text("package x\n")
        Path(self.co, ".env.example").write_text("KEY=\n")
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
        # A dotfile is adopted under its literal name; "./" is not a prefix to strip from it.
        src.write_text(json.dumps({"a.txt": good["a.txt"], ".env.example": rl.hash_worktree_path(self.co, ".env.example")}))
        adopted = rl.adopt_manifest("t_1", ident, rl.default_git, str(src))
        self.assertEqual(sorted(adopted["entries"]), [".env.example", "a.txt"])
        self.assertEqual(adopted["unlisted_dirty"], ["new.go"])
        self.assertEqual(adopted["source"], "adopt:preserved-sha256.json")
        verify = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((verify["known"], verify["unknown"]), ([".env.example", "a.txt"], ["new.go"]))
        src.write_text(json.dumps({"./a.txt": good["a.txt"]}))
        with self.assertRaises(rl.LeaseError) as ctx:
            rl.adopt_manifest("t_1", ident, rl.default_git, str(src))
        self.assertIn("not dirty here: ./a.txt", str(ctx.exception))
        with self.assertRaises(rl.LeaseError):
            src.write_text("[]"); rl.adopt_manifest("t_1", ident, rl.default_git, str(src))


# -------------------------------------------------------------- preflight ---

class BoundWorkerTest(TempState):
    """The lease owner comes from the board's worker_bound event of the card's current run, only
    when that interpreter is a per-attempt managed worker and an ancestor of this process with
    the recorded start time; every forged, mismatched or ambiguous binding fails closed."""

    def run_probe(self, probe: str) -> dict:
        root = Path(tempfile.mkdtemp(prefix="managed-worker-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "agent").mkdir()
        (root / "agent" / "__init__.py").write_text("")
        (root / "agent" / "managed_worker.py").write_text(MANAGED_WORKER)
        out = subprocess.run([sys.executable, "-m", "agent.managed_worker"], cwd=root, capture_output=True, text=True,
                             timeout=180, env={**os.environ, "PROBE": probe, "PYTHONPATH": str(root)})
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertTrue(line, out.stdout + out.stderr)
        return json.loads(line[len("RESULT "):])

    def test_genuine_managed_worker_parent_is_the_owner_and_forgeries_fail_closed(self):
        probe = (
            "import os, json, importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('rl', {str(SCRIPT)!r}); rl = importlib.util.module_from_spec(spec); spec.loader.exec_module(rl)\n"
            "P = os.getppid(); ticks = rl.proc_start_ticks(P); ep = rl.instantiation_epoch()\n"
            "def show(task='t_1', pid=P, run=7, status='running', started=None, extra=(), runs=None):\n"
            "    return {'task': {'id': task, 'status': status}, 'runs': runs if runs is not None else\n"
            "            [{'id': run, 'profile': 'default', 'status': 'running', 'ended_at': None}],\n"
            "            'events': [{'kind': 'worker_bound', 'run_id': run, 'payload': {'pid': pid, 'claim_lock': 'l',\n"
            "                        'started_at': started or f'{ep}|{ticks}'}}, *extra]}\n"
            "def attempt(s, **kw):\n"
            "    try:\n"
            "        return rl.bound_worker('t_1', s, **kw)\n"
            "    except rl.IdentityError as e:\n"
            "        return 'refused: ' + str(e)\n"
            "os.environ.update(HERMES_KANBAN_TASK='t_9', HERMES_KANBAN_RUN_ID='99', HERMES_KANBAN_SUPERVISED_WORKER_PID='1')\n"
            "res = {'P': P, 'cmd': rl.proc_cmdline(P), 'ok': attempt(show()),\n"
            "  'not_running': attempt(show(status='ready')),\n"
            "  'wrong_run': attempt(show(runs=[{'id': 8, 'status': 'running'}])),\n"
            "  'no_event': attempt({'task': {'id': 't_1', 'status': 'running'}, 'runs': [{'id': 7, 'status': 'running'}], 'events': []}),\n"
            "  'ticks': attempt(show(started=f'{ep}|{ticks + 1}')),\n"
            "  'epoch': attempt(show(started=f'other-boot:1|{ticks}')),\n"
            "  'unverified': attempt(show(started='unverified')),\n"
            "  'owner_pid': attempt(show(), explicit_pid=os.getpid()),\n"
            "  'owner_pid_ok': attempt(show(), explicit_pid=P),\n"
            "  'owner_admitted': attempt(show(extra=[{'kind': 'owner_admitted', 'run_id': 7, 'payload': {'pid': P}}])),\n"
            "  'sibling': attempt(show(), others=[show(task='t_2', run=3)]),\n"
            "  'sibling_done': attempt(show(), others=[show(task='t_2', run=3, status='done')]),\n"
            "  'other_task': attempt(show(task='t_2'))}\n"
            "print('RESULT ' + json.dumps(res))\n")
        got = self.run_probe(probe)
        self.assertEqual(got["cmd"][1:], ["-m", "agent.managed_worker"])
        ok = got["ok"]
        self.assertEqual((ok["task"], ok["run_id"], ok["pid"]), ("t_1", 7, got["P"]))  # env claims changed nothing
        self.assertEqual(got["owner_pid_ok"]["pid"], got["P"])
        self.assertEqual(got["sibling_done"]["pid"], got["P"], "a sibling that is not running is no binding")
        for case, text in (("not_running", "is ready, not running"), ("wrong_run", "no worker_bound event"),
                           ("no_event", "no worker_bound event"), ("ticks", "another start time"),
                           ("epoch", "another boot"), ("unverified", "no verifiable fingerprint"),
                           ("owner_pid", "is not the bound worker pid"), ("owner_admitted", "owner process"),
                           ("sibling", "running card t_2 is bound to this process chain too"),
                           ("other_task", "not t_1")):
            self.assertIsInstance(got[case], str, case)
            self.assertTrue(got[case].startswith("refused: "), (case, got[case]))
            self.assertIn(text, got[case], case)

    def test_not_an_ancestor_not_a_managed_worker_and_a_nested_worker_fail_closed(self):
        sleeper = self.spawn(["sleep", "60"])
        with self.assertRaisesRegex(rl.IdentityError, "is an ancestor of this process"):
            rl.bound_worker("t_1", bound_show(pid=sleeper.pid))
        with self.assertRaisesRegex(rl.IdentityError, "not a per-attempt managed worker"):
            rl.bound_worker("t_1", bound_show())  # this test's parent is a shell / runner, not a managed worker
        grand = rl.proc_parent(os.getppid())
        if grand and grand > 1:
            managed = lambda pid: ["python3", "-m", rl.MANAGED_WORKER_MODULE]  # noqa: E731
            with self.assertRaisesRegex(rl.IdentityError, "another managed worker"):
                rl.bound_worker("t_1", bound_show(pid=grand), cmdline=managed)
        self.assertTrue(rl.is_managed_worker(["/usr/bin/python3", "-m", "agent.managed_worker"]))
        for cmd in (["hermes", "gateway", "run"], ["python3", "-m", "hermes_cli.main"], [], None):
            self.assertFalse(rl.is_managed_worker(cmd), cmd)

    def test_owner_needs_a_board_identity_never_the_environment(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_RUN_ID": "7",
                                          "HERMES_KANBAN_SUPERVISED_WORKER_PID": str(os.getppid())}):
            with self.assertRaises(rl.IdentityError):
                rl.cli_owner("t_1", hermes=show_runner(bound_show(status="ready")))
            with self.assertRaisesRegex(rl.IdentityError, "show t_1 failed"):
                rl.cli_owner("t_1", hermes=show_runner())
        for name in ("anchor_pid", "owner_from_env", "proc_env_value"):
            self.assertFalse(hasattr(rl, name), name)


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
        (bindir / "nvim").symlink_to(shutil.which("sleep"))
        foreign = self.spawn([str(bindir / "codex"), "60"], cwd=self.co)
        mine = self.spawn([str(bindir / "nvim"), "60"], cwd=self.co)
        time.sleep(0.2)
        hits = scan_runner(1, f"MATCH: PID={mine.pid} CWD={self.co}\nMATCH: PID={foreign.pid} CWD={self.co}\nBLOCKED: 2 matching processes.\n")
        # The run's anchor is an unrelated live process and self_pid is far away, so neither match
        # descends from "us": `sleep` is a plain background process, `codex` a foreign agent.
        anchor = self.spawn(["sleep", "60"])
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=hits,
                              owner=self.owner(pid=anchor.pid), self_pid=10 ** 6)
        self.assertEqual((result["ok"], result["exit"]), (False, 2))
        kinds = {p["pid"]: p["kind"] for p in result["scan"]["processes"]}
        self.assertEqual((kinds[mine.pid], kinds[foreign.pid]), ("editor", "agent-writer"))
        self.assertIn(f"pid {foreign.pid}: agent-writer", next(c["detail"] for c in result["checks"] if c["check"] == "writers"))
        foreign.kill(); foreign.wait()
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=hits,
                              owner=self.owner(pid=anchor.pid), self_pid=10 ** 6)
        self.assertTrue(result["ok"], "a vanished match is not a writer; an editor is an observation")
        self.assertEqual({p["kind"] for p in result["scan"]["processes"]}, {"editor", "gone"})
        plain = self.spawn(["sleep", "60"], cwd=self.co); time.sleep(0.2)
        unknown_hits = scan_runner(1, f"MATCH: PID={plain.pid} CWD={self.co}\nBLOCKED: 1 matching processes.\n")
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=unknown_hits,
                              owner=self.owner(pid=anchor.pid), self_pid=10 ** 6)
        self.assertEqual((result["ok"], result["exit"]), (False, 3), "an arbitrary process is unknown: fail closed")
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
        self.assertTrue(self.pf()["ok"])                           # clean: baseline for the edit round
        Path(self.co, "a.txt").write_text("2\n")
        result = self.pf()
        self.assertEqual(self.names(result, False), ["dirty"])
        self.assertIn("unknown dirty paths", next(c["detail"] for c in result["checks"] if c["check"] == "dirty"))
        self.checkpoint()                                          # same lease tenure: a.txt is this round's edit
        self.assertTrue(self.pf()["ok"])
        Path(self.co, "a.txt").chmod(0o755)
        result = self.pf()
        self.assertIn("changed since checkpoint: a.txt (worktree_mode)", next(c["detail"] for c in result["checks"] if c["check"] == "dirty"))
        Path(self.co, "a.txt").chmod(0o644)
        self.assertFalse(self.pf(task="t_2")["ok"], "another task's run never resumes these files")
        strict = self.pf(require_clean=True)
        self.assertEqual(self.names(strict, False), ["dirty"])
        self.assertEqual(Path(self.co, "a.txt").read_text(), "2\n", "nothing is ever reset or stashed")

    def test_cli_without_a_bound_worker_refuses_before_any_lease(self):
        """No board-recorded bound worker of the card's running run above this process: exit 3
        (capability) before a lease that would die with the CLI process is taken."""
        for show in (bound_show(status="ready"), bound_show(), bound_show(pid=1)):
            for argv in (["preflight", "--task", "t_1", "--checkout", self.co, "--repo", self.url, "--branch", "feat/x"],
                         ["checkpoint", "--task", "t_1", "--checkout", self.co]):
                with mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
                    self.assertEqual(rl.main(argv, scan_runner=self.pass_scan, hermes=show_runner(show)), 3, argv)
                self.assertIn("capability: task-scoped worker identity", err.getvalue())
                self.assertIsNone(rl.read_lease(rl.repo_identity(self.co, rl.default_git)), argv)

    def test_cli_under_a_genuine_managed_worker_takes_the_workers_lease(self):
        """The genuine path, no patching: the CLI runs as the child of a real `python -m
        agent.managed_worker` process that the board records as the run's bound worker; the
        lease is that worker's, and a lease holder bound to the same chain is refused."""
        probe = (
            "import os, json, subprocess, importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('rl', {str(SCRIPT)!r}); rl = importlib.util.module_from_spec(spec); spec.loader.exec_module(rl)\n"
            "P = os.getppid()\n"
            "show = {'task': {'id': 't_1', 'status': 'running'}, 'runs': [{'id': 7, 'status': 'running'}],\n"
            "        'events': [{'kind': 'worker_bound', 'run_id': 7, 'payload': {'pid': P,\n"
            "                    'started_at': f'{rl.instantiation_epoch()}|{rl.proc_start_ticks(P)}'}}]}\n"
            "hermes = lambda argv: subprocess.CompletedProcess(argv, 0, json.dumps(show), '')\n"
            "scan = lambda argv: subprocess.CompletedProcess(argv, 0, 'PASS: No repository CWD matches found.\\n', '')\n"
            f"co, url = {self.co!r}, {self.url!r}\n"
            "codes = [rl.main(['preflight', '--task', 't_1', '--checkout', co, '--repo', url, '--branch', 'feat/x'],\n"
            "                 scan_runner=scan, hermes=hermes),\n"
            "         rl.main(['checkpoint', '--task', 't_1', '--checkout', co], scan_runner=scan, hermes=hermes),\n"
            "         rl.main(['release', '--task', 't_1', '--checkout', co, '--owner-pid', str(os.getpid())], hermes=hermes)]\n"
            "lease = rl.read_lease(rl.repo_identity(co, rl.default_git))\n"
            "codes.append(rl.main(['release', '--task', 't_1', '--checkout', co], hermes=hermes))\n"
            "print('RESULT ' + json.dumps({'codes': codes, 'owner': lease['owner'], 'P': P}))\n")
        got = BoundWorkerTest.run_probe(self, probe)
        self.assertEqual(got["codes"], [0, 0, 3, 0], got)
        self.assertEqual((got["owner"]["pid"], got["owner"]["run_id"], got["owner"]["task"]), (got["P"], 7, "t_1"),
                         "the lease is owned by the bound managed worker, not the CLI")

    def test_cli_round_trip(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "status", "--checkout", self.co], capture_output=True, text=True,
                             env={**os.environ, "HERMES_WORKFLOW_STATE": str(self.state)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["scanner"], ["sudo", "-n", "/bin/true"])
        owner_pid = str(os.getppid())  # the board-bound worker (lease adaptor: its cmdline stands in)
        hermes = show_runner(bound_show())
        adaptor = mock.patch.object(rl, "is_managed_worker", lambda cmd: cmd == rl.proc_cmdline(os.getppid()))
        with adaptor, mock.patch.object(rl, "default_scan_runner", self.pass_scan):
            with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as buf:
                code = rl.main(["preflight", "--task", "t_1", "--checkout", self.co, "--repo", self.url, "--branch", "feat/x",
                                "--owner-pid", owner_pid], hermes=hermes)
            self.assertEqual(code, 0, buf.getvalue())
            self.assertTrue(json.loads(buf.getvalue())["ok"])
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_zzz"}):  # an env claim changes nothing
                with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()):
                    self.assertEqual(rl.main(["checkpoint", "--task", "t_1", "--checkout", self.co], hermes=hermes), 0)
            with mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
                self.assertEqual(rl.main(["release", "--task", "t_1", "--checkout", self.co, "--owner-pid", "4242"],
                                         hermes=hermes), 3)
            self.assertIn("is not the bound worker pid", err.getvalue())
            with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as buf:
                self.assertEqual(rl.main(["release", "--task", "t_1", "--checkout", self.co, "--owner-pid", owner_pid],
                                         hermes=hermes), 0)
        self.assertEqual(json.loads(buf.getvalue())["status"], "released")


if __name__ == "__main__":
    unittest.main()
