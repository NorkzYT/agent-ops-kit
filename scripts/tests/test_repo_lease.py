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
sys.path.insert(0, str(SCRIPTS))
_wf_spec = importlib.util.spec_from_file_location("kanban_workflow_under_test", SCRIPTS / "workflow.py")
wf = importlib.util.module_from_spec(_wf_spec)
_wf_spec.loader.exec_module(wf)

REPO_URL = "https://github.com/acme/app.git"
# The strict (PR28) classification applies only under a granted observer policy. Tests of that
# regime classify with a grant whose VS Code server dir no fixture uses: strict rules apply, no
# terminal is trusted. Without a grant the pre-PR28 classification applies (ObserverGrantScopeTest).
STRICT_GRANT = {"status": "granted", "uid": os.getuid(), "vscode_server_dir": "/nonexistent/strict-regime-server",
                "repository": {"root": "/nonexistent", "gitdir": "/nonexistent/.git", "remote": ""}}


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
        # `/bin/true` stands in for the root helper: bind it to this checkout the way the approved
        # helper's own REPOSITORY declaration binds it to Validate (helper_repository).
        real = rl.helper_repository
        binding = mock.patch.object(rl, "helper_repository", lambda h: self.co if h == "/bin/true" else real(h))
        binding.start()
        self.addCleanup(binding.stop)

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
        shutil.copy(shutil.which("sleep"), bindir / "nvim")  # an image named nvim (a symlink to sleep is a spoof)
        editor_proc = self.spawn([str(bindir / "nvim"), "60"], cwd=self.co)
        plain = self.spawn(["sleep", "60"], cwd=self.co)
        time.sleep(0.5)
        real = rl._root_owned_image  # the user copy stands in for a root-installed editor image
        with mock.patch.object(rl, "_root_owned_image", lambda exe: None if exe == str(bindir / "nvim") else real(exe)):
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
        editors and explicit safe git reads are observations (language servers only via VS Code); shells never are;
        every other executable or script, including arbitrary python/node, is unknown and blocks."""
        cases = {
            ("nvim", "main.go"): ("editor", False), ("/usr/bin/vim", "x"): ("editor", False),
            ("/usr/share/code/code", "--type=renderer"): ("editor", False),
            ("gopls", "serve"): ("unknown", True), ("node", "/x/node_modules/.bin/typescript-language-server", "--stdio"): ("unknown", True),
            ("node", "/x/node_modules/typescript-language-server/lib/cli.mjs", "--stdio"): ("unknown", True),
            ("node", "/repo/node_modules/vite/bin/vite.js", "--port", "5173"): ("unknown", True),
            ("node", "/repo/node_modules/.bin/next", "dev"): ("unknown", True),
            ("-bash",): ("unknown", True), ("zsh", "-l", "-i"): ("unknown", True),
            ("bash",): ("unknown", True), ("bash", "-i"): ("unknown", True), ("bash", "--login"): ("unknown", True),
            ("bash", "-s"): ("unknown", True), ("sh", "--stdin"): ("unknown", True),
            ("bash", "-lc", "make deploy"): ("unknown", True), ("bash", "-sc", "x"): ("unknown", True),
            ("bash", "-i", "./scripts/build.sh"): ("unknown", True), ("bash", "--rcfile", "/tmp/rc"): ("unknown", True),
            ("bash", "-c", "while true; do ./deploy.sh; done"): ("unknown", True),
            ("bash", "./scripts/build.sh"): ("unknown", True),
            ("git", "status", "--porcelain"): ("unknown", True), ("/usr/bin/git", "-C", "/x", "log", "-1"): ("unknown", True),
            ("/usr/bin/git", "-C", "/x", "rev-parse", "HEAD"): ("git-readonly", False),
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
        partial = scan_runner(0, "PASS: Inspected 1 process directories.\n")
        self.assertEqual(rl.run_scan(self.co, partial)["status"], "incomplete", "one PASS marker is not a clean scan")
        runner = scan_runner(0, PASS_LINES)
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
        self.assertEqual((v["changed"], v["changed_fields"]["tool.sh"]), (["tool.sh"], ["worktree_mode", "worktree_perm"]))
        Path(self.co, "tool.sh").chmod(0o644)
        # A recorded regular file swapped for a symlink.
        Path(self.co, "target.txt").write_text("same bytes\n")
        Path(self.co, "link.txt").unlink(); Path(self.co, "link.txt").symlink_to("target.txt")
        v = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertIn("link.txt", v["changed"])
        self.assertEqual(set(v["changed_fields"]["link.txt"]), {"worktree_sha256", "worktree_mode", "worktree_type", "worktree_perm"})
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

    def inventory(self, **over):
        """The strict operator inventory: task/root/gitdir/remote/branch/head/status_sha256 (+ index_sha256)
        and files[{path, status, type, mode, sha256, index_oid, index_mode, origin}], computed here
        independently of repo_lease (raw git and lstat)."""
        ident = self.identity()
        status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=self.co,
                                capture_output=True, check=True).stdout
        files = []
        parts, i = status.split(b"\0"), 0
        while i < len(parts):
            rec = parts[i].decode(); i += 1
            if not rec:
                continue
            code, rel, origin = rec[:2], rec[3:], None
            if code[0] in "RC" or code[1] in "RC":
                origin = parts[i].decode(); i += 1
            full = Path(self.co, rel)
            staged = subprocess.run(["git", "ls-files", "-s", "-z", "--", rel], cwd=self.co, capture_output=True,
                                    check=True, text=True).stdout.split("\0")[0]
            imode, ioid = (staged.split("\t", 1)[0].split()[:2] if staged else (None, None))
            if not os.path.lexists(full):
                digest, kind, mode = None, None, None
            elif full.is_symlink():
                digest, kind, mode = rl.hashlib.sha256(os.readlink(full).encode()).hexdigest(), "symlink", os.lstat(full).st_mode & 0o777
            else:
                digest, kind, mode = rl.hashlib.sha256(full.read_bytes()).hexdigest(), "file", os.lstat(full).st_mode & 0o777
            files.append({"path": rel, "status": code, "type": kind, "mode": mode, "sha256": digest,
                          "index_oid": ioid, "index_mode": imode, "origin": origin})
        data = {"task": "t_1", "role": "pre-existing operator-authorized baseline inventory", "root": ident["root"],
                "gitdir": ident["gitdir"], "remote": ident["remote"], "branch": ident["branch"], "head": ident["head"],
                "status_sha256": rl.hashlib.sha256(status).hexdigest(),
                "index_sha256": rl.hashlib.sha256(Path(ident["gitdir"], "index").read_bytes()).hexdigest(),
                "files": files}
        data.update(over)
        src = self.tmp / "validate-baseline.json"
        src.write_text(json.dumps(data))
        return src, data

    def adopt(self, src, task="t_1", owner=None, runner=None, git=None):
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        return rl.adopt_manifest(task, self.identity(), git or rl.default_git, str(src), owner=owner or self.owner(task=task),
                                 scan_runner=runner or self.passing_runner())

    def dirty_fixture(self):
        Path(self.co, "a.txt").write_text("2\n")                     # modified
        Path(self.co, "new.go").write_text("package x\n")            # untracked
        Path(self.co, "tool.sh").write_text("#!/bin/sh\n"); os.chmod(Path(self.co, "tool.sh"), 0o755)
        os.symlink("new.go", Path(self.co, "link.go"))                # untracked symlink

    def test_adopt_inventory_format_resumes_the_pre_existing_baseline(self):
        self.dirty_fixture()
        src, data = self.inventory()
        adopted = self.adopt(src)
        self.assertEqual(sorted(adopted["entries"]), ["a.txt", "link.go", "new.go", "tool.sh"])
        self.assertEqual(adopted["unlisted_dirty"], [])
        self.assertEqual(adopted["entries"]["link.go"]["worktree_mode"], "120000")
        self.assertEqual(adopted["provenance"]["source_sha256"], rl.hashlib.sha256(src.read_bytes()).hexdigest())
        self.assertEqual(adopted["provenance"]["head"], data["head"])
        verify = rl.verify_dirty("t_1", self.identity(), rl.default_git)
        self.assertTrue(verify["ok"], verify)
        # Preflight then passes and records the edit-round baseline; new work after it is
        # distinguishable from the adopted pre-existing work.
        self.baseline()
        Path(self.co, "coordinator.go").write_text("package y\n")
        Path(self.co, "a.txt").write_text("3\n")
        cp = self.checkpoint()
        self.assertEqual(cp["evidence"]["new_since_baseline"], ["coordinator.go"])
        self.assertEqual(cp["evidence"]["changed_since_baseline"], ["a.txt"])
        self.assertEqual(sorted(cp["evidence"]["kept"]), ["link.go", "new.go", "tool.sh"])

    def test_adopt_inventory_refuses_any_drift_from_what_was_authorized(self):
        self.dirty_fixture()
        ident = self.identity()

        def refused(src, needle):
            with self.assertRaises(rl.LeaseError) as ctx:
                self.adopt(src)
            self.assertIn(needle, str(ctx.exception))
            self.assertIsNone(rl.read_manifest("t_1"))

        src, _ = self.inventory(task="t_other")
        refused(src, "inventory is for task t_other")
        src, _ = self.inventory(branch="feat/other")
        refused(src, "branch")
        src, _ = self.inventory(head="0" * 40)
        refused(src, "HEAD")
        src, _ = self.inventory(status_sha256="0" * 64)
        refused(src, "git status")
        # Content changed after the inventory (status unchanged).
        src, _ = self.inventory()
        Path(self.co, "new.go").write_text("package tampered\n")
        refused(src, "content differs: new.go")
        Path(self.co, "new.go").write_text("package x\n")
        # A new dirty path appeared after the inventory: the recorded git status no longer matches.
        src, _ = self.inventory()
        Path(self.co, "late.go").write_text("package late\n")
        refused(src, "git status")
        os.unlink(Path(self.co, "late.go"))
        # Mode or status recorded differently from the checkout.
        src, data = self.inventory()
        for f in data["files"]:
            if f["path"] == "tool.sh":
                f["mode"] = 0o644
        src.write_text(json.dumps(data))
        refused(src, "mode differs: tool.sh")
        src, data = self.inventory()
        for f in data["files"]:
            if f["path"] == "a.txt":
                f["status"] = "M "
        src.write_text(json.dumps(data))
        refused(src, "status differs: a.txt")
        # Duplicate or malformed rows.
        src, data = self.inventory()
        data["files"].append(dict(data["files"][0]))
        src.write_text(json.dumps(data))
        refused(src, "duplicate")
        src, data = self.inventory()
        data["files"][0] = {"path": data["files"][0]["path"]}
        src.write_text(json.dumps(data))
        refused(src, "sha256")
        # A live lease of another task on the checkout: not adopted under someone else's ownership.
        rl.acquire_lease(ident, self.owner(task="t_other"))
        src, _ = self.inventory()
        refused(src, "lease")

    def test_adopt_requires_exact_provenance_and_keeps_paths_literal(self):
        ident = self.identity()
        Path(self.co, "a.txt").write_text("2\n")
        Path(self.co, "new.go").write_text("package x\n")
        Path(self.co, ".env.example").write_text("KEY=\n")
        good = {"a.txt": rl.hash_worktree_path(self.co, "a.txt"), "new.go": rl.hash_worktree_path(self.co, "new.go")}
        src = self.tmp / "preserved-sha256.json"
        src.write_text(json.dumps({**good, "new.go": "0" * 64}))
        with self.assertRaises(rl.LeaseError) as ctx:
            self.adopt(src)
        self.assertIn("content differs: new.go", str(ctx.exception))
        self.assertIsNone(rl.read_manifest("t_1"))
        src.write_text(json.dumps({**good, "ghost.txt": "1" * 64}))
        with self.assertRaises(rl.LeaseError) as ctx:
            self.adopt(src)
        self.assertIn("not dirty here: ghost.txt", str(ctx.exception))
        # A dotfile is adopted under its literal name; "./" is not a prefix to strip from it.
        src.write_text(json.dumps({"a.txt": good["a.txt"], ".env.example": rl.hash_worktree_path(self.co, ".env.example")}))
        adopted = self.adopt(src)
        self.assertEqual(sorted(adopted["entries"]), [".env.example", "a.txt"])
        self.assertEqual(adopted["unlisted_dirty"], ["new.go"])
        self.assertEqual(adopted["source"], "adopt:preserved-sha256.json")
        verify = rl.verify_dirty("t_1", ident, rl.default_git)
        self.assertEqual((verify["known"], verify["unknown"]), ([".env.example", "a.txt"], ["new.go"]))
        src.write_text(json.dumps({"./a.txt": good["a.txt"]}))
        with self.assertRaises(rl.LeaseError) as ctx:
            self.adopt(src)
        self.assertIn("not dirty here: ./a.txt", str(ctx.exception))
        with self.assertRaises(rl.LeaseError):
            src.write_text("[]"); self.adopt(src)

    def refuse(self, src, needle, exc=None, **kw):
        with self.assertRaises(exc or rl.LeaseError) as ctx:
            self.adopt(src, **kw)
        self.assertIn(needle, str(ctx.exception))
        self.assertIsNone(rl.read_manifest(kw.get("task", "t_1")))
        return ctx.exception

    def test_inventory_must_bind_every_field_and_the_original_shape_is_refused(self):
        """Reviewer P2: an inventory without status/type/mode/status_sha256 (or root/gitdir/remote,
        index_oid/index_mode/origin) used to be adopted. Every binding is required now; the
        pre-existing 5-field shape is refused (a richer successor must be produced), never upgraded."""
        self.dirty_fixture()
        src, data = self.inventory()
        original = {k: data[k] for k in ("task", "role", "head", "branch", "status_sha256", "index_sha256")}
        original["files"] = [{k: f[k] for k in ("path", "status", "sha256", "mode", "type")} for f in data["files"]]
        src.write_text(json.dumps(original))
        before = src.read_bytes()
        self.refuse(src, "root")
        self.assertEqual(src.read_bytes(), before, "the source inventory is never rewritten")
        for key in ("root", "gitdir", "remote", "status_sha256", "branch", "head"):
            src, data = self.inventory()
            del data[key]
            src.write_text(json.dumps(data))
            self.refuse(src, key)
        for key in ("status", "type", "mode", "sha256", "index_oid", "index_mode", "origin"):
            src, data = self.inventory()
            for f in data["files"]:
                del f[key]
            src.write_text(json.dumps(data))
            self.refuse(src, key)
        src, data = self.inventory(gitdir="/elsewhere/.git")
        self.refuse(src, "gitdir")
        src, data = self.inventory(remote="https://github.com/other/repo.git")
        self.refuse(src, "remote")
        src, data = self.inventory(root="/elsewhere")
        self.refuse(src, "root")
        src, data = self.inventory()
        data["files"][0]["comment"] = "ignored?"
        src.write_text(json.dumps(data))
        self.refuse(src, "comment")
        # Actual mode, not just the executable bit: 0700 is not the recorded 0755.
        src, _ = self.inventory()
        os.chmod(Path(self.co, "tool.sh"), 0o700)
        self.refuse(src, "mode differs: tool.sh")
        os.chmod(Path(self.co, "tool.sh"), 0o755)
        self.assertEqual(sorted(self.adopt(self.inventory()[0])["entries"]), ["a.txt", "link.go", "new.go", "tool.sh"])

    def test_inventory_binds_the_index_and_a_foreign_git_add_is_refused(self):
        """Reviewer P1/P3: a foreign `git add` of other content that keeps the MM status was adopted
        (index object not compared) and index_sha256 was ignored. Both are bound now."""
        Path(self.co, "a.txt").write_text("staged\n"); self.g("add", "a.txt")
        Path(self.co, "a.txt").write_text("worktree\n")
        src, data = self.inventory()
        self.assertEqual(data["files"][0]["status"], "MM")
        Path(self.co, "a.txt").write_text("FOREIGN\n"); self.g("add", "a.txt")
        Path(self.co, "a.txt").write_text("worktree\n")
        status_now = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=self.co,
                                    capture_output=True).stdout
        self.assertEqual(rl.hashlib.sha256(status_now).hexdigest(), data["status_sha256"], "status alone cannot see it")
        del data["index_sha256"]
        src.write_text(json.dumps(data))
        self.refuse(src, "index differs: a.txt")
        src, data = self.inventory()
        self.refuse(self.inventory(index_sha256="0" * 64)[0], "index_sha256")
        src, data = self.inventory()
        data["files"][0]["index_sha256"] = "0" * 64
        src.write_text(json.dumps(data))
        self.refuse(src, "index_sha256 differs: a.txt")
        src, data = self.inventory()
        data["files"][0]["index_sha256"] = rl.hashlib.sha256(b"FOREIGN\n").hexdigest()
        src.write_text(json.dumps(data))
        adopted = self.adopt(src)
        self.assertEqual(adopted["entries"]["a.txt"]["index_oid"], data["files"][0]["index_oid"])
        self.assertEqual(adopted["provenance"]["index_sha256"], data["index_sha256"])

    def test_inventory_binds_the_rename_origin(self):
        Path(self.co, "b.txt").write_text("other\n"); self.g("add", "b.txt"); self.g("commit", "-m", "chore: b")
        self.g("mv", "a.txt", "c.txt")
        src, data = self.inventory()
        row = next(f for f in data["files"] if f["path"] == "c.txt")
        self.assertEqual((row["status"][0], row["origin"]), ("R", "a.txt"))
        row["origin"] = "b.txt"
        src.write_text(json.dumps(data))
        self.refuse(src, "origin differs: c.txt")
        self.assertEqual(self.adopt(self.inventory()[0])["entries"]["c.txt"]["origin"], "a.txt")

    def test_map_compatibility_cannot_launder_the_inventory_format(self):
        """The old {path: sha256} map binds content only, so it adopts no staged change, and a document
        that looks like an inventory (or this module's format) is never read as a map."""
        Path(self.co, "a.txt").write_text("staged\n"); self.g("add", "a.txt")
        src = self.tmp / "map.json"
        src.write_text(json.dumps({"a.txt": rl.hash_worktree_path(self.co, "a.txt")}))
        self.refuse(src, "staged")
        _, data = self.inventory()
        for bad in ({**data, "files": {"a.txt": data["files"][0]["sha256"]}},
                    {"task": "t_1", "a.txt": rl.hash_worktree_path(self.co, "a.txt")},
                    {"a.txt": {"sha256": rl.hash_worktree_path(self.co, "a.txt")}}):
            src.write_text(json.dumps(bad))
            self.refuse(src, "not adopted")
        # This module's own format compares the whole entry identity, not only the worktree hash.
        entry = dict(rl.snapshot_dirty(rl.default_git, self.identity())["a.txt"])
        src.write_text(json.dumps({"entries": {"a.txt": {**entry, "index_oid": "0" * 40}}}))
        self.refuse(src, "index differs: a.txt")
        src.write_text(json.dumps({"entries": {"a.txt": entry}}))
        self.assertEqual(sorted(self.adopt(src)["entries"]), ["a.txt"])

    def test_adopt_needs_the_bound_owner_and_no_other_live_lease_even_of_the_same_task(self):
        """Reviewer P5: another live run of the same task held the lease and its baseline was dropped.
        adopt needs this run's bound worker identity, refuses any live lease that is not exactly this
        run's (same task included), never acquires a lease, and keeps the other run's baseline."""
        self.dirty_fixture()
        src, _ = self.inventory()
        ident = self.identity()
        with self.assertRaises(rl.IdentityError):
            rl.adopt_manifest("t_1", ident, rl.default_git, str(src), owner=None, scan_runner=self.passing_runner())
        other = self.owner(run="8")
        self.assertEqual(rl.acquire_lease(ident, other)["status"], "acquired")
        rl.write_baseline("t_1", ident, other, rl.read_lease(ident), {})
        self.refuse(src, "lease")
        self.assertIsNotNone(rl.read_baseline("t_1"), "the other run's baseline is untouched")
        self.refuse(src, "lease", owner=self.owner(task="t_2"), task="t_2")
        # A dead holder: adopt proceeds but never takes the lease.
        dead = self.spawn(["sleep", "60"]); dead.kill(); dead.wait()
        rl.release_lease(ident, other)
        self.assertEqual(rl.acquire_lease(ident, self.owner(run="9", pid=os.getppid()))["status"], "acquired")
        lease = rl.read_lease(ident)
        lease["owner"].update(pid=dead.pid)
        rl._atomic_replace(rl.leases_dir(), rl.lease_path(ident).name, lease)
        self.assertEqual(sorted(self.adopt(src)["entries"]), ["a.txt", "link.go", "new.go", "tool.sh"])
        self.assertEqual(rl.read_lease(ident)["owner"]["pid"], dead.pid, "adopt took no lease")
        # Exactly this run's live lease: fine.
        rl.release_lease(ident, rl.read_lease(ident)["owner"])
        rl.acquire_lease(ident, self.owner())
        self.assertEqual(sorted(self.adopt(src)["entries"]), ["a.txt", "link.go", "new.go", "tool.sh"])

    def test_adopt_scans_for_writers_under_the_repo_lock_before_changing_provenance(self):
        self.dirty_fixture()
        src, _ = self.inventory()
        ident = self.identity()
        held = []

        def probing(argv):
            fd = os.open(rl.leases_dir() / f"{rl.repo_key(ident)}.lock", os.O_RDWR)
            try:
                rl.fcntl.flock(fd, rl.fcntl.LOCK_EX | rl.fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return done(0, self.PASS_SCAN)

        self.adopt(src, runner=probing)
        self.assertEqual(held, [True], "the writer scan runs while the repository lock is held")
        os.unlink(rl.manifest_path("t_1"))
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "codex").symlink_to(shutil.which("sleep"))
        # A foreign agent that does not descend from this run (orphaned via a background job).
        out = subprocess.run(["sh", "-c", f"'{bindir / 'codex'}' 60 </dev/null >/dev/null 2>&1 & echo $!"], cwd=self.co,
                             capture_output=True, text=True, check=True).stdout
        agent = int(out.split()[0])
        self.addCleanup(VSCodeServerTest._kill_pid, agent)
        time.sleep(0.2)
        self.refuse(src, "codex", runner=scan_runner(1, f"MATCH: PID={agent} CWD={self.co}\nBLOCKED: 1 matching processes.\n"))
        self.refuse(src, "incomplete", runner=scan_runner(2, "FAIL: 1 inspection errors.\n"))

    def test_status_binding_uses_the_injected_git_and_ignores_inherited_repo_env(self):
        """Reviewer P6: the status digest ran a raw subprocess (ignoring the injected runner) that
        inherited GIT_INDEX_FILE. It goes through the runner; the default runner drops the
        repository-redirecting git variables."""
        self.dirty_fixture()
        src, _ = self.inventory()
        calls = []

        def g(argv, cwd):
            calls.append(argv[0])
            return rl.default_git(argv, cwd)

        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.tmp / "alt-index"), "GIT_DIR": str(self.tmp)}):
            adopted = self.adopt(src, git=g)
        self.assertEqual(sorted(adopted["entries"]), ["a.txt", "link.go", "new.go", "tool.sh"])
        self.assertIn("status", calls)

