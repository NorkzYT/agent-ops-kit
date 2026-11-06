"""Tests for hermes/libexec/hermes-devtest-runner (trusted Go/PostgreSQL dev test runner).

Hermetic: a synthetic Go module, a synthetic credential file holding a canary password, a
throwaway Kanban board (sqlite) and a real repo_lease.py lease in a temp state dir. The
sandbox is the real one (Landlock + seccomp on this kernel); the database port is a local
TCP listener of this test, never the operator's database, and no live credential is read.

Run: python3 -m unittest discover -s scripts/tests -p 'test_devtest_runner.py'
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "hermes" / "libexec" / "hermes-devtest-runner"
LEASE = REPO / "hermes" / "skills" / "kanban-workflow" / "scripts" / "repo_lease.py"
EXAMPLE = REPO / "hermes" / "devtest" / "profiles.example.json"

sys.dont_write_bytecode = True


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


dt = _load("devtest_runner", RUNNER)
rl = _load("repo_lease_for_devtest", LEASE)

GO = "/usr/local/go/bin/go"
CANARY = "CANARY-pw-7f3a9c51-not-a-real-secret"
OTHER_CANARY = "OTHER-pw-2b8d0e44-not-a-real-secret"
TASK = "t_0a1b2c3d"
RUN_ID = 4242

GO_TEST_SRC = r'''
package probe

import (
	"fmt"
	"net"
	"os"
	"strings"
	"syscall"
	"testing"
)

const outsideSecret = %(outside)s
const parentPid = %(ppid)d
const dbPort = %(dbport)d
const deniedPort = %(deniedport)d
const unixSock = %(unixsock)s
const moduleDir = %(moduledir)s
const ephemeralPort = %(ephemeralport)d
const otherCanary = %(othercanary)s

func TestPasses(t *testing.T) {}

func TestSkipped(t *testing.T) { t.Skip("needs the synthetic fixture to be absent") }

func TestCleanEnvironment(t *testing.T) {
	if v := os.Getenv("HERMES_DEVTEST_PARENT_CANARY"); v != "" {
		t.Fatalf("parent environment leaked: %%q", v)
	}
	for _, k := range []string{"PGHOST", "PGPASSWORD", "PGSERVICEFILE", "AWS_SECRET_ACCESS_KEY", "HERMES_HOME"} {
		if os.Getenv(k) != "" {
			t.Fatalf("%%s inherited", k)
		}
	}
	if os.Getenv("PGPASSFILE") == "" || !strings.HasPrefix(os.Getenv("MATCHING_TEST_DATABASE_URL"), "postgresql://postgres@127.0.0.1:") {
		t.Fatalf("approved database variables missing")
	}
	if strings.Contains(os.Getenv("MATCHING_TEST_DATABASE_URL"), ":"+%(canary)s) {
		t.Fatalf("password in URL")
	}
}

func TestSandbox(t *testing.T) {
	if _, err := os.ReadFile(outsideSecret); err == nil {
		t.Fatalf("read a host secret outside the sandbox")
	}
	if _, err := os.ReadFile(fmt.Sprintf("/proc/%%d/environ", parentPid)); err == nil {
		t.Fatalf("read the parent's environment")
	}
	if _, err := os.ReadFile(moduleDir + "/.env"); err == nil {
		t.Fatalf("read the module .env")
	}
	if _, err := os.ReadFile(moduleDir + "/.env.example"); err != nil {
		t.Fatalf(".env.example should stay readable: %%v", err)
	}
	if err := os.WriteFile(moduleDir+"/planted.go", []byte("package probe"), 0o644); err == nil {
		t.Fatalf("wrote into the checkout")
	}
	if c, err := net.Dial("unix", unixSock); err == nil {
		c.Close()
		t.Fatalf("connected to a UNIX socket")
	}
	if c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%%d", deniedPort)); err == nil {
		c.Close()
		t.Fatalf("connected to an unapproved TCP port")
	}
	c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%%d", dbPort))
	if err != nil {
		t.Fatalf("database port must be reachable: %%v", err)
	}
	c.Close()
	if err := syscall.Kill(parentPid, 0); err == nil {
		t.Fatalf("signalled a process outside the sandbox")
	}
	data, err := os.ReadFile(os.Getenv("PGPASSFILE"))
	if err != nil {
		t.Fatalf("credential file must be readable by the tests: %%v", err)
	}
	if strings.Contains(string(data), otherCanary) {
		t.Fatalf("credential file hands the tests an entry of another database")
	}
	if _, err := os.ReadFile(moduleDir + "/env-alias.txt"); err == nil {
		t.Fatalf("read a hardlink alias of the module .env")
	}
	if c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%%d", ephemeralPort)); err == nil {
		c.Close()
		t.Fatalf("connected to a loopback ephemeral port that is not the database")
	}
}

func TestLeakName(t *testing.T) {
	data, _ := os.ReadFile(os.Getenv("PGPASSFILE"))
	t.Run(strings.TrimSpace(string(data)), func(t *testing.T) { t.Fatalf("named after the credential") })
}

func TestLeakFails(t *testing.T) {
	data, _ := os.ReadFile(os.Getenv("PGPASSFILE"))
	fmt.Println("dump:", string(data))
	t.Fatalf("leaking %%s and postgres://postgres:%%s@127.0.0.1/x", string(data), strings.TrimSpace(string(data)))
}
'''


def _free_port(low: int, high: int) -> int:
    for port in range(low, high):
        with contextlib.closing(socket.socket()) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


@unittest.skipUnless(os.path.exists(GO), "Go toolchain not installed")
class DevtestRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="devtest-"))
        cls.home_cache = Path(tempfile.mkdtemp(prefix="devtest-trust-", dir=os.path.expanduser("~")))
        os.chmod(cls.home_cache, 0o755)
        cls.etc = cls.home_cache / "etc"
        cls.etc.mkdir(mode=0o755)
        cls.checkout = cls.tmp / "checkout"
        cls.module = cls.checkout / "svc"
        cls.module.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(cls.checkout)], check=True)
        cls.secret_dir = cls.tmp / "secret"
        cls.secret_dir.mkdir(mode=0o700)
        cls.pgpass = cls.secret_dir / "pgpass"
        cls.outside = cls.tmp / "host-secret.txt"
        cls.outside.write_text("host secret canary\n")
        cls.unix_sock_path = str(cls.tmp / "agent.sock")
        cls.unix_sock = socket.socket(socket.AF_UNIX)
        cls.unix_sock.bind(cls.unix_sock_path)
        cls.unix_sock.listen(5)
        cls.db_port = _free_port(20000, 24000)
        cls.db_sock = socket.socket()
        cls.db_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.db_sock.bind(("127.0.0.1", cls.db_port))
        cls.db_sock.listen(16)
        cls.denied_port = _free_port(24000, 28000)
        cls.denied_sock = socket.socket()
        cls.denied_sock.bind(("127.0.0.1", cls.denied_port))
        cls.denied_sock.listen(16)
        cls.ephemeral_sock = socket.socket()
        cls.ephemeral_sock.bind(("127.0.0.1", 0))
        cls.ephemeral_sock.listen(16)
        cls.ephemeral_port = cls.ephemeral_sock.getsockname()[1]
        (cls.module / "go.mod").write_text("module example.com/probe\n\ngo 1.23\n")
        (cls.module / ".env").write_text("SECRET=module-dotenv-canary\n")
        (cls.module / ".env.example").write_text("SECRET=\n")
        os.link(cls.module / ".env", cls.module / "env-alias.txt")  # hardlink alias of the excluded file
        (cls.module / "probe_test.go").write_text(GO_TEST_SRC % {
            "outside": json.dumps(str(cls.outside)), "ppid": os.getpid(), "dbport": cls.db_port,
            "deniedport": cls.denied_port, "unixsock": json.dumps(cls.unix_sock_path),
            "moduledir": json.dumps(str(cls.module)), "canary": json.dumps(CANARY),
            "ephemeralport": cls.ephemeral_port, "othercanary": json.dumps(OTHER_CANARY)})
        cls.state = cls.tmp / "state"
        cls.devstate = cls.tmp / "devtest-state"
        cls.board = cls.tmp / "kanban.db"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.unix_sock, cls.db_sock, cls.denied_sock, cls.ephemeral_sock):
            s.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.home_cache, ignore_errors=True)

    def setUp(self):
        # Two entries: the registered one and another database's; only the first may reach the tests.
        self.pgpass.write_text(f"127.0.0.1:{self.db_port}:matching_test:postgres:{CANARY}\n"
                               f"127.0.0.1:5432:other_prod:postgres:{OTHER_CANARY}\n")
        os.chmod(self.pgpass, 0o600)
        self._board(status="running", worker_pid=os.getpid(), run_status="running")
        self.env = mock.patch.dict(os.environ, {"HERMES_WORKFLOW_STATE": str(self.state),
                                                "HERMES_DEVTEST_PARENT_CANARY": "parent-env-canary",
                                                "PGHOST": "prod-db.example.com", "PGPASSWORD": "parent-pg-canary"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.identity = rl.repo_identity(str(self.checkout), rl.default_git)
        self.owner = {"board": "default", "task": TASK, "run_id": str(RUN_ID), "pid": os.getpid(),
                      "pid_start": rl.proc_start_ticks(os.getpid()), "host": socket.gethostname()}
        lease = rl.read_lease(self.identity)
        if lease:
            os.unlink(rl.lease_path(self.identity))
        self.assertEqual(rl.acquire_lease(self.identity, self.owner)["status"], "acquired")
        self.config = self._write_config(self._config())

    # ------------------------------------------------------------ fixtures ---

    def _board(self, *, status, worker_pid, run_status, current_run=RUN_ID, claim_expires=None):
        if self.board.exists():
            self.board.unlink()
        conn = sqlite3.connect(self.board)
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, worker_pid INTEGER, "
                     "current_run_id INTEGER, claim_expires INTEGER)")
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, worker_pid INTEGER)")
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", (TASK, status, worker_pid, current_run, claim_expires))
        conn.execute("INSERT INTO task_runs VALUES (?, ?, ?, ?)", (RUN_ID, TASK, run_status, worker_pid))
        conn.commit()
        conn.close()

    def _config(self, **db_overrides) -> dict:
        db = {"host": "127.0.0.1", "port": self.db_port, "dbname": "matching_test", "user": "postgres",
              "sslmode": "disable", "url_env": "MATCHING_TEST_DATABASE_URL", "pgpassfile": str(self.pgpass)}
        db.update(db_overrides)
        return {"version": 1, "helper": "/usr/local/libexec/hermes-devtest-runner", "helper_sha256": "0" * 64,
                "python": "/usr/bin/python3", "run_as_uid": os.getuid(), "kanban_db": str(self.board),
                "lease_dir": str(self.state / "leases"), "state_dir": str(self.devstate), "go": GO,
                "gomodcache": os.path.expanduser("~/go/pkg/mod"),
                "profiles": {"probe": {"checkout": str(self.checkout), "module": "svc", "read_roots": ["svc"],
                                       "packages": ["./..."], "database": db,
                                       "isolation": {"mode": "same-uid-landlock"}}}}

    def _write_config(self, cfg: dict, mode: int = 0o644) -> str:
        path = self.etc / "profiles.json"
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(cfg))
        os.chmod(path, mode)
        return str(path)

    def _main(self, argv, regression=True, **kw) -> tuple:
        """regression=True exercises the components of the REJECTED same-uid design (what these
        regression tests cover); the production entry point (regression=False) refuses it."""
        out = io.StringIO()
        kw.setdefault("worker_pid", os.getpid())
        if regression:
            kw["allow_rejected_isolation_for_regression_tests"] = True
        with contextlib.redirect_stdout(out):
            code = dt.main(argv, config_path=self.config, trusted_uid=os.getuid(), **kw)
        text = out.getvalue()
        return code, json.loads(text), text

    def _run_args(self, *extra) -> list:
        return ["run", "--task", TASK, "--run-id", str(RUN_ID), "--profile", "probe", *extra]

    # -------------------------------------------------------------- happy ---

    def test_sandboxed_race_run_counts_and_isolation(self):
        code, result, text = self._main(self._run_args("--race", "--run", "^Test(Passes|Skipped|CleanEnvironment|Sandbox)$",
                                                       "--timeout", "600"))
        self.assertEqual(code, 0, text[-3000:])
        self.assertTrue(result["ok"])
        self.assertEqual(result["tests"], {"passed": 3, "failed": 0, "skipped": 1})
        self.assertEqual(result["sandbox"]["tcp_connect"], [self.db_port])
        self.assertIn("any host", result["sandbox"]["network_limit"])
        self.assertNotIn("confined to loopback", json.dumps(result["sandbox"]).lower())
        self.assertEqual(result["skipped_tests"][0]["test"], "TestSkipped")
        self.assertIn("needs the synthetic fixture", result["skipped_tests"][0]["reason"])
        self.assertEqual(result["argv"][:5], ["go", "test", "-json", "-count=1", "-p=1"])
        self.assertIn("-race", result["argv"])
        self.assertEqual(result["database"]["url"],
                         f"postgresql://postgres@127.0.0.1:{self.db_port}/matching_test?sslmode=disable")
        self.assertNotIn(CANARY, text)
        self.assertFalse(list(self.devstate.glob("run-*")), "per-run temp dir must be removed")

    def test_failing_test_output_is_scrubbed_and_exit_is_real(self):
        code, result, text = self._main(self._run_args("--no-race", "--run", "^TestLeakFails$", "--vet"))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["tests"]["failed"], 1)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertNotIn(CANARY, text)
        self.assertIn("[REDACTED]", result["failed_tests"][0]["output"])
        self.assertEqual(result["vet"]["exit_code"], 0)
        self.assertNotIn("-race", result["argv"])

    def _helper_copy(self, regression: bool = False) -> str:
        """The runner with only its two trust anchors pointed at this fixture (and, for the
        regression variant, the env opt-in constant flipped; the kit copy keeps it False)."""
        src = RUNNER.read_text()
        src = src.replace('CONFIG_PATH = "/usr/local/etc/hermes-devtest/profiles.json"', f"CONFIG_PATH = {self.config!r}")
        src = src.replace("TRUSTED_UID = 0\n", f"TRUSTED_UID = {os.getuid()}\n")
        if regression:
            assert "REGRESSION_ENV_OPT_IN = False" in src
            src = src.replace("REGRESSION_ENV_OPT_IN = False", "REGRESSION_ENV_OPT_IN = True")
        path = self.etc / ("hermes-devtest-runner-regression" if regression else "hermes-devtest-runner")
        path.write_text(src)
        os.chmod(path, 0o755)
        return str(path)

    def test_invoked_exactly_like_the_runtime_tool(self):
        helper = self._helper_copy()
        argv = ["/usr/bin/python3", "-I", "-S", helper, *self._run_args("--run", "^TestPasses$", "--no-race")]
        proc = subprocess.run(argv, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/", capture_output=True,
                              text=True, timeout=600, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 2, "the production runner copy refuses the rejected design")
        self.assertIn("rejected", json.loads(proc.stdout)["error"])
        proc = subprocess.run(argv, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                                      "HERMES_DEVTEST_REGRESSION_UNSAFE": "1"}, cwd="/",
                              capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 2, "the env variable alone does nothing to the kit copy")
        # Only the regression copy (REGRESSION_ENV_OPT_IN flipped in a private file) honours it.
        argv[3] = self._helper_copy(regression=True)
        proc = subprocess.run(argv, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                                      "HERMES_DEVTEST_REGRESSION_UNSAFE": "1"}, cwd="/",
                              capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        result = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.assertEqual(result["tests"], {"passed": 1, "failed": 0, "skipped": 0})
        self.assertFalse(result["no_tests_ran"])
        self.assertEqual(result["isolation"], {"mode": "same-uid-landlock", "approved": False,
                                               "regression_only": True})
        # The same invocation through an intermediate shell: the parent is no longer the worker.
        shell = subprocess.run(["/bin/sh", "-c", " ".join(argv) + "; :"], check=False,
                               env={"PATH": "/usr/bin:/bin", "HERMES_DEVTEST_REGRESSION_UNSAFE": "1"}, cwd="/",
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn("not running under this worker", json.loads(shell.stdout)["error"])

    # ---------------------------------------------------- argument refusal ---

    def test_injected_or_unapproved_arguments_are_refused(self):
        cases = [
            ["--toolexec", "/bin/sh"], ["-exec", "/bin/sh"], ["--overlay", "/tmp/o.json"], ["--modfile", "x"],
            ["--ldflags", "-X a=b"], ["--pro", "probe"], ["--package", "../other"], ["--package", "-race"],
            ["--package", "./a;rm"], ["--package", "/abs"], ["--run", "Test Foo"], ["--run", "`id`"], ["--run", "a;b"], ["--run", "x'y"],
            ["--timeout", "5"], ["--timeout", "99999"], ["--race", "--no-race"], ["--", "-toolexec=x"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                code, result, _ = self._main(self._run_args(*extra))
                self.assertEqual(code, 2, result)
                self.assertTrue(result["refused"])

    def test_bad_task_or_profile_is_refused(self):
        for argv in (["run", "--task", "x; rm", "--run-id", "1", "--profile", "probe"],
                     ["run", "--task", TASK, "--run-id", "0", "--profile", "probe"],
                     ["run", "--task", TASK, "--run-id", str(RUN_ID), "--profile", "prod"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._main(argv)[0], 2)

    def test_package_patterns(self):
        approved = ["./..."]
        for ok in ("./...", "./internal/db", "./internal/..."):
            self.assertTrue(dt.package_allowed(ok, approved), ok)
        for bad in ("../x", "./../x", "-race", "./a b", "all", "std", "./a/../b", "example.com/x"):
            self.assertFalse(dt.package_allowed(bad, approved), bad)
        self.assertFalse(dt.package_allowed("./cmd/x", ["./internal/..."]))

    # ------------------------------------------------------ config refusal ---

    def test_production_or_foreign_targets_are_refused(self):
        for override in ({"dbname": "matching"}, {"dbname": "validate_prod"}, {"host": "10.0.0.5"},
                         {"host": "db.prod.example.com"}, {"host": "localhost"}, {"sslmode": "require"},
                         {"url_env": "PGHOST"}, {"port": 80}, {"pgpassfile": "relative/pgpass"}):
            with self.subTest(override=override):
                self.config = self._write_config(self._config(**override))
                code, result, _ = self._main(self._run_args())
                self.assertEqual(code, 2, result)

    def test_config_trust_is_enforced(self):
        cfg = self._config()
        cfg["extra"] = True
        self.config = self._write_config(cfg)
        self.assertEqual(self._main(self._run_args())[0], 2)
        self.config = self._write_config(self._config(), mode=0o666)
        code, result, _ = self._main(self._run_args())
        self.assertEqual(code, 2)
        self.assertIn("writable", result["error"])
        self.config = self._write_config(self._config())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dt.main(self._run_args(), config_path=self.config, trusted_uid=0, worker_pid=os.getpid())
        self.assertEqual(code, 2)
        self.assertIn("owned by uid", out.getvalue())
        link = self.etc / "link.json"
        link.symlink_to(self.config)
        self.addCleanup(link.unlink)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dt.main(self._run_args(), config_path=str(link), trusted_uid=os.getuid(),
                                     worker_pid=os.getpid()), 2)

    def test_credential_file_metadata_is_enforced_without_printing_it(self):
        os.chmod(self.pgpass, 0o644)
        code, result, text = self._main(self._run_args())
        self.assertEqual(code, 2)
        self.assertIn("private", result["error"])
        self.assertNotIn(CANARY, text)
        os.chmod(self.pgpass, 0o600)
        link = self.secret_dir / "pgpass-link"
        link.symlink_to(self.pgpass)
        self.addCleanup(link.unlink)
        self.config = self._write_config(self._config(pgpassfile=str(link)))
        code, result, text = self._main(self._run_args())
        self.assertEqual(code, 2)
        self.assertNotIn(CANARY, text)
        self.config = self._write_config(self._config())
        self.pgpass.write_text(f"127.0.0.1:1:other_test:postgres:{CANARY}\n")
        code, result, text = self._main(self._run_args())
        self.assertEqual(code, 2)
        self.assertIn("no entry", result["error"])
        self.assertNotIn(CANARY, text)

    # ------------------------------------------------------------ binding ---

    def test_only_the_live_claimed_worker_with_the_lease_may_run(self):
        code, result, _ = self._main(self._run_args(), worker_pid=os.getppid())
        self.assertEqual(code, 2)
        self.assertIn("not running under this worker", result["error"])
        self._board(status="blocked", worker_pid=os.getpid(), run_status="blocked")
        self.assertEqual(self._main(self._run_args())[0], 2)
        self._board(status="running", worker_pid=os.getpid(), run_status="running", claim_expires=1)
        self.assertIn("claim expired", self._main(self._run_args())[1]["error"])
        self._board(status="running", worker_pid=os.getpid(), run_status="running")
        rl.release_lease(self.identity, self.owner)
        self.assertIn("no repository lease", self._main(self._run_args())[1]["error"])
        other = dict(self.owner, run_id="999")
        rl.acquire_lease(self.identity, other)
        self.assertIn("belongs to run", self._main(self._run_args())[1]["error"])
        rl.release_lease(self.identity, other)
        rl.acquire_lease(self.identity, self.owner, now=time.time() - 4000)
        self.assertIn("expired", self._main(self._run_args())[1]["error"])

    # ------------------------------------------------------------ sandbox ---

    def test_no_sandbox_means_no_run(self):
        with mock.patch.object(dt, "landlock_abi", return_value=0), \
                mock.patch.object(dt.subprocess, "Popen", side_effect=AssertionError("must not run")):
            code, result, _ = self._main(self._run_args())
        self.assertEqual(code, 3)
        self.assertTrue(result["sandbox_error"])

    def test_seccomp_program_shape(self):
        prog = dt.seccomp_program()
        self.assertEqual(len(prog) % 8, 0)
        self.assertLessEqual(len(prog) // 8, 64)

    # ------------------------------------------------------------- pieces ---

    def test_scrub(self):
        text = (f"pw {CANARY} enc {CANARY.replace('-', '%2D')} url postgres://u:hunter2@h/db "
                f"password=abc123 PASSWORD: 'x y'")
        out = dt.scrub(text, [CANARY])
        self.assertNotIn(CANARY, out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abc123", out)
        self.assertIn("postgres://u:[REDACTED]@h/db", out)

    def test_parse_go_json_build_failure(self):
        stdout = "\n".join(json.dumps(e) for e in [
            {"Action": "build-output", "ImportPath": "x/y", "Output": "y.go:3: undefined: z\n"},
            {"Action": "build-fail", "ImportPath": "x/y"},
            {"Action": "start", "Package": "x/y"},
            {"Action": "output", "Package": "x/y", "Output": "FAIL\tx/y [build failed]\n"},
            {"Action": "fail", "Package": "x/y"},
        ])
        parsed = dt.parse_go_json(stdout, "")
        self.assertEqual(parsed["packages"]["failed"], 1)
        self.assertEqual(parsed["failed_packages"], ["x/y"])

    def test_example_profile_is_valid(self):
        cfg = json.loads(EXAMPLE.read_text())
        prof = cfg["profiles"]["validate-matching"]
        self.assertEqual(prof["isolation"], {"mode": "same-uid-landlock"})
        self.assertFalse(dt.isolation_approved(prof))
        self.assertEqual(dt.database_url(prof["database"]),
                         "postgresql://postgres@127.0.0.1:55443/matching_test?sslmode=disable")
        self.assertEqual(prof["database"]["pgpassfile"], "/home/polymath-dev/.config/hermes-test-db/pgpass")
        self.assertEqual(prof["packages"], ["./..."])
        dt.validate_database(prof["database"], "example")

    # ------------------------------------------------ review regressions ---
    # Findings of the independent review of 3e84caa; each test failed against that runner.

    def test_production_entry_point_refuses_the_rejected_isolation_design(self):
        """The same-uid Landlock design does not meet the network/credential isolation
        requirements: main() fails closed on it, and on the not yet implemented approved mode."""
        with mock.patch.object(dt.subprocess, "Popen", side_effect=AssertionError("must not run")):
            code, result, text = self._main(self._run_args(), regression=False)
            self.assertEqual(code, 2, text)
            self.assertTrue(result["refused"])
            self.assertIn("same-uid-landlock", result["error"])
            self.assertIn("rejected", result["error"])
            self.assertNotIn(CANARY, text)
            cfg = self._config()
            cfg["profiles"]["probe"]["isolation"] = {"mode": "credential-broker-netns"}
            self.config = self._write_config(cfg)
            code, result, _ = self._main(self._run_args(), regression=False)
            self.assertEqual(code, 2)
            self.assertIn("not implemented", result["error"])
            cfg["profiles"]["probe"].pop("isolation")
            self.config = self._write_config(cfg)
            self.assertEqual(self._main(self._run_args(), regression=False)[0], 2)
            cfg["profiles"]["probe"]["isolation"] = {"mode": "trust-me"}
            self.config = self._write_config(cfg)
            self.assertEqual(self._main(self._run_args())[0], 2, "unknown modes are refused even in regression")

    def test_check_config_separates_validity_from_runnable_readiness(self):
        """Red against 042fbbe: check-config exited 0 and reported installable for a profile that
        merely declared the proposed credential-broker-netns mode string, although no runner
        version implements it. Validity (parses, well formed) and readiness (implemented backend)
        are reported separately and only readiness drives the exit code."""
        self.assertEqual(dt.IMPLEMENTED_ISOLATION, frozenset(), "no isolation backend is implemented")
        for mode, bucket in (("credential-broker-netns", "not_implemented_profiles"),
                             ("same-uid-landlock", "rejected_profiles")):
            with self.subTest(mode):
                cfg = self._config()
                cfg["profiles"]["probe"]["isolation"] = {"mode": mode}
                self.config = self._write_config(cfg)
                code, result, text = self._main(["check-config", "--config", self.config], regression=False)
                self.assertEqual(code, 1, text)
                self.assertTrue(result["valid"])
                self.assertFalse(result["installable"])
                self.assertFalse(result["runnable"])
                self.assertFalse(result["ok"])
                self.assertEqual(result[bucket], ["probe"])
                self.assertFalse(result["profiles"]["probe"]["runnable"])
                self.assertEqual(result["implemented_isolation_modes"], [])
        prof = {"isolation": {"mode": "credential-broker-netns", "attested": True, "ready": True}}
        self.assertTrue(dt.isolation_approved(prof), "syntactic recognition")
        self.assertFalse(dt.isolation_runnable(prof), "a mode string plus flags is never readiness")


    def test_network_rule_is_the_database_port_only_and_described_honestly(self):
        run_pgpass = self.tmp / "run-pgpass"
        run_pgpass.write_text("x\n")
        plan = dt.build_plan(self._config(), self._config()["profiles"]["probe"], str(self.tmp), str(self.tmp / "gc"),
                             str(run_pgpass))
        self.addCleanup(plan.close)
        self.assertEqual(plan.connect_ports, [self.db_port])
        self.assertNotIn(str(self.pgpass), [path for path, _ in plan.ro_files])
        desc = plan.describe()
        self.assertEqual(desc["tcp_connect"], [self.db_port])
        self.assertIn("any host", desc["network_limit"])
        self.assertNotIn("loopback ephemeral", json.dumps(desc))

    def _probe_python(self, code: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a python snippet under the real sandbox with a minimal plan (no network, no secrets)."""
        plan = dt.SandboxPlan()
        plan.rx = ["/usr", "/lib", "/lib64", "/bin"]
        plan.ro = ["/etc"]
        work = Path(tempfile.mkdtemp(prefix="probe-", dir=self.tmp))
        plan.rw = [str(work)]
        plan.rw_files = ["/dev/null", "/dev/urandom"]
        abi = dt.landlock_abi()
        self.assertGreaterEqual(abi, dt.LL_MIN_ABI)
        return subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", code], capture_output=True, text=True, timeout=timeout,
                              env={"PATH": "/usr/bin:/bin"}, cwd=str(work), stdin=subprocess.DEVNULL,
                              preexec_fn=dt.sandbox_preexec(plan, abi), start_new_session=True)

    def test_seccomp_allows_only_tcp_stream_sockets(self):
        """socket(2) protocol is checked: SCTP (132) and MPTCP (262) stream sockets, UNIX and UDP
        get EACCES; AF_INET/AF_INET6 SOCK_STREAM with protocol 0 or IPPROTO_TCP are allowed."""
        code = r"""
import errno, socket, json
out = {}
def attempt(name, *args):
    try:
        s = socket.socket(*args); s.close(); out[name] = "ok"
    except OSError as exc:
        out[name] = errno.errorcode.get(exc.errno, str(exc.errno))
attempt("tcp0", socket.AF_INET, socket.SOCK_STREAM, 0)
attempt("tcp6", socket.AF_INET6, socket.SOCK_STREAM, 6)
attempt("tcp_cloexec", socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK, 6)
attempt("sctp", socket.AF_INET, socket.SOCK_STREAM, 132)
attempt("mptcp", socket.AF_INET, socket.SOCK_STREAM, 262)
attempt("udp", socket.AF_INET, socket.SOCK_DGRAM, 0)
attempt("unix", socket.AF_UNIX, socket.SOCK_STREAM, 0)
attempt("raw", socket.AF_INET, socket.SOCK_RAW, 1)
attempt("netlink", 16, socket.SOCK_RAW, 0)
print(json.dumps(out))
"""
        proc = self._probe_python(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual({k: out[k] for k in ("tcp0", "tcp6", "tcp_cloexec")}, {"tcp0": "ok", "tcp6": "ok", "tcp_cloexec": "ok"})
        for name in ("sctp", "mptcp", "udp", "unix", "raw", "netlink"):
            self.assertEqual(out[name], "EACCES", f"{name}: {out[name]}")

    def test_source_rules_hold_verified_descriptors_and_skip_hardlinks(self):
        plan = dt.SandboxPlan()
        try:
            dt.source_rules(plan, str(self.checkout), ["svc"])
            names = sorted(os.path.basename(fd_path) for fd_path, _fd in plan.ro_files)
            self.assertIn("probe_test.go", names)
            self.assertIn(".env.example", names)
            self.assertNotIn(".env", names)
            self.assertNotIn("env-alias.txt", names, "a hardlink of .env is not granted (st_nlink > 1)")
            for path, fd in plan.ro_files:
                st = os.fstat(fd)
                self.assertTrue(stat.S_ISREG(st.st_mode))
                self.assertEqual(st.st_nlink, 1)
                self.assertEqual((st.st_dev, st.st_ino), (os.lstat(path).st_dev, os.lstat(path).st_ino))
            for path, fd in plan.ro_dirs_listing:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            plan.close()

    def test_secret_in_a_test_name_is_scrubbed_from_every_key_and_value(self):
        code, result, text = self._main(self._run_args("--no-race", "--run", "^TestLeakName$"))
        self.assertEqual(code, 1, text[-2000:])
        self.assertEqual(result["tests"]["failed"], 2)  # parent + named subtest
        self.assertNotIn(CANARY, text)
        self.assertNotIn(OTHER_CANARY, text)
        self.assertTrue(any("[REDACTED]" in item["test"] for item in result["failed_tests"]), result["failed_tests"])

    def test_scrub_object_covers_keys_and_runs_before_truncation(self):
        obj = {f"name/{CANARY}": {CANARY: [CANARY, {"k": f"x{CANARY}y"}]}, "ok": True, "n": 3}
        out = dt.scrub_object(obj, [CANARY])
        self.assertNotIn(CANARY, json.dumps(out))
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["n"], 3)
        long_text = CANARY + "x" * 10_000
        self.assertNotIn(CANARY[:8], dt.scrub(long_text, [CANARY])[:12])

    def test_filter_matching_no_tests_fails_validation(self):
        code, result, text = self._main(self._run_args("--no-race", "--run", "^TestNoSuchTestExists$"))
        self.assertEqual(code, 1, text[-2000:])
        self.assertFalse(result["ok"])
        self.assertTrue(result["no_tests_ran"])
        self.assertIn("matched no tests", result["error"])

    def test_malformed_lease_is_refused_not_crashed(self):
        path = Path(rl.lease_path(self.identity))
        good = json.loads(path.read_text())
        bad_cases = [
            ("owner-not-object", dict(good, owner="x")),
            ("renewed-not-number", dict(good, renewed_at="abc")),
            ("ttl-unbounded", dict(good, ttl=10**9, renewed_at=time.time() - 100_000)),
            ("ttl-string", dict(good, ttl="forever")),
            ("board-mismatch", dict(good, owner=dict(good["owner"], board="other"))),
            ("pid-string", dict(good, owner=dict(good["owner"], pid=str(os.getpid())))),
            ("not-json", None),
        ]
        for name, payload in bad_cases:
            with self.subTest(name):
                path.write_text("{not json" if payload is None else json.dumps(payload))
                code, result, text = self._main(self._run_args())
                self.assertEqual(code, 2, text[-500:])
                self.assertTrue(result["refused"])
        path.write_text(json.dumps(good))
        self.assertEqual(self._main(self._run_args("--run", "^TestPasses$", "--no-race"))[0], 0)

    def test_build_cache_is_per_profile(self):
        self.assertEqual(dt.build_cache_dir("/s", "probe"), "/s/go-build/probe")
        self.assertNotEqual(dt.build_cache_dir("/s", "probe"), dt.build_cache_dir("/s", "other"))

    def test_only_the_matching_pgpass_entry_reaches_the_sandbox(self):
        code, result, text = self._main(self._run_args("--no-race", "--run", "^TestSandbox$"))
        self.assertEqual(code, 0, text[-3000:])
        self.assertNotIn(str(self.pgpass), json.dumps(result["sandbox"]), "the operator's file itself is not granted")
        self.assertNotIn(OTHER_CANARY, text)
        self.assertNotIn(CANARY, text)
        self.assertNotEqual(result["database"]["pgpassfile"], str(self.pgpass))

    def test_timeout_cleanup_is_bounded_even_with_escaped_descendants(self):
        plan = dt.SandboxPlan()
        plan.rx = ["/usr", "/lib", "/lib64", "/bin"]
        plan.ro = ["/etc"]
        plan.rw = [str(self.tmp)]
        plan.rw_files = ["/dev/null"]
        started = time.monotonic()
        with mock.patch.object(dt, "KILL_GRACE", 2):
            res = dt.run_sandboxed(["/bin/sh", "-c", "setsid sleep 25 >/dev/null 2>&1 & sleep 25"], {"PATH": "/usr/bin:/bin"},
                                   str(self.tmp), plan, dt.landlock_abi(), timeout=1)
        self.assertLess(time.monotonic() - started, 10, "cleanup must not wait for escaped descendants")
        self.assertTrue(res["timed_out"])
        self.assertTrue(res["cleanup"]["bounded"])
        self.assertIn("not contained", res["cleanup"]["note"])

    def test_vet_budget_is_the_remaining_time_of_the_run(self):
        self.assertEqual(dt.remaining_budget(deadline=1000.0, now=400.0), 600)
        self.assertEqual(dt.remaining_budget(deadline=1000.0, now=990.0), dt.MIN_VET_BUDGET)
        self.assertEqual(dt.remaining_budget(deadline=100.0, now=400.0), dt.MIN_VET_BUDGET)