# ------------------------------------------------- VS Code Remote server ---

# A synthetic VS Code Remote server tree with the real layout: `server/node` is a copy of this
# interpreter (so /proc/<pid>/exe resolves to the server's own binary, as with the real node),
# `out/bootstrap-fork --type=extensionHost|ptyHost` spawns the real child command lines.
BOOTSTRAP_FORK = r"""
import fcntl, json, os, subprocess, sys, termios, threading, time
server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
node = os.path.join(server, "node")
kind = sys.argv[1]
cwd = os.environ["FIXTURE_CWD"]
if kind == "--type=extensionHost":
    child = subprocess.Popen([node, os.path.join(server, os.environ["FIXTURE_LS"]), "--node-ipc",
                              "--clientProcessId=%s" % os.environ.get("FIXTURE_CLIENT", os.getpid())], cwd=cwd)
    print(child.pid, flush=True)
    child.wait()
elif kind == "--type=ptyHost":
    master, slave = os.openpty()
    def ctty():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    init = os.path.join(server, "out/vs/workbench/contrib/terminal/common/scripts/shellIntegration-bash.sh")
    argv = json.loads(os.environ["FIXTURE_ARGV"]) if "FIXTURE_ARGV" in os.environ else [os.environ["FIXTURE_BASH"], "--init-file", init]
    child = subprocess.Popen(argv, cwd=cwd, stdin=slave, stdout=slave,
                             stderr=slave, preexec_fn=ctty, env={"PATH": os.environ["PATH"], "HOME": cwd, "TERM": "dumb"})
    os.close(slave)
    def drain():
        while True:
            try:
                if not os.read(master, 4096):
                    return
            except OSError:
                return
    threading.Thread(target=drain, daemon=True).start()
    print(child.pid, flush=True)
    for line in sys.stdin:
        os.write(master, line.encode())
    child.wait()
"""
SLEEPER = "import time\ntime.sleep(120)\n"


class VSCodeServerTest(TempRepo):
    """VS Code Remote-SSH components with the checkout as CWD: the bundled language servers are
    passive only when /proc proves it (exact entry script, the server's own node binary, parented
    by that server's extension host that is also their --clientProcessId). The integrated
    terminal's bash is identified but always blocks: no /proc sample proves a shell will not run
    code (traps, PROMPT_COMMAND, read -t). Every other claim stays unknown."""

    HASH = "2242ebbb54efeeb0129e08e919e7e8d43033cd83"
    JSON_LS = "extensions/json-language-features/server/dist/node/jsonServerMain"
    MD_LS = "extensions/markdown-language-features/dist/serverWorkerMain"

    def setUp(self):
        super().setUp()
        self.server = self.tmp / "home" / ".vscode-server" / "cli" / "servers" / f"Stable-{self.HASH}" / "server"
        (self.server / "out").mkdir(parents=True)
        self.node = self.server / "node"
        shutil.copy2(os.path.realpath(sys.executable), self.node)
        (self.server / "out" / "bootstrap-fork").write_text(BOOTSTRAP_FORK)
        integ = self.server / "out/vs/workbench/contrib/terminal/common/scripts/shellIntegration-bash.sh"
        integ.parent.mkdir(parents=True)
        integ.write_text("PS1='$ '\n")
        for rel in (self.JSON_LS, self.MD_LS, "extensions/evil/agent"):
            (self.server / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.server / rel).write_text(SLEEPER)
        self.bash = os.path.realpath(shutil.which("bash"))

    def host(self, kind, **env):
        """Start `node out/bootstrap-fork --type=<kind>`; returns (host process, child pid)."""
        full = {**os.environ, "FIXTURE_CWD": self.co, "FIXTURE_LS": self.JSON_LS, "FIXTURE_BASH": self.bash, **env}
        p = subprocess.Popen([str(self.node), "-u", str(self.server / "out" / "bootstrap-fork"),
                              f"--type={kind}"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=full, cwd=str(self.tmp), text=True)
        self.procs.append(p)
        child = int(p.stdout.readline())
        self.addCleanup(self._kill_pid, child)
        return p, child

    @staticmethod
    def _kill_pid(pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    def classify(self, pid):
        return rl.classify_process(pid, self_pid=10 ** 6, anchors=[], self_chain=set(), grant=STRICT_GRANT)

    def wait_for(self, pid, kind, blocking, timeout=10.0):
        deadline = time.time() + timeout
        row = self.classify(pid)
        while (row["kind"], row["blocking"]) != (kind, blocking) and time.time() < deadline:
            time.sleep(0.1)
            row = self.classify(pid)
        return row

    def test_bundled_language_servers_under_their_extension_host_are_passive(self):
        for ls in (self.JSON_LS, self.MD_LS):
            _, child = self.host("extensionHost", FIXTURE_LS=ls)
            row = self.wait_for(child, "language-server", False)
            self.assertEqual((row["kind"], row["blocking"]), ("language-server", False), (ls, row))
            self.assertIn("VS Code", row["detail"])
        # By command line alone the same process stays unknown: only /proc ancestry decides.
        cmd = [str(self.node), str(self.server / self.JSON_LS), "--node-ipc", "--clientProcessId=1"]
        self.assertEqual(rl.classify_cmdline(cmd)[:2], ("unknown", True))

    def test_language_server_claims_that_proc_does_not_back_stay_unknown(self):
        # Not parented by the extension host (started directly from here).
        direct = self.spawn([str(self.node), str(self.server / self.JSON_LS), "--node-ipc",
                             f"--clientProcessId={os.getpid()}"], cwd=self.co)
        # The server's command line, but another binary (argv[0] forged, exe is the system python).
        forged = self.spawn([str(self.node), str(self.server / self.JSON_LS), "--node-ipc",
                             f"--clientProcessId={os.getpid()}"], executable=os.path.realpath(sys.executable), cwd=self.co)
        # Under the genuine extension host but not a bundled language server.
        _, evil = self.host("extensionHost", FIXTURE_LS="extensions/evil/agent")
        # The genuine entry script, but --clientProcessId names another process than its parent.
        _, wrong_client = self.host("extensionHost", FIXTURE_CLIENT="1")
        time.sleep(0.5)
        for pid in (direct.pid, forged.pid, evil, wrong_client):
            row = self.classify(pid)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)

    def test_integrated_terminal_is_identified_and_blocks_at_its_prompt_and_running_a_command(self):
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        row = self.classify(shell)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        self.assertIn("VS Code integrated terminal (bash) under pty host", row["detail"])
        self.assertEqual(rl.classify_cmdline(rl.proc_cmdline(shell))[:2], ("unknown", True))
        for line in ("sleep 30\n", "\x03sleep 30 &\n"):
            host.stdin.write(line); host.stdin.flush()
            time.sleep(0.3)
            self.assertEqual(self.classify(shell)["blocking"], True)

    def test_integrated_terminal_claims_that_proc_does_not_back_stay_unknown(self):
        init = str(self.server / "out/vs/workbench/contrib/terminal/common/scripts/shellIntegration-bash.sh")
        # Same command line, not started by the server's pty host (no tty either).
        direct = subprocess.Popen([self.bash, "--init-file", init], cwd=self.co, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(direct)
        # A different init file is an ordinary shell with arguments: unknown.
        other = self.tmp / "rc.sh"
        other.write_text("PS1='$ '\n")
        cases = [direct.pid]
        time.sleep(0.3)
        for pid in cases:
            row = self.classify(pid)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        self.assertEqual(rl.classify_cmdline([self.bash, "--init-file", str(other)])[:2], ("unknown", True))

    def assert_never_idle(self, shell, samples):
        rows = [self.classify(shell) for _ in range(samples)]
        passive = [r for r in rows if not r["blocking"]]
        self.assertEqual(passive, [], f"{len(passive)}/{samples} samples read as passive")
        self.assertEqual({r["kind"] for r in rows}, {"unknown"})

    def test_busy_builtin_writer_loop_in_the_terminal_is_never_idle(self):
        """Reviewer V1: a builtin-only write loop has no child and owns the terminal."""
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        host.stdin.write("while :; do echo x >> busy.txt; : > other.txt; done\n"); host.stdin.flush()
        time.sleep(0.5)
        self.assert_never_idle(shell, 20)
        self.assertGreater(os.path.getsize(Path(self.co, "busy.txt")), 0)

    def test_loop_between_short_children_is_never_idle(self):
        """Reviewer V2: the gap between two short children of a loop read as idle (45/300)."""
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        host.stdin.write("while :; do /bin/true; printf y >> loop.txt; done\n"); host.stdin.flush()
        time.sleep(0.5)
        self.assert_never_idle(shell, 40)

    def test_read_timeout_builtin_loop_is_never_idle(self):
        """A loop that waits on the tty with a timeout looks like a prompt at any instant."""
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        host.stdin.write("while :; do read -t 0.02 x; printf y >> r.txt; done\n"); host.stdin.flush()
        time.sleep(0.5)
        self.assert_never_idle(shell, 10)

    def test_name_only_shell_needs_the_same_idle_proof(self):
        """`bash`/`bash -i` by name alone is no bypass: at its prompt, running a builtin loop, or
        without a terminal, it is unknown."""
        host, shell = self.host("ptyHost", FIXTURE_ARGV=json.dumps([self.bash, "-i"]))
        self.prompt(host, shell)
        row = self.classify(shell)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        host.stdin.write("while :; do echo x >> plain.txt; done\n"); host.stdin.flush()
        time.sleep(0.5)
        self.assert_never_idle(shell, 10)
        notty = subprocess.Popen([self.bash], cwd=self.co, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.procs.append(notty)
        time.sleep(0.3)
        row = self.classify(notty.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)

    def prompt(self, host, shell):
        """Wait until bash sits at its prompt (has read its init file and is waiting on the tty)."""
        host.stdin.write("echo ready > .ready\n"); host.stdin.flush()
        deadline = time.time() + 10
        while not Path(self.co, ".ready").exists() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)

    def test_read_timeout_then_write_is_never_passive(self):
        """Reviewer r3: `read -t 3; echo >> file` sleeps in a tty wait with no child, stable CPU and
        context switches across any short window, then writes. Samples cannot tell it from a prompt."""
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        host.stdin.write("read -t 3 x; echo late >> late.txt\n"); host.stdin.flush()
        time.sleep(0.4)
        row = self.classify(shell)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        deadline = time.time() + 10
        while not Path(self.co, "late.txt").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(Path(self.co, "late.txt").exists(), "the 'idle-looking' shell did write")

    def test_trap_and_prompt_command_writers_at_the_prompt_are_never_passive(self):
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        host.stdin.write("trap 'echo t >> trap.txt' USR1; PROMPT_COMMAND='echo p >> pc.txt'\n"); host.stdin.flush()
        time.sleep(0.5)
        row = self.classify(shell)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        os.kill(shell, 10)
        host.stdin.write("\n"); host.stdin.flush()
        deadline = time.time() + 10
        while not (Path(self.co, "trap.txt").exists() and Path(self.co, "pc.txt").exists()) and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(Path(self.co, "pc.txt").exists(), "PROMPT_COMMAND wrote from the prompt")
        self.assertTrue(Path(self.co, "trap.txt").exists(), "the trap wrote from the prompt")

    def test_a_shell_at_its_prompt_is_unknown_with_an_actionable_reason(self):
        """No /proc evidence proves a shell will not run code (traps, PROMPT_COMMAND, read -t): every
        interactive shell whose CWD is the checkout blocks, the VS Code one included."""
        for argv in (None, [self.bash, "-i"]):
            host, shell = self.host("ptyHost", **({"FIXTURE_ARGV": json.dumps(argv)} if argv else {}))
            time.sleep(1.0)
            row = self.classify(shell)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
            self.assertIn("cd out of", row["detail"])
        self.assertNotIn("shell", rl.NONBLOCKING_KINDS)

    def test_agents_and_git_inside_the_terminal_still_block(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "claude").symlink_to(shutil.which("sleep"))
        host, shell = self.host("ptyHost")
        self.prompt(host, shell)
        host.stdin.write(f"{bindir / 'claude'} 30\n"); host.stdin.flush()
        deadline = time.time() + 10
        kids = []
        while not kids and time.time() < deadline:
            time.sleep(0.1)
            kids = rl.proc_children(shell) or []
        self.assertTrue(kids)
        self.assertEqual(self.classify(shell)["blocking"], True)
        self.assertEqual((self.classify(kids[0])["kind"], self.classify(kids[0])["blocking"]), ("agent-writer", True))
        scan = rl.classify_scan({"status": "complete", "scanner": "x",
                                 "matches": [{"pid": shell, "cwd": self.co}, {"pid": kids[0], "cwd": self.co}]},
                                self_pid=10 ** 6)  # the fixture descends from this test process
        self.assertFalse(scan["ok"])


# ------------------------------------------ trusted VS Code observer policy ---

SERVER_MAIN = r"""
import os, subprocess, sys
server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
child = subprocess.Popen([os.path.join(server, "node"), "-u", os.path.join(server, "out", "bootstrap-fork"),
                          "--type=ptyHost", "--logsPath", "/nonexistent"])
child.wait()
"""


class _FifoHost:
    def __init__(self, fd):
        self.stdin = os.fdopen(fd, "w")


class TrustedTerminalPolicyTest(VSCodeServerTest):
    """The operator accepts the risk of their own VS Code integrated terminal in one checkout on one
    host (observer policy). Only the real chain passes: the server's bin/code-server launcher ->
    server-main.js -> pty host -> bash with the server's init file, session leader of a pty the pty
    host holds, root-owned bash and launcher images, the policy uid, no agent ancestor. The fixture
    launches that chain detached, as the real server runs (reparented away from this test)."""

    def setUp(self):
        super().setUp()
        (self.server / "out" / "server-main.js").write_text(SERVER_MAIN)
        (self.server / "bin").mkdir()
        self.launcher = self.server / "bin" / "code-server"
        self.launcher.write_text(f'#!/bin/sh\n"{self.node}" "{self.server}/out/server-main.js" "$@"\n')
        self.launcher.chmod(0o755)
        self.n = 0

    def launch(self, under=None, **env):
        """Start `sh <server>/bin/code-server` detached (or as a child of *under*, an argv prefix
        that keeps running); returns (host with .stdin, bash pid)."""
        self.n += 1
        fifo, pidf = self.tmp / f"in-{self.n}", self.tmp / f"pid-{self.n}"
        os.mkfifo(fifo)
        full = {**os.environ, "FIXTURE_CWD": self.co, "FIXTURE_LS": self.JSON_LS, "FIXTURE_BASH": self.bash, **env}
        if under:
            argv = [*under, '/bin/sh "$1" <"$2" >"$3" 2>/dev/null; true', "x", str(self.launcher), str(fifo), str(pidf)]
            self.spawn(argv, env=full, cwd=str(self.tmp))
        else:
            subprocess.run(["/bin/sh", "-c", 'setsid /bin/sh "$1" <"$2" >"$3" 2>/dev/null &', "x", str(self.launcher),
                            str(fifo), str(pidf)], env=full, cwd=str(self.tmp), check=True)
        host = _FifoHost(os.open(fifo, os.O_WRONLY))
        self.addCleanup(host.stdin.close)
        deadline = time.time() + 15
        while time.time() < deadline and not (pidf.exists() and pidf.read_text().strip()):
            time.sleep(0.05)
        shell = int(pidf.read_text().split()[0])
        chain = [shell] + rl.ancestors(shell)[:3]  # pty host, server-main, launcher
        for pid in chain:
            self.addCleanup(self._kill_pid, pid)
        return host, shell

    def write_policy(self, **over):
        ident = self.identity()
        policy = {"version": 1, "kind": "trusted-vscode-terminal", "host": rl.host_identity(), "uid": os.getuid(),
                  "vscode_server_dir": str(self.tmp / "home" / ".vscode-server"),
                  "repositories": [{"root": ident["root"], "gitdir": ident["gitdir"], "remote": ident["remote"]}],
                  "accepted_risk": "I accept my own VS Code integrated terminal running in this checkout."}
        policy.update(over)
        path = rl.observer_policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(policy))
        path.chmod(0o600)
        return path

    def scan(self, *pids):
        grant = rl.observer_grant(self.identity())
        return rl.classify_scan({"status": "complete", "scanner": "x", "matches": [{"pid": p, "cwd": self.co} for p in pids]},
                                self_pid=10 ** 6, grant=grant)

    def row(self, pid):
        return self.scan(pid)["processes"][0]

    def test_verified_terminal_is_trusted_only_under_a_policy_for_this_host_and_repository(self):
        host, shell = self.launch()
        self.prompt(host, shell)
        # Default: no policy, the same genuine terminal blocks exactly as before.
        r = self.scan(shell)
        self.assertFalse(r["ok"])
        self.assertEqual(r["observer_policy"]["status"], "none")
        self.write_policy()
        r = self.scan(shell)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["processes"][0]["kind"], r["observer_policy"]["status"]), ("trusted-terminal", "granted"))
        self.assertIn("operator-accepted", r["processes"][0]["detail"])
        # Without a grant (other call sites, other checkouts) nothing changes.
        self.assertEqual(self.classify(shell)["blocking"], True)

    def test_policy_scope_and_file_refusals_keep_the_terminal_blocking(self):
        host, shell = self.launch()
        self.prompt(host, shell)
        ident = self.identity()
        here = rl.host_identity()
        other_repo = {"root": ident["root"] + "-other", "gitdir": ident["gitdir"], "remote": ident["remote"]}
        cases = {
            "other machine": {"host": {**here, "machine_id": "0" * 32}},
            "other hostname": {"host": {**here, "hostname": here["hostname"] + "-x"}},
            "other uid": {"uid": os.getuid() + 1},
            "other repository": {"repositories": [other_repo]},
            "other gitdir": {"repositories": [{**other_repo, "root": ident["root"], "gitdir": "/nope/.git"}]},
            "other remote": {"repositories": [{"root": ident["root"], "gitdir": ident["gitdir"],
                                               "remote": "https://github.com/evil/app.git"}]},
            "other server dir": {"vscode_server_dir": str(self.tmp / "elsewhere")},
            "template risk": {"accepted_risk": "<operator: state that you accept your own terminal>"},
            "unknown field": {"scanner_bypass": True},
            "wrong kind": {"kind": "all-shells"},
        }
        for name, over in cases.items():
            self.write_policy(**over)
            r = self.scan(shell)
            self.assertFalse(r["ok"], name)
            self.assertEqual(r["processes"][0]["kind"], "unknown", name)
            if name != "other server dir":
                self.assertEqual(r["observer_policy"]["status"], "refused", name)
        path = self.write_policy()
        path.chmod(0o644)
        r = self.scan(shell)
        self.assertEqual((r["ok"], r["observer_policy"]["status"]), (False, "refused"))
        self.assertIn("0600", r["observer_policy"]["detail"])
        real = self.tmp / "policy.json"
        path.chmod(0o600)
        os.replace(path, real)
        path.symlink_to(real)
        r = self.scan(shell)
        self.assertEqual((r["ok"], r["observer_policy"]["status"]), (False, "refused"))

    def test_spoofed_terminal_chains_stay_blocked_under_a_granted_policy(self):
        self.write_policy()
        # 1. The earlier fixture: a genuine pty host whose parent is not the server's server-main.js.
        _, orphan_host = self.host("ptyHost")
        # 2. The full genuine chain, started by an autonomous agent (still its ancestor).
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "codex").symlink_to("/bin/sh")
        _, agent_shell = self.launch(under=[str(bindir / "codex"), "-c"])
        # 3. The genuine chain running a user copy of bash (not a root-owned image).
        fake_bash = self.tmp / "bash"
        shutil.copy2(self.bash, fake_bash)
        init = str(self.server / rl.VSCODE_BASH_INTEGRATION)
        _, copied = self.launch(FIXTURE_ARGV=json.dumps([str(fake_bash), "--init-file", init]))
        # 4. bash with the init file, started directly (no pty host at all).
        direct = subprocess.Popen([self.bash, "--init-file", init], cwd=self.co, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(direct)
        time.sleep(1.0)
        expect = {orphan_host: "pty host parent", agent_shell: "autonomous agent", copied: "bash image",
                  direct.pid: "ptyHost"}
        for pid, needle in expect.items():
            row = self.row(pid)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
            self.assertIn(needle, row["detail"])
        # 5. The genuine chain, but the pty host does not hold this shell's pty master.
        host, shell = self.launch()
        self.prompt(host, shell)
        self.assertEqual(self.row(shell)["kind"], "trusted-terminal")
        with mock.patch.object(rl, "ptmx_indexes", lambda pid: set()):
            row = self.row(shell)
        self.assertEqual(row["blocking"], True)
        self.assertIn("does not hold the master", row["detail"])

    def test_agents_git_mutations_and_scripts_started_in_the_trusted_terminal_still_block(self):
        self.write_policy()
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "claude").symlink_to(shutil.which("sleep"))
        host, shell = self.launch()
        self.prompt(host, shell)
        lines = [f"{bindir / 'claude'} 30 &\n", "GIT_EDITOR='sleep 30; :' git commit --allow-empty &\n",
                 "python3 -c 'import time; time.sleep(30)' &\n"]
        for line in lines:
            host.stdin.write(line); host.stdin.flush()
        deadline = time.time() + 15
        kids = []
        while time.time() < deadline:
            kids = [k for k in (rl.proc_children(shell) or []) if rl.proc_cmdline(k)]
            kinds = {self.row(k)["kind"] for k in kids}
            if {"agent-writer", "git-mutation"} <= kinds and len(kids) >= 3:
                break
            time.sleep(0.1)
        r = self.scan(shell, *kids)
        self.assertFalse(r["ok"])
        by_pid = {row["pid"]: row for row in r["processes"]}
        self.assertEqual(by_pid[shell]["kind"], "trusted-terminal")
        self.assertEqual({row["kind"] for row in r["blocking"]} >= {"agent-writer", "git-mutation", "unknown"}, True,
                         r["blocking"])

    def test_preflight_with_a_trusted_terminal_still_detects_drift_and_holds_the_lease(self):
        self.write_policy()
        host, shell = self.launch()
        self.prompt(host, shell)
        os.unlink(Path(self.co, ".ready"))
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        runner = scan_runner(1, f"MATCH: PID={shell} CWD={self.co}\nBLOCKED: 1 matching processes.\n")
        branch = self.identity()["branch"]
        ok = rl.preflight("t_1", self.co, self.url, branch, "main", git=rl.default_git, scan_runner=runner,
                          owner=self.owner())
        self.assertTrue(ok["ok"], ok["checks"])
        self.assertEqual(ok["scan"]["observer_policy"]["status"], "granted")
        host.stdin.write("echo drift > drift.txt\n"); host.stdin.flush()
        deadline = time.time() + 10
        while not Path(self.co, "drift.txt").exists() and time.time() < deadline:
            time.sleep(0.05)
        again = rl.preflight("t_1", self.co, self.url, branch, "main", git=rl.default_git, scan_runner=runner,
                             owner=self.owner())
        self.assertFalse(again["ok"])
        dirty = [c for c in again["checks"] if c["check"] == "dirty"][0]
        self.assertIn("drift.txt", dirty["detail"])
        other = rl.preflight("t_2", self.co, self.url, branch, "main", git=rl.default_git, scan_runner=runner,
                             owner=self.owner(task="t_2", run="9", pid=os.getppid()))
        lease = [c for c in other["checks"] if c["check"] == "lease"][0]
        self.assertFalse(lease["ok"], lease)