@unittest.skipUnless(shutil.which("bash"), "bash required")
class InstallerTests(unittest.TestCase):
    """scripts/install-hermes-devtest.sh must fail closed: missing or untrusted install is a non-zero
    status, and a profile whose isolation design is rejected cannot be planned or installed."""

    SCRIPT = REPO / "scripts" / "install-hermes-devtest.sh"

    def _run(self, *args, env_extra=None):
        tmp = Path(tempfile.mkdtemp(prefix="installer-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, HERMES_DEVTEST_DEST_RUNNER=str(tmp / "runner"), HERMES_DEVTEST_DEST_DIR=str(tmp / "etc"),
                   **(env_extra or {}))
        return subprocess.run(["bash", str(self.SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)

    def test_status_is_nonzero_when_nothing_is_installed(self):
        proc = self._run("status")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("missing", proc.stdout)
        self.assertIn("NOT INSTALLED", proc.stdout)

    def test_plan_and_install_refuse_a_rejected_isolation_design(self):
        for cmd in ("plan", "install"):
            with self.subTest(cmd):
                proc = self._run(cmd, env_extra={"SUDO": "/bin/false"})
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("REJECTED", proc.stdout + proc.stderr)
                self.assertIn("same-uid-landlock", proc.stdout + proc.stderr)


    def _profiles_with_mode(self, mode: str) -> str:
        cfg = json.loads(EXAMPLE.read_text())
        cfg["profiles"]["validate-matching"]["isolation"] = {"mode": mode}
        tmp = Path(tempfile.mkdtemp(prefix="installer-profiles-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "profiles.json"
        path.write_text(json.dumps(cfg))
        return str(path)

    def test_plan_and_install_refuse_the_unimplemented_proposed_design(self):
        """Red against 042fbbe: plan/install accepted a profile whose isolation.mode was the proposed
        broker string although nothing implements it (install would have gone ahead with sudo)."""
        profiles = self._profiles_with_mode("credential-broker-netns")
        for cmd in ("plan", "install"):
            with self.subTest(cmd):
                proc = self._run(cmd, "--profiles", profiles, env_extra={"SUDO": "/bin/false"})
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("NOT IMPLEMENTED", proc.stderr)
                self.assertIn("mode string is not readiness", proc.stderr)
                self.assertNotIn("REJECTED", proc.stderr, "a valid proposed-mode profile is not a rejected design")
                self.assertNotIn("plan only; run:", proc.stdout)
                self.assertNotIn("installed;", proc.stdout)

    def test_status_is_not_ready_for_an_installed_proposed_mode_registry(self):
        """A trusted-looking install whose registry declares the proposed mode must not report
        ready: status exits non-zero and names the missing implementation."""
        tmp = Path(tempfile.mkdtemp(prefix="installer-status-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        dest_runner = tmp / "runner"
        shutil.copy(RUNNER, dest_runner)
        os.chmod(dest_runner, 0o755)
        etc = tmp / "etc"
        etc.mkdir()
        cfg = json.loads(EXAMPLE.read_text())
        cfg["helper"] = str(dest_runner)
        cfg["helper_sha256"] = __import__("hashlib").sha256(dest_runner.read_bytes()).hexdigest()
        cfg["profiles"]["validate-matching"]["isolation"] = {"mode": "credential-broker-netns"}
        (etc / "profiles.json").write_text(json.dumps(cfg))
        env = dict(os.environ, HERMES_DEVTEST_DEST_RUNNER=str(dest_runner), HERMES_DEVTEST_DEST_DIR=str(etc))
        proc = subprocess.run(["bash", str(self.SCRIPT), "status"], capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("NOT RUNNABLE", proc.stdout)
        self.assertIn("NOT INSTALLED / not trusted", proc.stdout)
        self.assertNotIn("INSTALLED and trusted", proc.stdout)
        self.assertNotIn("approved isolation design", proc.stdout)


if __name__ == "__main__":
    unittest.main()