# ------------------------------------------------- successor of the baseline ---

class SuccessorInventoryTest(ManifestTest):
    """The pre-existing operator baseline (task, role, head, branch, status_sha256, index_sha256,
    files[{path, status, sha256, mode, type}]) is never adopted as such; a strict successor is
    derived from it read-only when everything it binds still matches, and adopted once."""

    def legacy(self):
        self.dirty_fixture()
        _, data = self.inventory()
        legacy = {k: data[k] for k in ("task", "role", "head", "branch", "status_sha256", "index_sha256")}
        legacy["files"] = [{k: row[k] for k in ("path", "status", "sha256", "mode", "type")} for row in data["files"]]
        src = self.tmp / "validate-baseline.json"
        src.write_text(json.dumps(legacy))
        src.chmod(0o600)
        return src, rl.hashlib.sha256(src.read_bytes()).hexdigest()

    def succ(self, src, digest, out=None, task="t_1"):
        out = out or self.tmp / f"successor-{time.time_ns()}.json"
        return rl.successor_inventory(task, self.identity(), rl.default_git, str(src), digest, str(out)), out

    def test_successor_binds_strict_fields_and_is_adopted_once(self):
        src, digest = self.legacy()
        before = src.read_bytes()
        self.refuse(src, "strict bound format")  # the original itself is never adopted
        res, out = self.succ(src, digest)
        self.assertEqual(res["files"], 4)
        self.assertEqual(src.read_bytes(), before, "the original is never modified")
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        data = json.loads(out.read_text())
        ident = self.identity()
        self.assertEqual((data["root"], data["gitdir"], data["remote"], data["predecessor_sha256"]),
                         (ident["root"], ident["gitdir"], ident["remote"], digest))
        self.assertTrue(all(set(row) == set(rl.ROW_REQUIRED) for row in data["files"]))
        adopted = self.adopt(out)
        self.assertEqual(sorted(adopted["entries"]), ["a.txt", "link.go", "new.go", "tool.sh"])
        self.assertEqual(adopted["provenance"]["predecessor_sha256"], digest)
        self.assertTrue(rl.verify_dirty("t_1", self.identity(), rl.default_git)["ok"])
        # One-time: neither the same successor nor a freshly derived one is adopted again.
        manifest = rl.read_manifest("t_1")
        with self.assertRaises(rl.LeaseError) as ctx:
            self.adopt(out)
        self.assertIn("one-time", str(ctx.exception))
        _, again = self.succ(src, digest)
        with self.assertRaises(rl.LeaseError):
            self.adopt(again)
        self.assertEqual(rl.read_manifest("t_1"), manifest)

    def assert_no_successor(self, src, digest, needle, out=None, task="t_1"):
        out = out or self.tmp / f"refused-{time.time_ns()}.json"
        with self.assertRaises(rl.LeaseError) as ctx:
            rl.successor_inventory(task, self.identity(), rl.default_git, str(src), digest, str(out))
        self.assertIn(needle, str(ctx.exception))
        return out

    def test_successor_refuses_unpinned_drifted_or_misplaced_inputs(self):
        src, digest = self.legacy()
        self.assertFalse(self.assert_no_successor(src, "0" * 64, "pinned").exists())
        self.assert_no_successor(src, digest, "task", task="t_2")
        self.assert_no_successor(src, digest, "outside the checkout", out=Path(self.co, "succ.json"))
        existing = self.tmp / "exists.json"; existing.write_text("{}")
        self.assert_no_successor(src, digest, "exists", out=existing)
        self.assert_no_successor(src, digest, "exists", out=src)
        # Tampered original shape (an extra field), with a matching pin.
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({**json.loads(src.read_text()), "root": self.co}))
        self.assert_no_successor(bad, rl.hashlib.sha256(bad.read_bytes()).hexdigest(), "legacy baseline shape")
        # Content drift at the same status: a.txt rewritten (status digest unchanged, content differs).
        Path(self.co, "a.txt").write_text("3\n")
        self.assert_no_successor(src, digest, "content differs: a.txt")
        Path(self.co, "a.txt").write_text("2\n")
        os.chmod(Path(self.co, "tool.sh"), 0o700)
        self.assert_no_successor(src, digest, "mode differs: tool.sh")
        os.chmod(Path(self.co, "tool.sh"), 0o755)
        Path(self.co, "extra.txt").write_text("x\n")
        self.assert_no_successor(src, digest, "git status no longer matches")
        os.unlink(Path(self.co, "extra.txt"))
        self.g("add", "new.go")
        self.assert_no_successor(src, digest, "index file")
        self.g("reset", "-q", "--", "new.go")
        self.g("commit", "--allow-empty", "-qm", "chore: move head")
        self.assert_no_successor(src, digest, "HEAD")

    def test_tampered_successor_is_refused_by_adopt(self):
        src, digest = self.legacy()
        _, out = self.succ(src, digest)
        data = json.loads(out.read_text())
        data["files"][0]["index_oid"] = "0" * 40
        data["files"][0]["index_mode"] = "100644"
        out.write_text(json.dumps(data))
        self.refuse(out, "differs")
        data = json.loads(out.read_text())
        data["predecessor_sha256"] = "symlink:" + digest
        out.write_text(json.dumps(data))
        self.refuse(out, "predecessor_sha256")


# The two classes above reuse their parents' fixtures, not their tests.
for _base, _sub in ((VSCodeServerTest, TrustedTerminalPolicyTest), (ManifestTest, SuccessorInventoryTest)):
    for _name in [n for n in vars(_base) if n.startswith("test_") and n not in vars(_sub)]:
        setattr(_sub, _name, None)


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
        shutil.copy(shutil.which("sleep"), bindir / "nvim")  # an image named nvim (a symlink to sleep is a spoof)
        foreign = self.spawn([str(bindir / "codex"), "60"], cwd=self.co)
        mine = self.spawn([str(bindir / "nvim"), "60"], cwd=self.co)
        time.sleep(0.2)
        hits = scan_runner(1, f"MATCH: PID={mine.pid} CWD={self.co}\nMATCH: PID={foreign.pid} CWD={self.co}\nBLOCKED: 2 matching processes.\n")
        # The run's anchor is an unrelated live process and self_pid is far away, so neither match
        # descends from "us": `sleep` is a plain background process, `codex` a foreign agent.
        anchor = self.spawn(["sleep", "60"])
        real = rl._root_owned_image  # the user copy stands in for a root-installed editor image
        image = mock.patch.object(rl, "_root_owned_image", lambda exe: None if exe == str(bindir / "nvim") else real(exe))
        image.start(); self.addCleanup(image.stop)
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
        self.assertIn("changed since checkpoint: a.txt (worktree_mode/worktree_perm)", next(c["detail"] for c in result["checks"] if c["check"] == "dirty"))
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
            "scan = lambda argv: subprocess.CompletedProcess(argv, 0, 'PASS: Inspected 1 process directories.\\nPASS: No repository CWD matches found.\\n', '')\n"
            f"co, url = {self.co!r}, {self.url!r}\n"
            "rl.helper_repository = lambda h: co  # /bin/true stands in for a helper bound to co\n"
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



# ----------------------------------------------- review r3: identity hardening ---

EMPTY_ARGV_WRITER = "import sys, time\nopen(sys.argv[1] if len(sys.argv) > 1 else 'spoof.txt', 'a').write('x')\ntime.sleep(60)\n"
LEADER_EXIT_WRITER = (
    "import ctypes, threading, time\n"
    "def loop():\n"
    "    while True:\n"
    "        open('thread-writer.txt', 'a').write('x'); time.sleep(0.05)\n"
    "threading.Thread(target=loop).start()\n"
    "time.sleep(0.2)\n"
    "ctypes.CDLL(None).pthread_exit(None)\n")


class ProcessIdentityHardeningTest(TempRepo):
    """Real processes: argv cannot be spoofed with empty elements, and a live process whose
    command line reads empty is unknown (it can still write), never "gone"."""

    def classify(self, pid):
        return rl.classify_process(pid, self_pid=10 ** 6, anchors=[], self_chain=set(), grant=STRICT_GRANT)

    def test_empty_argv0_cannot_spoof_a_passive_name(self):
        bindir = self.tmp / "bin"; bindir.mkdir()
        script = bindir / "nvim"                       # named like an editor, actually a writer script
        script.write_text(EMPTY_ARGV_WRITER)
        p = self.spawn(["", str(script)], executable=os.path.realpath(sys.executable), cwd=self.co)
        deadline = time.time() + 5
        while not Path(self.co, "spoof.txt").exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(Path(self.co, "spoof.txt").exists(), "the fixture is a real writer")
        self.assertEqual(rl.proc_cmdline(p.pid), ["", str(script)], "empty argv elements are kept, not dropped")
        row = self.classify(p.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        # Empty elements in the middle do not shift the program either.
        self.assertEqual(rl.classify_cmdline(["", "nvim"])[:2], ("unknown", True))
        self.assertEqual(rl.classify_cmdline([""])[:2], ("unknown", True))

    def test_live_process_with_an_empty_command_line_is_unknown_not_gone(self):
        # argv == [""]: the interpreter reads its program from stdin and stays alive.
        p = subprocess.Popen([""], executable=os.path.realpath(sys.executable), stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.co)
        self.procs.append(p)
        time.sleep(0.3)
        row = self.classify(p.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        # Leader thread exited, another thread keeps writing: cmdline reads b"" and the leader is Z.
        w = self.spawn([sys.executable, "-c", LEADER_EXIT_WRITER], cwd=self.co)
        deadline = time.time() + 5
        while (rl._proc_read(w.pid, "cmdline") or b"x") != b"" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(rl._proc_read(w.pid, "cmdline"), b"")
        size = os.path.getsize(Path(self.co, "thread-writer.txt")); time.sleep(0.3)
        self.assertGreater(os.path.getsize(Path(self.co, "thread-writer.txt")), size, "still writing")
        row = self.classify(w.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        # A genuinely exited (unreaped, single-thread zombie) process is gone; a reaped one too.
        z = subprocess.Popen(["sleep", "60"], cwd=self.co); self.procs.append(z)
        os.kill(z.pid, 9)
        deadline = time.time() + 5
        while (rl.proc_stat_fields(z.pid) or [None] * 3)[2] != "Z" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.classify(z.pid)["kind"], "gone")
        z.wait()
        self.assertEqual(self.classify(z.pid)["kind"], "gone")


class LeasePidValidationTest(TempRepo):
    def plant(self, owner):
        ident = self.identity()
        payload = rl._lease_payload(ident, owner, 1, time.time() - 100, time.time() - 100)
        rl._atomic_replace(rl.leases_dir(), rl.lease_path(ident).name, payload)
        return ident

    def test_invalid_owner_pid_or_start_is_undecidable_and_never_recovered(self):
        sleeper = self.spawn(["sleep", "60"])
        base = {"board": "default", "task": "t_old", "run_id": "1", "host": rl.socket.gethostname()}
        start = rl.proc_start_ticks(sleeper.pid)
        bad = [{"pid": 0, "pid_start": start}, {"pid": -1, "pid_start": start}, {"pid": str(sleeper.pid), "pid_start": start},
               {"pid": True, "pid_start": start}, {"pid": None, "pid_start": start}, {"pid": 1.5, "pid_start": start},
               {"pid": 2 ** 40, "pid_start": start}, {"pid_start": start},
               {"pid": sleeper.pid, "pid_start": "x"}, {"pid": sleeper.pid, "pid_start": True},
               {"pid": sleeper.pid, "pid_start": -3}, {"pid": sleeper.pid}]
        for extra in bad:
            owner = {**base, **extra}
            ident = self.plant(owner)
            self.assertIsNone(rl.owner_alive(owner), extra)
            result = rl.acquire_lease(ident, self.owner())
            self.assertEqual(result["status"], "conflict", (extra, result))
            self.assertIn("not decidable", result["reason"])
            self.assertEqual(rl.read_lease(ident)["owner"], owner, "never replaced")
        # A valid, dead owner is still recovered.
        sleeper.kill(); sleeper.wait()
        ident = self.plant({**base, "pid": sleeper.pid, "pid_start": start})
        self.assertEqual(rl.acquire_lease(ident, self.owner())["status"], "recovered")


class SnapshotBindingTest(TempRepo):
    inventory = ManifestTest.inventory
    adopt = ManifestTest.adopt
    refuse = ManifestTest.refuse

    def test_permission_bits_are_bound_after_adoption(self):
        Path(self.co, "a.txt").write_text("2\n"); os.chmod(Path(self.co, "a.txt"), 0o644)
        self.adopt(self.inventory()[0])
        self.assertTrue(rl.verify_dirty("t_1", self.identity(), rl.default_git)["ok"])
        for mode in (0o666, 0o4644, 0o600):
            os.chmod(Path(self.co, "a.txt"), mode)
            v = rl.verify_dirty("t_1", self.identity(), rl.default_git)
            self.assertEqual(v["changed"], ["a.txt"], (oct(mode), v))
        os.chmod(Path(self.co, "a.txt"), 0o644)
        self.assertTrue(rl.verify_dirty("t_1", self.identity(), rl.default_git)["ok"])

    def test_special_file_does_not_hang_the_snapshot_and_is_never_adopted(self):
        Path(self.co, "a.txt").unlink(); os.mkfifo(Path(self.co, "a.txt"))
        probe = ("import importlib.util, json, sys\n"
                 f"spec = importlib.util.spec_from_file_location('repo_lease', {str(SCRIPT)!r})\n"
                 "rl = importlib.util.module_from_spec(spec); sys.modules['repo_lease'] = rl; spec.loader.exec_module(rl)\n"
                 f"print(json.dumps(rl.snapshot_dirty(rl.default_git, rl.repo_identity({self.co!r}, rl.default_git))))\n")
        try:
            out = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("snapshot_dirty blocked opening a FIFO in the worktree")
        self.assertEqual(out.returncode, 0, out.stderr)
        entry = json.loads(out.stdout)["a.txt"]
        self.assertEqual(entry["worktree_type"], "fifo")
        self.assertNotRegex(entry["worktree_sha256"] or "", r"^[0-9a-f]{64}$")
        # Hand-built inventory (the helper's read_bytes() would itself block on the FIFO).
        rows = {"path": "a.txt", "status": " M", "type": "file", "mode": 0o644,
                "sha256": rl.hashlib.sha256(b"").hexdigest(), "index_oid": entry["index_oid"],
                "index_mode": entry["index_mode"], "origin": None}
        ident = self.identity()
        status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=self.co,
                                capture_output=True, check=True).stdout
        doc = {"task": "t_1", "root": ident["root"], "gitdir": ident["gitdir"], "remote": ident["remote"],
               "branch": ident["branch"], "head": ident["head"], "status_sha256": rl.hashlib.sha256(status).hexdigest(),
               "files": [rows]}
        src = self.tmp / "fifo-inventory.json"; src.write_text(json.dumps(doc))
        self.refuse(src, "a.txt")

    def test_unmerged_index_binds_every_stage(self):
        self.g("switch", "-c", "side")
        Path(self.co, "a.txt").write_text("side\n"); self.g("commit", "-qam", "side")
        self.g("switch", "feat/x")
        Path(self.co, "a.txt").write_text("ours\n"); self.g("commit", "-qam", "ours")
        subprocess.run(["git", "merge", "side"], cwd=self.co, capture_output=True)
        ident = self.identity()
        snap = rl.snapshot_dirty(rl.default_git, ident)
        self.assertEqual(snap["a.txt"]["status"], "UU")
        rl.write_manifest("t_1", ident, snap, "test")
        self.assertTrue(rl.verify_dirty("t_1", ident, rl.default_git)["ok"])
        other = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.co, input="FOREIGN\n",
                               capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "update-index", "--index-info"], cwd=self.co, check=True,
                       input=f"100644 {other} 3\ta.txt\n", text=True)
        v = rl.verify_dirty("t_1", self.identity(), rl.default_git)
        self.assertEqual(v["changed"], ["a.txt"], v)

    def test_inventory_index_object_ids_are_validated_before_git_sees_them(self):
        Path(self.co, "a.txt").write_text("2\n")
        calls = []

        def git(argv, cwd):
            calls.append(list(argv))
            return rl.default_git(argv, cwd)

        for field, value in (("index_oid", "HEAD:a.txt"), ("index_oid", "--batch-all-objects"),
                             ("index_oid", "ABCDEF" * 7), ("index_mode", "garbage"), ("index_mode", 100644),
                             ("status", 7), ("origin", 3)):
            src, data = self.inventory()
            data["files"][0][field] = value
            data["files"][0]["index_sha256"] = rl.hashlib.sha256(b"1\n").hexdigest()
            src.write_text(json.dumps(data))
            calls.clear()
            self.refuse(src, "malformed", git=git)
            self.assertFalse([c for c in calls if str(value) in c], (field, value, calls))
        # A well-formed but different object id is a mismatch, and cat-file only sees the index's own id.
        src, data = self.inventory()
        good = data["files"][0]["index_oid"]
        data["files"][0]["index_oid"] = "0" * len(good)
        data["files"][0]["index_sha256"] = rl.hashlib.sha256(b"1\n").hexdigest()
        src.write_text(json.dumps(data)); calls.clear()
        self.refuse(src, "index differs: a.txt", git=git)
        self.assertFalse([c for c in calls if "0" * len(good) in c], calls)
        src, data = self.inventory()
        data["files"][0]["index_sha256"] = rl.hashlib.sha256(b"1\n").hexdigest()
        src.write_text(json.dumps(data))
        self.assertEqual(sorted(self.adopt(src)["entries"]), ["a.txt"])



# ------------------------------------------------- exact scanner protocol ---

PASS_LINES = "PASS: Inspected 412 process directories.\nPASS: No repository CWD matches found.\n"


class ScannerProtocolTest(TempRepo):
    """The registered helper's protocol, exactly: exit 0 = the two PASS markers and nothing else;
    exit 1 = one or more MATCH lines and a BLOCKED count equal to them; anything else (sudo
    errors, stderr, missing or malformed markers, other exit codes) is incomplete."""

    def status(self, code, out, err=""):
        return rl.parse_scan_output(code, out, err)["status"]

    def test_exit_zero_is_complete_only_with_both_pass_markers_and_no_match(self):
        self.assertEqual(self.status(0, PASS_LINES), "complete")
        for out in ("", "PASS: Inspected 1 process directories.\n", "PASS: No repository CWD matches found.\n",
                    PASS_LINES + "MATCH: PID=4 CWD=/x\n", "MATCH: PID=4 CWD=/x\n" + PASS_LINES,
                    PASS_LINES + "note: something else\n", "PASS: Inspected 0 process directories.\n"
                    "PASS: No repository CWD matches found.\n", PASS_LINES + PASS_LINES,
                    "PASS: Inspected many process directories.\nPASS: No repository CWD matches found.\n"):
            self.assertEqual(self.status(0, out), "incomplete", out)
        self.assertEqual(self.status(0, PASS_LINES, "sudo: unable to resolve host x\n"), "incomplete")

    def test_exit_one_needs_valid_matches_and_the_equal_blocked_count(self):
        hit = rl.parse_scan_output(1, "MATCH: PID=4321 CWD=/r/apps\nMATCH: PID=9 CWD=/r\nBLOCKED: 2 matching processes.\n")
        self.assertEqual((hit["status"], [m["pid"] for m in hit["matches"]]), ("complete", [4321, 9]))
        for out in ("", "MATCH: PID=4321 CWD=/r\n", "BLOCKED: 1 matching processes.\n",
                    "BLOCKED: 0 matching processes.\n", "MATCH: PID=4321 CWD=/r\nBLOCKED: 2 matching processes.\n",
                    "MATCH: PID=x CWD=/r\nBLOCKED: 1 matching processes.\n",
                    "MATCH: PID=4 CWD=/r\nMATCH: garbage\nBLOCKED: 1 matching processes.\n",
                    "MATCH: PID=4 CWD=relative\nBLOCKED: 1 matching processes.\n",
                    "MATCH: PID=4 CWD=/r\nBLOCKED: 1 matching processes.\nPASS: No repository CWD matches found.\n"):
            self.assertEqual(self.status(1, out), "incomplete", out)
        self.assertEqual(self.status(1, "MATCH: PID=4 CWD=/r\nBLOCKED: 1 matching processes.\n", "warning\n"),
                         "incomplete")
        for code in (2, 3, 126, 127, -9):
            self.assertEqual(self.status(code, PASS_LINES), "incomplete", code)

    def test_sudo_failure_is_incomplete_not_a_clean_scan(self):
        """Reproduction: `sudo -n` without a matching rule exits 1 with nothing on stdout; that
        used to read as a complete scan with zero matches, i.e. no writers."""
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        fake = self.tmp / "sudo-fails"
        fake.write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n"); fake.chmod(0o755)
        real = lambda argv: subprocess.run([str(fake)], capture_output=True, text=True)  # noqa: E731
        scan = rl.run_scan(self.co, real)
        self.assertEqual((scan["status"], scan["returncode"]), ("incomplete", 1))
        self.assertIn("password is required", scan["tail"])
        self.assertFalse(rl.classify_scan(scan)["ok"])
        quiet = lambda argv: subprocess.run(["/bin/sh", "-c", "exit 1"], capture_output=True, text=True)  # noqa: E731
        self.assertEqual(rl.run_scan(self.co, quiet)["status"], "incomplete")
        result = rl.preflight("t_1", self.co, self.url, "feat/x", git=rl.default_git, scan_runner=real,
                              owner=self.owner())
        self.assertEqual((result["ok"], result["exit"]), (False, 3), "unknown visibility, fail closed")
        self.assertIn("scan incomplete", next(c["detail"] for c in result["checks"] if c["check"] == "writers"))

    def test_matches_outside_the_registered_checkout_are_incomplete(self):
        rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        other = scan_runner(1, "MATCH: PID=4 CWD=/opt/repos/other-project\nBLOCKED: 1 matching processes.\n")
        self.assertEqual(rl.run_scan(self.co, other)["status"], "incomplete")
        prefix = scan_runner(1, f"MATCH: PID=4 CWD={self.co}-evil\nBLOCKED: 1 matching processes.\n")
        self.assertEqual(rl.run_scan(self.co, prefix)["status"], "incomplete")
        inside = scan_runner(1, f"MATCH: PID=4 CWD={self.co}/apps\nBLOCKED: 1 matching processes.\n")
        self.assertEqual(rl.run_scan(self.co, inside)["status"], "complete")

    def test_registration_is_revalidated_at_execution(self):
        """A registry edited after registration (user-owned helper, extra arguments, no `-n`) is
        never executed: the scan is incomplete and the runner is not called."""
        mine = self.tmp / "scan.sh"
        mine.write_text("#!/bin/sh\nprintf 'PASS: Inspected 1 process directories.\\n"
                        "PASS: No repository CWD matches found.\\n'\n"); mine.chmod(0o755)
        for argv in (["sudo", "-n", str(mine)], [str(mine)], ["sudo", "-n", "/bin/true", "--all"],
                     ["sudo", "/bin/true"], ["/bin/../bin/true"]):
            runner = scan_runner(0, PASS_LINES)
            scan = rl.run_scan(self.co, runner, registry={self.co: argv})
            self.assertEqual((scan["status"], runner.calls), ("incomplete", []), argv)
        ok = scan_runner(0, PASS_LINES)
        self.assertEqual(rl.run_scan(self.co, ok, registry={self.co: ["sudo", "-n", "/bin/true"]})["status"], "complete")

    def test_default_runner_uses_the_system_sudo_not_the_callers_path(self):
        bindir = self.tmp / "bin"; bindir.mkdir()
        fake = bindir / "sudo"
        fake.write_text("#!/bin/sh\nprintf 'PASS: Inspected 1 process directories.\\n"
                        "PASS: No repository CWD matches found.\\n'\n"); fake.chmod(0o755)
        seen = {}

        def capture(argv, **kw):
            seen.update(argv=list(argv), path=kw["env"].get("PATH"))
            return done(1, "", "sudo: a password is required\n")
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ.get('PATH', '')}"}), \
                mock.patch.object(rl.subprocess, "run", side_effect=capture):
            self.assertEqual(shutil.which("sudo"), str(fake), "the fixture really shadows sudo")
            rl.default_scan_runner(["sudo", "-n", "/bin/true"])
        self.assertTrue(seen["argv"][0].startswith("/") and seen["argv"][0] != str(fake), seen)
        self.assertEqual(os.stat(seen["argv"][0]).st_uid, 0)
        self.assertEqual(seen["argv"][1:], ["-n", "/bin/true"])
        self.assertNotIn(str(bindir), seen["path"])


# ------------------------------------------ passive executable identity ---

SPOOF_WRITER = "import time\nwhile True:\n    open('spoof.txt', 'a').write('x'); time.sleep(0.05)\n"


class PassiveExecutableIdentityTest(TempRepo):
    """A passive name in argv is a claim; /proc/<pid>/exe must back it."""

    def classify(self, pid):
        return rl.classify_process(pid, self_pid=10 ** 6, anchors=[], self_chain=set(), grant=STRICT_GRANT)

    def wait_writing(self, name="spoof.txt"):
        deadline = time.time() + 5
        while not Path(self.co, name).exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(Path(self.co, name).exists(), "the fixture is a real writer")

    def test_argv0_renamed_writer_is_not_an_editor(self):
        """`exec -a nvim python3 writer.py`: argv says nvim, the image is python, it writes."""
        script = self.tmp / "writer.py"; script.write_text(SPOOF_WRITER)
        for name in ("nvim", "vim", "/usr/bin/nano"):
            Path(self.co, "spoof.txt").unlink(missing_ok=True)
            p = self.spawn([name, str(script)], executable=os.path.realpath(sys.executable), cwd=self.co)
            self.wait_writing()
            self.assertEqual(rl.proc_cmdline(p.pid)[0], name)
            self.assertEqual(rl.classify_cmdline(rl.proc_cmdline(p.pid))[1], False, "argv alone looks passive")
            row = self.classify(p.pid)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), (name, row))
            p.kill(); p.wait()

    def test_argv_git_readonly_spoof_is_not_git(self):
        Path(self.co, "rev-parse").write_text(SPOOF_WRITER)
        p = self.spawn(["git", "rev-parse"], executable=os.path.realpath(sys.executable), cwd=self.co)
        self.wait_writing()
        self.assertEqual(rl.classify_cmdline(["git", "rev-parse"])[0], "git-readonly")
        row = self.classify(p.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)

    def test_symlink_named_like_a_tool_runs_another_image(self):
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "nvim").symlink_to(shutil.which("sleep"))
        (bindir / "vite").symlink_to(shutil.which("sleep"))
        for name in ("nvim", "vite"):
            p = self.spawn([str(bindir / name), "60"], cwd=self.co); time.sleep(0.2)
            row = self.classify(p.pid)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), (name, row))

    def test_interpreter_hosted_tool_must_run_on_that_interpreter(self):
        """`node .../vite.js` is accepted by script name only when the image really is node."""
        mods = Path(self.co, "node_modules", "vite", "bin"); mods.mkdir(parents=True)
        Path(mods, "vite.js").write_text(SPOOF_WRITER)
        p = self.spawn(["node", str(mods / "vite.js")], executable=os.path.realpath(sys.executable), cwd=self.co)
        self.wait_writing()
        self.assertEqual(rl.classify_cmdline(["node", str(mods / "vite.js")])[0], "unknown", "a script is never a known tool")
        row = self.classify(p.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)

    def test_genuine_images_stay_passive(self):
        bindir = self.tmp / "bin"; bindir.mkdir()
        shutil.copy(shutil.which("sleep"), bindir / "nvim")   # an image that is itself named nvim
        rows = {}
        nv = self.spawn([str(bindir / "nvim"), "60"], cwd=self.co)
        git = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=self.co)
        self.procs.append(git)
        procs = {"nvim": nv, "git": git}
        vi = shutil.which("vi")
        if vi and Path(os.path.realpath(vi)).name.startswith("vim"):   # Debian: vi -> vim.tiny/vim.basic
            procs["vi"] = subprocess.Popen(["vi", "-n", "-R"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, cwd=self.co)
            self.procs.append(procs["vi"])
        time.sleep(0.5)
        for name, p in procs.items():
            rows[name] = self.classify(p.pid)
        # A user copy named nvim is a name chosen by the user, not root: unknown (copied-interpreter spoof).
        self.assertEqual((rows["nvim"]["kind"], rows["nvim"]["blocking"]), ("unknown", True), rows["nvim"])
        self.assertIn("root-owned", rows["nvim"]["detail"])
        self.assertEqual((rows["git"]["kind"], rows["git"]["blocking"]), ("git-readonly", False), rows["git"])
        if "vi" in rows:
            self.assertEqual((rows["vi"]["kind"], rows["vi"]["blocking"]), ("editor", False), rows["vi"])


class PassiveWriteFlagTest(unittest.TestCase):
    """Passive names with arguments that make them writers (or run arbitrary commands) block."""

    def test_write_capable_invocations_block(self):
        cases = [
            ("git", "diff", "--output=/r/a.patch"), ("git", "log", "--output", "f"), ("git", "show", "--output=x", "HEAD"),
            ("git", "-c", "core.fsmonitor=./hook", "status"), ("git", "-c", "core.pager=sh -c 'touch x'", "log"),
            ("git", "-c", "alias.st=!touch x", "status"), ("git", "--config-env=core.hooksPath=H", "status"),
            ("git", "--exec-path=/tmp/x", "status"),
            ("git", "symbolic-ref", "HEAD", "refs/heads/x"), ("git", "symbolic-ref", "-d", "HEAD"),
            ("git", "symbolic-ref", "--delete", "refs/x"), ("git", "symbolic-ref", "-m", "why", "HEAD", "refs/heads/y"),
            ("git", "reflog", "expire", "--all"), ("git", "reflog", "delete", "HEAD@{1}"), ("git", "reflog", "drop", "x"),
            ("git", "-C", "/r", "reflog", "expire"), ("git", "fsck", "--lost-found"),
            ("git", "grep", "-Ocat", "x"), ("git", "grep", "--open-files-in-pager=vi", "x"), ("git", "grep", "-O", "x"),
            ("ruff", "check", "--fix", "."), ("ruff", "--fix"), ("ruff", "format", "."), ("ruff", "check", "."),
            ("ruff",), ("eslint_d", "--fix", "a.js"),
            ("emacs", "--batch", "-l", "x.el"), ("emacs", "-batch"), ("emacs", "--script", "x.el"),
            ("emacs", "--eval=(write-region 1 2 \"x\")"), ("emacs", "-l", "x.el"), ("emacs", "-f", "save-buffer"),
            ("emacsclient", "-e", "(save-buffer)"), ("emacsclient", "--eval", "(x)"),
            ("nvim", "--headless"), ("nvim", "--headless", "-c", "w"), ("nvim", "-es"), ("vim", "-Es"),
            ("vim", "-c", "wq", "x"), ("vim", "+wq", "x"), ("vim", "--cmd", "w"), ("vim", "-S", "s.vim"),
            ("vim", "-s", "keys"), ("vim", "-w", "keys"), ("nvim", "-l", "x.lua"), ("vim", "-e"),
            ("vim", "--remote-send", ":w<CR>"), ("vim", "-u", "rc.vim"), ("vi", "-Rc", "w"), ("view", "-es"),
            ("kak", "-f", "ggd", "x"), ("kak", "-e", "write"),
        ]
        for cmd in cases:
            kind, blocking, detail = rl.classify_cmdline(list(cmd))
            self.assertTrue(blocking, (cmd, kind, detail))
            self.assertNotIn(kind, rl.NONBLOCKING_KINDS, cmd)

    def test_plain_invocations_stay_passive(self):
        cases = [
            ("git", "symbolic-ref", "HEAD"), ("git", "symbolic-ref", "--short", "-q", "HEAD"),
            ("git", "-C", "/r", "rev-parse", "HEAD"), ("emacs", "main.go"), ("emacs", "-nw", "x"),
            ("nvim", "+12", "main.go"), ("vim", "-R", "x"), ("vim", "+/foo", "x"), ("vim", "-d", "a", "b"),
            ("nvim",), ("vim", "+"), ("emacsclient", "-t", "x"), ("kak", "x"),
        ]
        for cmd in cases:
            kind, blocking, detail = rl.classify_cmdline(list(cmd))
            self.assertFalse(blocking, (cmd, kind, detail))


class OwnGitFsmonitorTest(TempRepo):
    def test_own_git_reads_never_run_a_repository_fsmonitor_hook(self):
        sentinel = self.tmp / "fsmonitor-ran"
        hook = self.tmp / "fsmonitor-hook"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n"); hook.chmod(0o755)
        self.g("config", "core.fsmonitor", str(hook))
        subprocess.run(["git", "status", "--porcelain"], cwd=self.co, capture_output=True)
        self.assertTrue(sentinel.exists(), "the fixture hook really runs under plain git")
        sentinel.unlink()
        self.assertEqual(rl.default_git(["status", "--porcelain"], self.co).returncode, 0)
        rl.snapshot_dirty(rl.default_git, self.identity())
        self.assertFalse(sentinel.exists(), "repo_lease git read ran the fsmonitor hook")


class IndependentAuditR3Test(TempRepo):
    """Audit of aeb785d: names, scripts, build tools and abbreviated git options are claims that
    cannot prove passivity; a helper's PASS is about its own root; own `git status` must not run a
    repository clean filter."""

    def classify(self, pid):
        return rl.classify_process(pid, self_pid=10 ** 6, anchors=[], self_chain=set(), grant=STRICT_GRANT)

    def wait_writing(self, name="spoof.txt"):
        deadline = time.time() + 5
        while not Path(self.co, name).exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(Path(self.co, name).exists(), "the fixture is a real writer")

    def test_copied_interpreter_named_like_a_tool_blocks(self):
        """A user copy of python named gopls / vim.basic: image name matches, it still writes."""
        bindir = self.tmp / "bin"; bindir.mkdir()
        script = self.tmp / "writer.py"; script.write_text(SPOOF_WRITER)
        for name in ("gopls", "vim.basic"):
            shutil.copy(os.path.realpath(sys.executable), bindir / name)
            Path(self.co, "spoof.txt").unlink(missing_ok=True)
            p = self.spawn([str(bindir / name), str(script)], cwd=self.co)
            self.wait_writing()
            row = self.classify(p.pid)
            self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), (name, row))
            p.kill(); p.wait()

    def test_interpreter_scripts_named_like_tools_block(self):
        for name in ("vite", "gopls", "nvim", "webpack"):
            for interp, ext in (("python3", ".py"), ("node", ".js"), ("/usr/bin/python3.12", ".py")):
                cmd = [interp, f"/x/node_modules/{name}/bin/{name}{ext}"]
                self.assertEqual(rl.classify_cmdline(cmd)[:2], ("unknown", True), cmd)
        script = Path(self.co, "gopls.py"); script.write_text(SPOOF_WRITER)
        p = self.spawn([os.path.realpath(sys.executable), str(script)], cwd=self.co)
        self.wait_writing()
        self.assertEqual(self.classify(p.pid)["blocking"], True)

    def test_language_servers_by_name_alone_block(self):
        for cmd in (("gopls", "serve"), ("/usr/bin/clangd",), ("rust-analyzer",), ("ruff", "server"),
                    ("node", "/x/node_modules/.bin/typescript-language-server", "--stdio"), ("eslint_d", "start")):
            self.assertEqual(rl.classify_cmdline(list(cmd))[:2], ("unknown", True), cmd)

    def test_build_tools_and_dev_servers_block(self):
        for cmd in (("tsc",), ("tsc", "-w"), ("rollup", "-c"), ("webpack",), ("esbuild", "--outfile=out.js", "a.ts"),
                    ("vite", "build"), ("vite",), ("nodemon", "--exec", "touch x"), ("next", "dev"), ("air",)):
            kind, blocking, _ = rl.classify_cmdline(list(cmd))
            self.assertEqual((kind, blocking), ("unknown", True), cmd)
        self.assertNotIn("dev-server", rl.NONBLOCKING_KINDS)

    def test_git_reads_only_in_explicit_safe_forms(self):
        blocking = [
            ("git", "grep", "--open-fil=vi", "x"), ("git", "symbolic-ref", "--del", "HEAD"), ("git", "fsck", "--lost"),
            ("git", "log", "--outp=f"), ("git", "status"), ("git", "status", "--porcelain"), ("git", "diff"),
            ("git", "log", "-1"), ("git", "show", "HEAD"), ("git", "grep", "-n", "x"), ("git", "blame", "a"),
            ("git", "ls-files", "-m"), ("git", "describe", "--dirty"), ("git", "reflog"), ("git", "fsck"),
            ("git", "cat-file", "--textconv", "HEAD:a"), ("git", "cat-file", "--filters", "HEAD:a"),
            ("git", "cat-file", "--text", "HEAD:a"), ("git", "cat-file", "-pt", "HEAD"),
            ("git", "-c", "x=y", "rev-parse", "HEAD"), ("git", "--exec-path=/x", "rev-parse"),
            ("git", "symbolic-ref", "HEAD", "refs/heads/x"), ("git", "rev-list", "--count", "--output=x", "HEAD"),
            ("git", "ls-remote"), ("git", "for-each-ref", "--sort", "-committerdate"),
        ]
        for cmd in blocking:
            kind, b, detail = rl.classify_cmdline(list(cmd))
            self.assertTrue(b, (cmd, kind, detail))
        passive = [
            ("git", "rev-parse", "--show-toplevel"), ("/usr/bin/git", "-C", "/r", "rev-parse", "HEAD"),
            ("git", "cat-file", "--batch"), ("git", "cat-file", "-p", "HEAD:a"), ("git", "ls-tree", "-r", "HEAD"),
            ("git", "for-each-ref", "--format=%(refname)"), ("git", "rev-list", "--count", "HEAD", "--", "a"),
            ("git", "merge-base", "--is-ancestor", "a", "b"), ("git", "show-ref", "--heads"),
            ("git", "symbolic-ref", "--short", "-q", "HEAD"), ("git", "--no-pager", "rev-parse", "HEAD"),
        ]
        for cmd in passive:
            kind, b, detail = rl.classify_cmdline(list(cmd))
            self.assertEqual((kind, b), ("git-readonly", False), (cmd, detail))

    def test_git_read_on_a_terminal_can_spawn_a_configured_pager(self):
        """pager.<cmd> config applies to any builtin when stdout is a tty: such a read blocks."""
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        git = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=slave,
                               stderr=subprocess.DEVNULL, cwd=self.co)
        os.close(slave)
        self.procs.append(git)
        time.sleep(0.3)
        row = self.classify(git.pid)
        self.assertEqual((row["kind"], row["blocking"]), ("unknown", True), row)
        self.assertIn("terminal", row["detail"])

    def test_declared_repository_is_parsed_strictly(self):
        ok = 'import os\nREPOSITORY = "/opt/repos/validate"\n\ndef main():\n    return REPOSITORY\n'
        self.assertEqual(rl.declared_repository(ok), "/opt/repos/validate")
        for text in ('REPOSITORY = "/a"\nREPOSITORY = "/b"\n', 'REPOSITORY = os.environ["R"]\n',
                     'REPOSITORY = "relative"\n', 'REPOSITORY = "/a/../b"\n', 'X = "/a"\n', "not python (",
                     'def f():\n    REPOSITORY = "/a"\n', 'REPOSITORY = "/a"\ndef f():\n    global REPOSITORY\n    REPOSITORY = "/b"\n',
                     'REPOSITORY = "/a"\nREPOSITORY += "/b"\n', 'REPOSITORY: str = "/a"\nREPOSITORY = "/b"\n'):
            self.assertIsNone(rl.declared_repository(text), text)
        self.assertIsNone(rl.helper_repository("/bin/ls"), "a binary declares no repository")

    def test_live_validate_helper_declares_validate(self):
        helper = "/usr/local/libexec/hermes-validate-cwd-scan"
        if not os.path.exists(helper):
            self.skipTest("approved helper not installed on this host")
        self.assertEqual(rl.helper_repository(helper), "/opt/repos/validate")

    def test_helper_bound_to_another_root_never_passes_for_this_checkout(self):
        reg = {self.co: ["sudo", "-n", "/bin/true"]}
        runner = scan_runner(0, PASS_LINES)
        with mock.patch.object(rl, "helper_repository", lambda h: "/opt/repos/validate"):
            scan = rl.run_scan(self.co, runner, reg)
            self.assertEqual(scan["status"], "incomplete")
            self.assertIn("/opt/repos/validate", scan["tail"])
            self.assertEqual(runner.calls, [], "a helper for another root is never run")
            self.assertFalse(rl.classify_scan(scan)["ok"])
            with self.assertRaises(rl.LeaseError):
                rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        with mock.patch.object(rl, "helper_repository", lambda h: None):
            self.assertEqual(rl.run_scan(self.co, scan_runner(0, PASS_LINES), reg)["status"], "incomplete")
            with self.assertRaises(rl.LeaseError):
                rl.register_scanner(self.co, ["sudo", "-n", "/bin/true"])
        with mock.patch.object(rl, "helper_repository", lambda h: self.co):
            self.assertEqual(rl.run_scan(self.co, scan_runner(0, PASS_LINES), reg)["status"], "complete")

    def test_own_git_status_never_runs_a_repository_clean_filter(self):
        sentinel = self.tmp / "filter-ran"
        self.g("config", "filter.x.clean", f"sh -c 'touch {sentinel}; cat'")
        Path(self.co, ".gitattributes").write_text("* filter=x\n")
        Path(self.co, "a.txt").write_text("2\n")
        subprocess.run(["git", "status", "--porcelain"], cwd=self.co, capture_output=True)
        self.assertTrue(sentinel.exists(), "the fixture filter really runs under plain git")
        sentinel.unlink()
        ObserverGrantScopeTest.policy(self)  # suppression is the granted regime only (PR28 scope)
        r = rl.default_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.co)
        self.assertFalse(sentinel.exists(), "repo_lease git status ran a clean filter")
        self.assertNotEqual(r.returncode, 0, "a filter a tracked path uses is unsupported, not disabled")
        self.assertIn("unsupported", r.stderr)
        with self.assertRaises(rl.LeaseError):
            rl.snapshot_dirty(rl.default_git, self.identity())
        self.assertFalse(sentinel.exists())
        # A driver name a `-c` override cannot express fails the read instead of running it.
        self.g("config", "filter.we=ird.process", f"sh -c 'touch {sentinel}'")
        r = rl.default_git(["status", "--porcelain"], self.co)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(sentinel.exists())

    def test_own_git_status_fails_closed_on_a_required_filter(self):
        self.g("config", "filter.x.clean", "cat")
        self.g("config", "filter.x.required", "true")
        Path(self.co, ".gitattributes").write_text("* filter=x\n")
        Path(self.co, "a.txt").write_text("3\n")
        self.assertEqual(rl.default_git(["status", "--porcelain"], self.co).returncode, 0,
                         "no grant: git applies the filter as before PR28")
        ObserverGrantScopeTest.policy(self)
        r = rl.default_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.co)
        self.assertNotEqual(r.returncode, 0, "a required filter cannot be neutered: the read must fail")


class ObserverGrantScopeTest(TempRepo):
    """PR28 stricter classification and own-git filter suppression apply only under a granted
    observer policy for this exact host, uid and repository; without one (absent, malformed,
    foreign host/uid/repo) process classification and git status are the pre-PR28 (d468701) ones."""

    def policy(self, **over):
        ident = self.identity()
        data = {"version": 1, "kind": "trusted-vscode-terminal", "host": rl.host_identity(), "uid": os.getuid(),
                "vscode_server_dir": str(self.tmp / "home" / ".vscode-server"),
                "repositories": [{"root": ident["root"], "gitdir": ident["gitdir"], "remote": ident["remote"]}],
                "accepted_risk": "I accept my own VS Code integrated terminal running in this checkout."}
        data.update(over)
        path = rl.observer_policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        path.chmod(0o600)
        return path

    def fake(self, *argv):
        """A live process whose /proc cmdline is exactly *argv* (argv[0] via exec -a)."""
        d = self.tmp / f"bin-{len(self.procs)}"
        d.mkdir()
        script = d / (argv[1] if len(argv) > 1 else "x")
        script.write_text("while :; do sleep 1; done\n")
        args = list(argv[1:]) or ["x"]
        p = self.spawn(["/bin/bash", "-c", 'exec -a "$0" /bin/sh "$@"', argv[0], *args], cwd=str(d))
        deadline = time.time() + 5
        while time.time() < deadline and (rl.proc_cmdline(p.pid) or [""])[0] != argv[0]:
            time.sleep(0.02)
        return p.pid

    def scan(self, *pids):
        return rl.classify_scan({"status": "complete", "scanner": "x",
                                 "matches": [{"pid": p, "cwd": self.co} for p in pids]},
                                self_pid=10 ** 6, grant=rl.observer_grant(self.identity()))

    def regimes(self):
        """(name, setup) for every non-granted regime."""
        return [("absent", lambda: None),
                ("malformed", lambda: self.policy(version=99)),
                ("not-json", lambda: rl.observer_policy_path().parent.mkdir(parents=True, exist_ok=True)
                 or (rl.observer_policy_path().write_text("{"), rl.observer_policy_path().chmod(0o600))),
                ("foreign-host", lambda: self.policy(host={"machine_id": "0" * 32, "hostname": "elsewhere"})),
                ("foreign-uid", lambda: self.policy(uid=os.getuid() + 1)),
                ("foreign-repo", lambda: self.policy(repositories=[{"root": "/nonexistent/other", "gitdir":
                                                                    "/nonexistent/other/.git", "remote": "file:///x"}]))]

    def reset_policy(self):
        path = rl.observer_policy_path()
        if path.exists():
            path.unlink()

    def test_without_a_grant_reads_and_watchers_classify_as_before_pr28(self):
        readers = [("git", "status"), ("git", "diff"), ("git", "log"), ("git", "ls-files"),
                   ("vite",), ("nodemon",), ("gopls",)]
        pids = {argv: self.fake(*argv) for argv in readers}
        for name, setup in self.regimes():
            self.reset_policy()
            setup()
            scan = self.scan(*pids.values())
            self.assertNotEqual(scan["observer_policy"]["status"], "granted", name)
            for argv, row in zip(pids, scan["processes"]):
                self.assertFalse(row["blocking"], (name, argv, row))
            self.assertTrue(scan["ok"], name)

    def test_agents_and_git_mutations_block_in_both_regimes(self):
        pids = [self.fake("git", "commit"), self.fake("git", "rebase"), self.fake("claude"), self.fake("hermes")]
        for name, setup in [*self.regimes(), ("granted", self.policy)]:
            self.reset_policy()
            setup()
            scan = self.scan(*pids)
            self.assertEqual([r["blocking"] for r in scan["processes"]], [True] * 4, (name, scan["processes"]))
            self.assertFalse(scan["ok"], name)

    def test_granted_policy_keeps_the_strict_classification(self):
        self.policy()
        pids = [self.fake("git", "status"), self.fake("vite")]
        scan = self.scan(*pids)
        self.assertEqual(scan["observer_policy"]["status"], "granted")
        self.assertTrue(all(r["blocking"] for r in scan["processes"]), scan["processes"])

    def test_failed_or_unknown_scan_blocks_in_both_regimes(self):
        for name, setup in [("absent", lambda: None), ("granted", self.policy)]:
            self.reset_policy()
            setup()
            grant = rl.observer_grant(self.identity())
            for scan in ({"status": "incomplete", "scanner": "sudo", "tail": "sudo: a password is required", "matches": []},
                         {"status": "unknown", "scanner": "x", "matches": []}, {"matches": []}):
                self.assertFalse(rl.classify_scan(scan, self_pid=10 ** 6, grant=grant)["ok"], (name, scan))

    def _normalizing_filter(self):
        """A clean filter that normalizes X to x: a worktree "X" equal to HEAD after normalization,
        stat-dirty (touched) so git must hash it through the filter."""
        self.g("config", "filter.crlf.clean", "tr X x")
        Path(self.co, "a.txt").write_text("x\n")
        self.g("add", "a.txt")
        Path(self.co, ".gitattributes").write_text("*.txt filter=crlf\n")
        self.g("add", ".gitattributes")
        self.g("commit", "-m", "chore: attrs")
        Path(self.co, "a.txt").write_text("X\n")
        os.utime(Path(self.co, "a.txt"), (time.time() + 5, time.time() + 5))

    def test_without_a_grant_own_git_status_keeps_legitimate_filter_normalization(self):
        self._normalizing_filter()
        plain = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                               cwd=self.co, capture_output=True, text=True)
        self.assertEqual(plain.stdout, "", "fixture: plain git sees the normalized file as clean")
        r = rl.default_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.co)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual(rl.dirty_paths(rl.default_git, self.co), [])
        r = wf.default_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.co)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_with_a_grant_an_applied_filter_is_explicitly_unsupported_not_dirty(self):
        self._normalizing_filter()
        self.policy()
        r = rl.default_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.co)
        self.assertNotEqual(r.returncode, 0, "a filter in scope must not be silently disabled")
        self.assertNotIn("a.txt", r.stdout or "")
        self.assertIn("unsupported", r.stderr)
        with self.assertRaises(rl.LeaseError):
            rl.dirty_paths(rl.default_git, self.co)
        # A configured driver no tracked path uses does not block the granted read.
        Path(self.co, ".gitattributes").write_text("*.nomatch filter=crlf\n")
        self.g("add", ".gitattributes")
        self.g("commit", "-m", "chore: attrs off")
        Path(self.co, "a.txt").write_text("x\n")
        r = rl.default_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.co)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
