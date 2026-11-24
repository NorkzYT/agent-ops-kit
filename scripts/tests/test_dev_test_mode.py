#!/usr/bin/env python3
"""Development-test mode (AOK_REQUIRE_DEV_TEST_RUN / AOK_TEST_BACKEND, `Dev-test-mode:` card header).

Covers: resolution (true/false, invalid, conflicts, CLI vs environment); card rendering per mode
and the byte-identical default; durable per-card mode across rounds and existing-card
compatibility; worker-environment conflicts fail closed; completion needs (db-tests evidence
bound to the backend, handoff, block-check); no dev_test_run or docker invocation by the
workflow script even when such executables are on PATH; and real subprocess inheritance of the
settings through a controlled launcher -> worker -> terminal chain.

What these tests prove is the workflow script's own behaviour. They do not (and cannot) prove
that an agent never calls a dev_test_run tool: that rule is card text (instruction only).

    python3 -m unittest discover -s scripts/tests -p 'test_dev_test_mode.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_completion as tc  # noqa: E402
import test_kanban_workflow as tk  # noqa: E402

wf = tk.wf
nw = tk.nw
tm = wf.tm
SCRIPT = tk.SCRIPT
ENV_KEYS = (tm.ENV_REQUIRE, tm.ENV_BACKEND)
COMPOSE_ENV = {tm.ENV_REQUIRE: "false", tm.ENV_BACKEND: "compose"}
DTR_ENV = {tm.ENV_REQUIRE: "true"}

# sha256 of the default renders at cb6f377 (main, the commit this release starts from, and the
# generator that rendered existing default cards such as example-app's), with the rendering
# workflow.py path replaced by <SCRIPT>. The default mode must keep them. When the default card
# text itself is changed on purpose, re-pin these from that commit.
CB6F377_DEFAULT = {
    "code": "1e5087a2d3f5585074a2ad4d1565fa32d79808d5f40c82110178fd51b0fe70bc",
    "round": "4bb7bb40b0b5485d5b74159d221880fc5a2ae1fdce7cf60d249453982c93b8b4",
    "review": "bb5744c12965de53e2d39d07f1cd16d50b0d1b3d86c0a2b2015fbb72a6ad0b49",
    "code/opt/repos/app": "ac2bed78a8d7bdc88f5660211f889b610273ff4e4847853c2db24aa0c09a541c",
    "round/opt/repos/app": "5a0604b18dfa081b89f0d696e0e09e260a86b7638b5fdeae0521ed90ea2babb4",
    "review/opt/repos/app": "0e79eacaae139f5007855546c7d61d01dbcef10fc4044250bd6aa38c0ada7024",
    "db": "6b29384856c92856522396762d549348df43f673364d71b021820bf07cc9f0b5",
    "scan": "4e628e433ab29d2b8a139630bb2371d34900f51251e05a045f9092ea33859b9d",
}


# A throwaway HERMES_HOME so no test reads the live config.yaml for the native setting.
ISOLATED_HOME = tempfile.mkdtemp(prefix="dev-test-mode-home-")


def clean_env(**extra) -> dict:
    """os.environ without the two settings (any case) and with an empty HERMES_HOME, plus *extra*."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in ENV_KEYS}
    env["HERMES_HOME"] = ISOLATED_HOME
    env.update(extra)
    return env


def no_mode_env():
    """Patch the process environment so neither setting is present."""
    return mock.patch.dict(os.environ, clean_env(), clear=True)


def mode_env(values: dict):
    return mock.patch.dict(os.environ, clean_env(**values), clear=True)


def ctx(mode="", checkout=""):
    return wf.Ctx(key="k", title="T", repo="https://github.com/acme/app.git", branch="feat/x", base="main",
                  lanes={"mac-worker": "mac", "windows-worker": "windows"}, coder="coder", reviewer="reviewer",
                  orchestrator="orchestrator", checkout=checkout, dev_test_mode=mode)


def digest(text: str) -> str:
    return hashlib.sha256(text.replace(str(wf.SCRIPT), "<SCRIPT>").encode()).hexdigest()


class ResolutionTest(unittest.TestCase):
    def test_environment_table(self):
        ok = [({}, ""), ({tm.ENV_REQUIRE: ""}, ""), ({tm.ENV_REQUIRE: " ", tm.ENV_BACKEND: ""}, ""),
              ({tm.ENV_REQUIRE: "true"}, "dev_test_run"), ({tm.ENV_REQUIRE: "TRUE"}, "dev_test_run"),
              ({tm.ENV_REQUIRE: "true", tm.ENV_BACKEND: "dev_test_run"}, "dev_test_run"),
              ({tm.ENV_REQUIRE: "false", tm.ENV_BACKEND: "compose"}, "compose"),
              ({tm.ENV_REQUIRE: " False ", tm.ENV_BACKEND: "compose"}, "compose"),
              ({tm.ENV_BACKEND: "compose"}, "compose"), ({tm.ENV_BACKEND: "dev_test_run"}, "dev_test_run")]
        for env, want in ok:
            self.assertEqual(tm.from_env(env), want, env)

    def test_invalid_and_conflicting_values_fail_closed(self):
        bad = [({tm.ENV_REQUIRE: "yes"}, "is not true or false"), ({tm.ENV_REQUIRE: "1"}, "is not true or false"),
               ({tm.ENV_REQUIRE: "0"}, "is not true or false"),
               ({tm.ENV_BACKEND: "podman"}, "is not one of"), ({tm.ENV_BACKEND: "Compose"}, "is not one of"),
               ({tm.ENV_BACKEND: "dev-test-run"}, "is not one of"),
               ({tm.ENV_REQUIRE: "true", tm.ENV_BACKEND: "compose"}, "conflicts"),
               ({tm.ENV_REQUIRE: "false"}, "needs AOK_TEST_BACKEND=compose"),
               ({tm.ENV_REQUIRE: "false", tm.ENV_BACKEND: "dev_test_run"}, "needs AOK_TEST_BACKEND=compose")]
        for env, text in bad:
            with self.assertRaises(tm.ModeError, msg=env) as cm:
                tm.from_env(env)
            self.assertIn(text, str(cm.exception), env)

    def test_cli_and_environment(self):
        self.assertEqual(tm.resolve_intake(None, {}), {"mode": "", "source": "default"})
        self.assertEqual(tm.resolve_intake(None, COMPOSE_ENV), {"mode": "compose", "source": "environment"})
        self.assertEqual(tm.resolve_intake("compose", {}), {"mode": "compose", "source": "cli"})
        self.assertEqual(tm.resolve_intake("compose", COMPOSE_ENV), {"mode": "compose", "source": "cli"})
        self.assertEqual(tm.resolve_intake("default", {}), {"mode": "", "source": "cli"})
        for cli, env in (("dev_test_run", COMPOSE_ENV), ("default", COMPOSE_ENV), ("compose", DTR_ENV)):
            with self.assertRaises(tm.ModeError):
                tm.resolve_intake(cli, env)
        with self.assertRaises(tm.ModeError):
            tm.resolve_intake("docker", {})
        with self.assertRaises(tm.ModeError):  # an invalid environment is not overridden by a CLI value
            tm.resolve_intake("compose", {tm.ENV_REQUIRE: "maybe"})

    def test_card_header_and_worker_check(self):
        self.assertEqual(tm.card_mode({}), "")
        self.assertEqual(tm.card_mode({"dev-test-mode": "compose"}), "compose")
        with self.assertRaises(tm.ModeError):
            tm.card_mode({"dev-test-mode": "docker"})
        self.assertEqual(tm.check_worker("compose", {})["mode"], "compose")  # card wins over an unset env
        self.assertEqual(tm.check_worker("compose", COMPOSE_ENV)["environment"], "compose")
        with self.assertRaises(tm.ModeError):
            tm.check_worker("compose", DTR_ENV)
        with self.assertRaises(tm.ModeError):
            tm.check_worker("dev_test_run", COMPOSE_ENV)
        with self.assertRaises(tm.ModeError):  # an invalid worker env fails closed even on a matching card
            tm.check_worker("compose", {tm.ENV_REQUIRE: "nah"})
        with self.assertRaises(tm.ModeError):  # a card without a mode on a worker set to one: refused
            tm.check_worker("", COMPOSE_ENV)
        self.assertEqual(tm.check_worker("", {})["mode"], "default")

    def test_check_cli(self):
        for env, code, out in ((clean_env(), 0, {"mode": "default", "source": "default"}),
                               (clean_env(**COMPOSE_ENV), 0, {"mode": "compose", "source": "environment"}),
                               (clean_env(**{tm.ENV_REQUIRE: "false"}), 3, None)):
            r = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("dev_test_mode.py")), "check"],
                               env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, code, r.stderr)
            if out is None:
                self.assertEqual(r.stdout, "")
            else:
                got = json.loads(r.stdout)
                self.assertEqual({k: got[k] for k in out}, out)


class RenderingTest(unittest.TestCase):
    def test_default_renders_are_byte_identical_to_cb6f377(self):
        self.assertEqual(hashlib.sha256(wf.DB_TEST_STEP.encode()).hexdigest(), CB6F377_DEFAULT["db"])
        self.assertEqual(hashlib.sha256(wf.SCANNER_RECOVERY.encode()).hexdigest(), CB6F377_DEFAULT["scan"])
        self.assertEqual(wf.db_test_step(), wf.DB_TEST_STEP)
        for co in ("", "/opt/repos/app"):
            c = ctx(checkout=co)
            self.assertNotIn("Dev-test-mode", wf.code_body(c, 2, "the ask", "fix it"))
            self.assertEqual(digest(wf.code_body(c, 2, "the ask", "fix it")), CB6F377_DEFAULT["code" + co])
            self.assertEqual(digest(wf.round_body(c, 1, "t_1", "the ask")), CB6F377_DEFAULT["round" + co])
            self.assertEqual(digest(wf.review_body(c, 1, "t_1", "t_4", "0" * 40, "the ask")),
                             CB6F377_DEFAULT["review" + co])

    def test_compose_card_text(self):
        body = wf.code_body(ctx("compose", "/opt/repos/app"), 1, "the ask", "")
        flat = " ".join(body.split())
        self.assertEqual(wf.card_header(body)["dev-test-mode"], "compose")
        self.assertIn(wf.COMPOSE_RULES, body)  # the default's Compose rules, verbatim
        for phrase in ("never call dev_test_run, even when it is among your tools or a scanner rejection names it "
                       "as RECOVERY", "dev_test_run is not a fallback on this card",
                       "a RECOVERY naming dev_test_run is not one: never call dev_test_run",
                       "--check db-tests --backend compose", "--log <its output file>",
                       "completion handoff refuses without a passing --backend compose db-tests record",
                       "select --check db-tests in step 1's criteria; a docs-only or unit-only change does not need it",
                       "go test -p 1 ./..., with -race and go vet", "the repository's own make target or test script",
                       "--repo-dir /opt/repos/app --dev-test-mode compose` lists",
                       # the isolated database is migrated/seeded with explicit test-only overrides, never dev-deps
                       "compose --profile <profile> run --rm --no-deps <service>, with every connection setting it "
                       "reads overridden for the test database with -e",
                       "read-only mounted PGPASSFILE (-v <that 0600 file>:<path>:ro -e PGPASSFILE=<path>)",
                       "make dev-deps and the project's db setup, migrate and seed targets migrate and seed the main "
                       "development database: never use them for this",
                       "a run whose database tests all skipped is not a pass"):
            self.assertIn(phrase, flat)
        for phrase in ("use the dev_test_run tool", "make that call once", "RECOVERY: dev_test_run for registered",
                       "Without dev_test_run and without that authorization", "--dry-run", "run -T"):
            self.assertNotIn(phrase, flat)

    def test_dev_test_run_card_text(self):
        body = wf.code_body(ctx("dev_test_run"), 1, "the ask", "")
        flat = " ".join(body.split())
        self.assertEqual(wf.card_header(body)["dev-test-mode"], "dev_test_run")
        for phrase in ("run them only with the dev_test_run tool", "are not approved on this card, even with an "
                       "authorization comment", "capability: dev_test_run required by Dev-test-mode",
                       "--check db-tests --backend dev_test_run"):
            self.assertIn(phrase, flat)
        self.assertNotIn(wf.COMPOSE_RULES, body)
        self.assertNotIn("up -d --no-deps --no-recreate", flat)
        self.assertEqual(wf.scanner_recovery("dev_test_run"), wf.SCANNER_RECOVERY)

    def test_every_mode_embeds_only_the_rendering_script_path(self):
        # The body-plan path guard compares embedded `python3 <workflow.py>` paths: a mode paragraph
        # frozen at import time would carry another path than the rest of the card once SCRIPT is the
        # cards' canonical path, and the plan would refuse every real card.
        canonical = Path("/opt/repos/agent-ops-kit/hermes/skills/kanban-workflow/scripts/workflow.py")
        with mock.patch.object(wf, "SCRIPT", canonical):
            for mode in ("", "compose", "dev_test_run"):
                body = wf.code_body(ctx(mode, "/opt/repos/app"), 1, "the ask", "")
                self.assertEqual(wf.embedded_scripts(body), {str(canonical)}, mode)

    def test_skill_and_souls_agree_with_the_modes(self):
        repo = SCRIPT.parents[4]
        skill = " ".join((repo / "hermes/skills/kanban-workflow/SKILL.md").read_text().split())
        for phrase in ("step 3 follows the card's `Dev-test-mode`", "`compose`: never call `dev_test_run`",
                       "`dev_test_run`: only the `dev_test_run` tool", "its header only (a comment never sets a mode",
                       "On a `Dev-test-mode: compose` card a `RECOVERY:` naming `dev_test_run` is not an approved "
                       "equivalent", "host `go test -p 1 ./...`"):
            self.assertIn(phrase, skill)
        self.assertNotIn("(both are blocked)", skill)
        soul = " ".join((repo / "hermes/SOUL.md").read_text().split())
        self.assertIn("`compose` never calls `dev_test_run`", soul)
        coder = " ".join((repo / "hermes/profiles/coder/SOUL.md").read_text().split())
        self.assertIn("except on a `Dev-test-mode: compose` card header, which never calls "
                      "`dev_test_run`", coder)

    def test_header_round_trip_and_invalid_header(self):
        for mode in ("", "compose", "dev_test_run"):
            h = wf.card_header(wf.round_body(ctx(mode), 1, "t_1", "ask"))
            self.assertEqual(wf.Ctx.from_header(h).dev_test_mode, mode)
        h = wf.card_header(wf.round_body(ctx("compose"), 1, "t_1", "ask"))
        h["dev-test-mode"] = "docker"
        with self.assertRaises(wf.WorkflowError):
            wf.Ctx.from_header(h)


class IntakeModeTest(tk.StateDirMixin, unittest.TestCase):
    def cards_with_mode(self, fake):
        return {tid: wf.header_of(s).get("dev-test-mode") for tid, s in fake.shows.items()}

    def test_default_intake_has_no_header(self):
        fake = tk.FakeHermes()
        with no_mode_env():
            plan = tk.graph(fake)
        self.assertEqual(plan["dev_test_mode"], {"mode": "default", "source": "default"})
        self.assertEqual(set(self.cards_with_mode(fake).values()), {None})

    def test_environment_mode_is_recorded_on_the_cards_and_inherited_by_the_next_round(self):
        fake = tk.FakeHermes()
        with mode_env(COMPOSE_ENV):
            plan = tk.graph(fake)
        self.assertEqual(plan["dev_test_mode"], {"mode": "compose", "source": "environment"})
        modes = self.cards_with_mode(fake)
        self.assertEqual((modes["t_1"], modes["t_4"]), ("compose", "compose"))  # code + round cards
        self.assertIsNone(modes["t_2"])  # validation and report cards carry no Ctx header
        tk.push(fake)
        # The next round is rendered by a process WITHOUT the setting: the card header carries it.
        with no_mode_env():
            for tid in ("t_2", "t_3"):
                tk.validate(fake, tid, tk.evidence("fail", sha=tk.SHA, task=tid))
            code, out = nw.advance("t_4", fake)
        self.assertEqual(code, 0, out)
        (code2,) = fake.by_title("[code 2]")
        body = fake.body(code2)
        self.assertEqual(wf.card_header(body)["dev-test-mode"], "compose")
        self.assertIn("never call dev_test_run", body)

    def test_cli_flag_and_conflicts(self):
        fake = tk.FakeHermes()
        with no_mode_env():
            plan = tk.graph(fake, "--dev-test-mode", "dev_test_run")
        self.assertEqual(plan["dev_test_mode"], {"mode": "dev_test_run", "source": "cli"})
        self.assertEqual(wf.header_of(fake.shows["t_1"])["dev-test-mode"], "dev_test_run")
        for env, extra in ((COMPOSE_ENV, ("--dev-test-mode", "dev_test_run")), ({tm.ENV_REQUIRE: "maybe"}, ()),
                           ({tm.ENV_REQUIRE: "true", tm.ENV_BACKEND: "compose"}, ())):
            fake = tk.FakeHermes()
            with mode_env(env):
                code, out = tk.main_out(["intake", "--title", "T", "--repo", tk.REPO_URL, "--lane", "mac",
                                         "--key", "k2", *extra], fake)
            self.assertEqual(code, 3, env)  # capability: fail closed, nothing planned
            self.assertEqual(fake.shows, {}, env)
            self.assertEqual(fake.native_calls, [], env)

    def test_next_round_refuses_a_conflicting_worker_environment(self):
        fake = tk.FakeHermes()
        with mode_env(COMPOSE_ENV):
            tk.graph(fake)
        tk.push(fake)
        with mode_env(DTR_ENV):
            for tid in ("t_2", "t_3"):
                tk.validate(fake, tid, tk.evidence("fail", sha=tk.SHA, task=tid))
            code, _ = tk.main_out(["advance", "t_4"], fake)
        self.assertEqual(code, 3)
        self.assertEqual(fake.by_title("[code 2]"), [])

    def test_existing_default_card_is_refused_when_the_worker_sets_a_mode(self):
        fake = tk.FakeHermes()
        with no_mode_env():
            tk.graph(fake)  # an existing request, created before the setting
        tk.push(fake)
        with mode_env(COMPOSE_ENV):
            for tid in ("t_2", "t_3"):
                tk.validate(fake, tid, tk.evidence("fail", sha=tk.SHA, task=tid))
            code, _ = tk.main_out(["advance", "t_4"], fake)
        self.assertEqual(code, 3)  # not silently the default: the operator updates the card or the setting
        self.assertEqual(fake.by_title("[code 2]"), [])


class ModeCoordinator(tc.Coordinator):
    """A --checkout code card in a given mode; `db` adds a database stack (db/migrations)."""
    MODE_ENV: dict = {}
    DB = True

    def setUp(self):
        env = mode_env(self.MODE_ENV)
        env.start()
        self.addCleanup(env.stop)
        tk.CheckoutRepo.setUp(self)
        if self.DB:
            Path(self.co, "db", "migrations").mkdir(parents=True)
            Path(self.co, "db", "migrations", "001.sql").write_text("create table t (id int);\n")
            self.git("add", "db", cwd=self.co)
            self.git("commit", "-m", "chore: migrations", cwd=self.co)
            self.git("push", "origin", "main", cwd=self.co)
        self.intake("--checkout", self.co)
        self.git("checkout", "-b", tc.BRANCH, cwd=self.co)
        code, pre = nw.checkout_command("preflight", "t_1", self.co, run=self.fake, scan_runner=tc.SCAN)
        self.assertEqual(code, 0, pre)
        self.log = os.path.join(self.tmp, "db-tests.log")
        Path(self.log).write_text("ok  validate/internal/db  1.2s\n")

    def ready_except_db(self, db=True):
        """Criteria (selecting db-tests unless db=False), plan, implementation, unit/lint/diff, push."""
        if db:
            self.w("criteria", item=["matching query handles ties"], check=["unit", "lint", tm.DB_CHECK],
                   source=["card body"])
            self.w("plan", step=["fix the query", "add a database test"])
        else:
            self.understand_and_plan()
        self.implement()
        self.ev("pass", check="unit")
        self.ev("pass", check="lint")
        self.ev("pass", kind="review", check="diff")
        self.commit_and_push()

    def db_ev(self, backend, command, result="pass", passed=4, log=True, exit_code=None):
        return self.w("evidence", repo_dir=self.co, kind="test", check=tm.DB_CHECK, result=result,
                      command=command, exit=(0 if result == "pass" else 1) if exit_code is None else exit_code,
                      passed=passed, log=self.log if log else "", backend=backend)


COMPOSE_CMD = "docker compose -p example-dev exec -T example-dev-db go test ./internal/db/..."


class ComposeCompletionTest(ModeCoordinator, unittest.TestCase):
    MODE_ENV = COMPOSE_ENV

    def test_handoff_requires_passing_compose_db_tests(self):
        self.ready_except_db()
        v = self.view()
        self.assertTrue(v["dev_test_mode"]["db_tests_required"])
        self.assertEqual(v["dev_test_mode"]["mode"], "compose")
        self.assertIn("instruction_only", v["dev_test_mode"])
        test = self.stage("test", v)
        self.assertIn(tm.DB_CHECK, test["checks"])
        self.assertFalse(test["met"])
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 4)
        # Evidence that contradicts the mode or is not a test run is refused (exit 2), never recorded.
        for kw in ({"backend": "dev_test_run", "command": "dev_test_run(profile=example-service)"},
                   {"backend": "", "command": COMPOSE_CMD},
                   {"backend": "compose", "command": COMPOSE_CMD, "passed": 0},
                   {"backend": "compose", "command": COMPOSE_CMD, "log": False}):
            with self.assertRaises(wf.WorkflowError, msg=kw):
                self.db_ev(**kw)
        # The structured --backend is the rule on every check; the command text is not policed.
        with self.assertRaises(wf.WorkflowError):
            self.w("evidence", repo_dir=self.co, kind="test", check="unit", result="pass",
                   command="python3 -m unittest", exit=0, backend="dev_test_run")
        self.assertFalse(self.stage("test")["met"])
        # A failing compose run is recorded; it does not satisfy the check.
        self.db_ev("compose", COMPOSE_CMD, result="fail", passed=3, exit_code=1)
        self.assertFalse(self.stage("test")["met"])
        code, out = self.db_ev("compose", COMPOSE_CMD)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["record"]["backend"], "compose")
        self.assertEqual(out["record"]["log"]["sha256"], hashlib.sha256(Path(self.log).read_bytes()).hexdigest())
        self.assertTrue(self.stage("test")["met"])
        code, out = nw.completion_handoff("t_1", self.co, self.fake)
        self.assertEqual(code, 0, out)

    def test_records_that_bypass_the_cli_check_never_count(self):
        # A record written by an older script, under another mode or by hand: judged by the same
        # rule as ingestion, rejected cleanly (no exception) when malformed.
        self.ready_except_db()
        good = {"event": "evidence", "kind": "test", "check": tm.DB_CHECK, "result": "pass", "backend": "compose",
                "command": COMPOSE_CMD, "exit_code": 0, "counts": {"passed": 3},
                "log": {"sha256": "0" * 64, "bytes": 1}}
        self.assertTrue(tm.accepts("compose", good))
        for change in ({"backend": "dev_test_run"}, {"backend": None}, {"log": None},
                       {"exit_code": False}, {"exit_code": True}, {"exit_code": "0"}, {"exit_code": 0.0},
                       {"counts": {"passed": True}}, {"counts": {"passed": 1.9}}, {"counts": {"passed": "abc"}},
                       {"counts": ["x"]}, {"counts": {"passed": 3, "failed": -1}}, {"counts": {"passed": 3, "x": 1}},
                       {"counts": {"passed": 3, "failed": 1}}, {"counts": {}},
                       {"result": "PASS"}, {"result": "ok"}, {"exit_code": 9},
                       {"log": "x"}, {"log": {"a": 1}}, {"log": {"sha256": "x" * 64, "bytes": 1}},
                       {"log": {"sha256": "0" * 64, "bytes": True}}, {"log": {"sha256": "0" * 64, "bytes": 0}},
                       {"command": ""}, {"command": None}):
            ev = {**good, **change}
            self.assertFalse(tm.accepts("compose", ev), change)
            self.assertIsInstance(tm.db_record_problem("compose", ev), str, change)
            with self.assertRaises(tm.ModeError, msg=change):  # ingestion applies the same rule
                tm.check_db_record("compose", ev)
        for bad in (None, [], "evidence"):
            self.assertFalse(tm.accepts("compose", bad))
        # A failing record must still be well-formed (it is shown as the current failure).
        self.assertTrue(tm.accepts("compose", {**good, "result": "fail", "exit_code": 1, "log": None,
                                               "counts": {"passed": 2, "failed": 1}}))
        self.assertFalse(tm.accepts("compose", {**good, "result": "fail", "exit_code": True}))

    def test_a_malformed_card_record_does_not_crash_status_or_count(self):
        # Revalidation at read: a hand-written record with bool/float/string counts on the card.
        self.ready_except_db()
        source = self.view()["source"]
        for i, counts in enumerate(({"passed": True}, {"passed": "abc"}, ["x"], {"passed": 1.9})):
            rec = {"rid": f"completion:bad{i}", "event": "evidence", "kind": "test", "check": tm.DB_CHECK,
                   "result": "pass", "backend": "compose", "command": COMPOSE_CMD, "exit_code": 0, "counts": counts,
                   "source": {k: source[k] for k in ("head", "content_id", "clean", "branch")},
                   "log": {"sha256": "0" * 64, "bytes": 1}, "summary": "", "ts": 10**10 + i}
            self.fake.native("kanban_comment", {"task_id": "t_1", "body": wf.record_body({**rec, "type": "completion"})},
                             "default")
        test = self.stage("test")
        self.assertFalse(test["met"])
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 4)

    def test_valid_compose_paths_are_accepted(self):
        # Host go test, race/vet, make and script wrappers against the isolated Compose PostgreSQL.
        self.ready_except_db()
        for command in ("go test -p 1 ./...", "go test -p 1 -race ./...", "go vet ./...", "make test-db",
                        "./scripts/test-pg.sh", COMPOSE_CMD):
            code, out = self.db_ev("compose", command)
            self.assertEqual(code, 0, (command, out))
            self.assertEqual(out["record"]["command"], command)
        # An ordinary check whose command merely mentions the name is not refused.
        code, _ = self.w("evidence", repo_dir=self.co, kind="test", check="unit", result="pass",
                         command="go test ./internal/devtestrun/...", exit=0)
        self.assertEqual(code, 0)
        self.assertTrue(self.stage("test")["met"])

    def test_missing_dev_test_run_is_not_a_stop_on_a_compose_card(self):
        reason = "capability: go test ./internal/db/...; no dev_test_run tool; missing an operator-approved profile"
        code, out = self.w("block-check", reason=reason)
        self.assertEqual(code, 2)
        self.assertEqual(out["record"]["event"], "block-refused")
        self.assertIn("not this card's test path", out["record"]["detail"])
        # A Compose-path capability stop stays a genuine stop.
        code, _ = self.w("block-check", reason="capability: docker compose exec -T example-dev-db; service config-hash "
                                               "drifted; missing an approved up")
        self.assertEqual(code, 0)

    def test_worker_environment_conflict_fails_closed_and_release_still_works(self):
        with mode_env(DTR_ENV):
            for argv in (["completion", "status", "t_1", "--repo-dir", self.co],
                         ["completion", "criteria", "t_1", "--item", "x"],
                         ["preflight", "t_1", "--repo-dir", self.co]):
                code, _ = tk.main_out(argv, self.fake)
                self.assertEqual(code, 3, argv)
            code, out = tk.main_out(["release", "t_1", "--repo-dir", self.co], self.fake)
            self.assertEqual(code, 0, out)
        with mode_env({tm.ENV_REQUIRE: "bogus"}):
            self.assertEqual(tk.main_out(["completion", "status", "t_1", "--repo-dir", self.co], self.fake)[0], 3)

    def test_card_mode_holds_without_the_environment(self):
        self.w("criteria", item=["x"], check=["unit", tm.DB_CHECK], source=["card body"])
        with no_mode_env():
            v = self.view()
        self.assertEqual((v["dev_test_mode"]["mode"], v["dev_test_mode"]["environment"]), ("compose", "unset"))
        self.assertTrue(v["dev_test_mode"]["db_tests_required"])

    def test_capabilities_text(self):
        code, out = tk.main_out(["completion", "capabilities", "--repo-dir", self.co, "--dev-test-mode", "compose"],
                                self.fake)
        self.assertEqual(code, 0)
        need = " ".join(out["approved_capabilities_needed"])
        self.assertIn("Dev-test-mode: compose", need)
        self.assertIn("never dev_test_run", need)
        code, out = tk.main_out(["completion", "capabilities", "--repo-dir", self.co], self.fake)
        self.assertIn("a registered dev_test_run profile", " ".join(out["approved_capabilities_needed"]))


class ComposeNoDatabaseTest(ModeCoordinator, unittest.TestCase):
    MODE_ENV = COMPOSE_ENV
    DB = False

    def test_no_database_stack_needs_no_db_tests(self):
        self.ready_except_db(db=False)
        v = self.view()
        self.assertFalse(v["dev_test_mode"]["db_tests_required"])
        self.assertNotIn(tm.DB_CHECK, self.stage("test", v)["checks"])
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 0)


class ComposeUnitOnlyChangeTest(ModeCoordinator, unittest.TestCase):
    """A database directory in the checkout does not by itself demand db-tests: a unit or docs
    change whose criteria do not select the check hands off on its own checks."""
    MODE_ENV = COMPOSE_ENV
    DB = True

    def test_db_directory_alone_does_not_require_db_tests(self):
        self.ready_except_db(db=False)
        v = self.view()
        self.assertFalse(v["dev_test_mode"]["db_tests_required"])
        self.assertNotIn(tm.DB_CHECK, self.stage("test", v)["checks"])
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 0)


class DevTestRunCompletionTest(ModeCoordinator, unittest.TestCase):
    MODE_ENV = DTR_ENV

    def test_compose_evidence_refused_dev_test_run_evidence_counts(self):
        self.ready_except_db()
        with self.assertRaises(wf.WorkflowError):
            self.db_ev("compose", COMPOSE_CMD)
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 4)
        code, out = self.db_ev("dev_test_run", 'dev_test_run(profile="example-service", packages=["./..."])')
        self.assertEqual(code, 0, out)
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 0)

    def test_missing_dev_test_run_stays_a_capability_stop(self):
        code, _ = self.w("block-check", reason="capability: dev_test_run required by Dev-test-mode; not listed")
        self.assertEqual(code, 0)


class DefaultCompletionTest(ModeCoordinator, unittest.TestCase):
    MODE_ENV = {}

    def test_default_card_keeps_old_completion_rules(self):
        self.ready_except_db(db=False)
        v = self.view()
        self.assertFalse(v["dev_test_mode"]["db_tests_required"])
        self.assertNotIn(tm.DB_CHECK, self.stage("test", v)["checks"])
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 0)

    def test_environment_set_later_is_refused_not_defaulted(self):
        with mode_env(COMPOSE_ENV):
            self.assertEqual(tk.main_out(["completion", "status", "t_1", "--repo-dir", self.co], self.fake)[0], 3)
        code, _ = self.w("block-check", reason="capability: go test; no dev_test_run tool; missing a profile")
        self.assertEqual(code, 0)  # unchanged default classification without the setting


def write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class NoInvocationTest(ModeCoordinator, unittest.TestCase):
    """A compose card with `dev_test_run` and `docker` executables on PATH: the workflow script
    never runs either (in process: every subprocess argv is recorded; as a real subprocess: the
    stand-ins write a marker file when executed)."""
    MODE_ENV = COMPOSE_ENV

    def test_in_process_flow_never_spawns_dev_test_run_or_docker(self):
        seen = []
        real_run, real_popen = subprocess.run, subprocess.Popen

        def run(argv, *a, **kw):
            seen.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
            return real_run(argv, *a, **kw)

        class Popen(real_popen):
            def __init__(self, argv, *a, **kw):
                seen.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
                super().__init__(argv, *a, **kw)

        with mock.patch.object(subprocess, "run", run), mock.patch.object(subprocess, "Popen", Popen):
            self.ready_except_db()
            self.db_ev("compose", COMPOSE_CMD)
            self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 0)
            tk.main_out(["completion", "capabilities", "--repo-dir", self.co, "--dev-test-mode", "compose"],
                        self.fake)
        self.assertTrue(seen, "the flow ran git subprocesses")
        for argv in seen:
            flat = " ".join(str(a) for a in argv)
            self.assertNotRegex(flat, r"dev[_-]?test[_-]?run", argv)
            self.assertNotIn("docker", Path(str(argv[0])).name, argv)

    def test_subprocess_commands_with_stand_ins_on_path(self):
        bindir = Path(self.tmp, "bin")
        bindir.mkdir()
        marker = Path(self.tmp, "invoked")
        for name in ("dev_test_run", "docker", "docker-compose"):
            write_exe(bindir / name, f"#!/bin/sh\necho {name} \"$@\" >> '{marker}'\nexit 0\n")
        env = clean_env(**COMPOSE_ENV, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
                        HERMES_WORKFLOW_STATE=os.environ["HERMES_WORKFLOW_STATE"])
        for argv in (["intake", "--title", "T", "--repo", tk.REPO_URL, "--lane", "mac", "--key", "kx", "--dry-run"],
                     ["completion", "capabilities", "--repo-dir", self.co, "--dev-test-mode", "compose"]):
            r = subprocess.run([sys.executable, "-I", str(SCRIPT), *argv], env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")


LAUNCHER = r"""
import json, os, subprocess, sys
# Stands in for the Kanban dispatcher: copy this process's environment, add per-task pins,
# optionally drop names (a scrub), and start the worker in its own session.
spec = json.loads(sys.argv[1])
env = dict(os.environ)
env.update({"HERMES_KANBAN_TASK": "t_1", "HERMES_PROFILE": "default"})
for name in spec.get("drop", []):
    env.pop(name, None)
worker = [sys.executable, "-I", "-c", spec["worker"], json.dumps(spec)]
p = subprocess.run(worker, env=env, capture_output=True, text=True, start_new_session=True, timeout=60)
sys.stdout.write(p.stdout); sys.stderr.write(p.stderr); sys.exit(p.returncode)
"""

WORKER = r"""
import json, os, subprocess, sys
# Stands in for the worker's terminal tool: a non-interactive shell inheriting its environment
# runs the workflow command the card prints.
spec = json.loads(sys.argv[1])
p = subprocess.run(["/bin/sh", "-c", 'exec "$0" -I "$@"', sys.executable, *spec["argv"]],
                   capture_output=True, text=True, timeout=60)
sys.stdout.write(p.stdout); sys.stderr.write(p.stderr); sys.exit(p.returncode)
"""


class SubprocessInheritanceTest(tk.StateDirMixin, unittest.TestCase):
    """A real three-process chain (launcher -> worker -> shell -> workflow.py): the settings
    reach the command only by inheritance, and a scrub anywhere in the chain changes the result."""

    def chain(self, env_extra: dict, drop=(), argv=None):
        argv = argv or [str(SCRIPT), "intake", "--title", "T", "--repo", tk.REPO_URL, "--lane", "mac",
                        "--key", "kchain", "--dry-run"]
        spec = {"worker": WORKER, "argv": argv, "drop": list(drop)}
        env = clean_env(**env_extra)
        return subprocess.run([sys.executable, "-I", "-c", LAUNCHER, json.dumps(spec)], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_settings_reach_the_terminal_command(self):
        r = self.chain(COMPOSE_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["dev_test_mode"], {"mode": "compose", "source": "environment"})
        r = self.chain(DTR_ENV)
        self.assertEqual(json.loads(r.stdout)["dev_test_mode"]["mode"], "dev_test_run")

    def test_unset_or_scrubbed_settings_give_the_default(self):
        r = self.chain({})
        self.assertEqual(json.loads(r.stdout)["dev_test_mode"], {"mode": "default", "source": "default"})
        r = self.chain(COMPOSE_ENV, drop=ENV_KEYS)  # a launcher that does not pass them on
        self.assertEqual(json.loads(r.stdout)["dev_test_mode"]["mode"], "default")

    def test_half_scrubbed_or_invalid_settings_fail_closed_in_the_child(self):
        r = self.chain(COMPOSE_ENV, drop=(tm.ENV_BACKEND,))  # false without its backend
        self.assertEqual(r.returncode, 3)
        self.assertIn("needs AOK_TEST_BACKEND=compose", r.stderr)
        self.assertEqual(r.stdout, "")
        r = self.chain({tm.ENV_REQUIRE: "true", tm.ENV_BACKEND: "compose"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("conflicts", r.stderr)

    def test_the_scripts_own_children_inherit_the_settings(self):
        # default_runner (`hermes ...`) is the script's only non-git child: a stand-in records its env.
        tmp = Path(tempfile.mkdtemp(prefix="dev-test-mode-bin-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        out = tmp / "env.json"
        write_exe(tmp / "hermes", f"#!{sys.executable}\nimport json, os\n"
                                  f"json.dump({{k: os.environ.get(k) for k in {list(ENV_KEYS)!r}}}, open({str(out)!r}, 'w'))\n")
        with mode_env({**COMPOSE_ENV, "HERMES_BIN": str(tmp / "hermes")}):
            wf.default_runner(["--version"])
        self.assertEqual(json.loads(out.read_text()), COMPOSE_ENV)


def write_config(home: Path, require=None, backend=None, raw: str = "") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    lines = ["_config_version: 52", "kanban:", "  max_in_progress: 2"]
    if require is not None or backend is not None:
        lines += ["agent_ops_kit:", "  dev_test:"]
        if require is not None:
            lines.append(f"    require_dev_test_run: {require}")
        if backend is not None:
            lines.append(f"    backend: {backend}")
    path = home / "config.yaml"
    path.write_text(raw or "\n".join(lines) + "\n")
    return path


class NativeConfigTest(unittest.TestCase):
    """The non-secret setting lives in Hermes' config.yaml (`hermes config set`); the two
    environment names are aliases; disagreement fails closed."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dev-test-mode-cfg-"))
        self.addCleanup(shutil.rmtree, self.home, True)

    def env(self, **extra):
        return {"HERMES_HOME": str(self.home), **extra}

    def test_config_only_env_only_agreeing_and_conflicting(self):
        write_config(self.home, "false", "compose")
        self.assertEqual(tm.local_setting(self.env()), {"mode": "compose", "source": "config"})
        self.assertEqual(tm.local_setting(self.env(**COMPOSE_ENV)), {"mode": "compose", "source": "config+environment"})
        self.assertEqual(tm.local_setting(self.env(**{tm.ENV_BACKEND: "compose"}))["mode"], "compose")
        for env in (DTR_ENV, {tm.ENV_BACKEND: "dev_test_run"}):
            with self.assertRaises(tm.ModeError, msg=env) as cm:
                tm.local_setting(self.env(**env))
            self.assertIn("make them agree", str(cm.exception))
        write_config(self.home)  # no section: the environment alone decides
        self.assertEqual(tm.local_setting(self.env(**DTR_ENV)), {"mode": "dev_test_run", "source": "environment"})
        self.assertEqual(tm.local_setting(self.env()), {"mode": "", "source": "default"})
        (self.home / "config.yaml").unlink()
        self.assertEqual(tm.local_setting(self.env()), {"mode": "", "source": "default"})

    def test_config_values_fail_closed(self):
        for req, backend, text in (("maybe", None, "is not true or false"), ("true", "compose", "conflicts"),
                                   ("false", None, "needs agent_ops_kit.dev_test.backend=compose"),
                                   (None, "podman", "is not one of")):
            write_config(self.home, req, backend)
            with self.assertRaises(tm.ModeError, msg=(req, backend)) as cm:
                tm.local_setting(self.env())
            self.assertIn(text, str(cm.exception))
        for raw in ("agent_ops_kit: {dev_test: {backend: compose}}\n",
                    "agent_ops_kit:\n  dev_test: {backend: compose}\n",
                    "agent_ops_kit:\n  dev_test:\n    backend: [compose]\n",
                    "agent_ops_kit:\n  dev_test:\n    backend: compose\n    backend: dev_test_run\n",
                    "agent_ops_kit:\n  dev_test:\n    test_backend: compose\n",
                    "agent_ops_kit:\n  dev_test:\n    backend:\n      nested: compose\n"):
            write_config(self.home, raw=raw)
            with self.assertRaises(tm.ModeError, msg=raw):
                tm.local_setting(self.env())

    def test_only_the_exact_two_environment_names(self):
        for name in ("aok_test_backend", "Aok_Require_Dev_Test_Run"):
            with self.assertRaises(tm.ModeError, msg=name):
                tm.from_env({name: "compose"})
        self.assertEqual(tm.from_env({"AOK_DEV_TEST_BACKEND": "compose"}), "")  # unrelated name: not a setting

    def test_worker_and_intake_use_config(self):
        write_config(self.home, "false", "compose")
        self.assertEqual(tm.resolve_intake(None, self.env()), {"mode": "compose", "source": "config"})
        with self.assertRaises(tm.ModeError):
            tm.resolve_intake("dev_test_run", self.env())
        with self.assertRaises(tm.ModeError):
            tm.check_worker("dev_test_run", self.env())
        self.assertEqual(tm.check_worker("compose", self.env())["setting_source"], "config")

    def test_written_by_hermes_config_set(self):
        # The supported writer: the real `hermes config set` in a throwaway HERMES_HOME.
        hermes = shutil.which("hermes")
        if not hermes:
            self.skipTest("no hermes CLI on PATH")
        write_config(self.home)
        env = {**os.environ, "HERMES_HOME": str(self.home)}
        for key, value in (("agent_ops_kit.dev_test.require_dev_test_run", "false"),
                           ("agent_ops_kit.dev_test.backend", "compose")):
            r = subprocess.run([hermes, "config", "set", key, value], env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tm.read_config(self.home / "config.yaml"),
                         {"require_dev_test_run": "false", "backend": "compose"})
        self.assertEqual(tm.local_setting({"HERMES_HOME": str(self.home)})["mode"], "compose")
        r = subprocess.run([hermes, "config", "get", "agent_ops_kit.dev_test.backend"], env=env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "compose")
        self.assertFalse((self.home / ".env").exists() and "AOK_" in (self.home / ".env").read_text())

    def test_config_plan_is_a_proposal(self):
        env = clean_env(HERMES_HOME=str(self.home))
        r = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("dev_test_mode.py")), "config-plan",
                            "--require", "false", "--backend", "compose"], env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual((plan["mode"], plan["executed"]), ("compose", False))
        self.assertEqual(plan["commands"][0], f"cp -p {self.home}/config.yaml {self.home}/config.yaml.bak.dev-test-mode")
        self.assertIn("hermes config set agent_ops_kit.dev_test.require_dev_test_run false", plan["commands"])
        self.assertIn("hermes config set agent_ops_kit.dev_test.backend compose", plan["commands"])
        self.assertFalse((self.home / "config.yaml").exists())  # nothing written
        r = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("dev_test_mode.py")), "config-plan",
                            "--require", "true", "--backend", "compose"], env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 3)

    def test_installer_and_env_example_no_longer_carry_the_setting(self):
        repo = SCRIPT.parents[4]
        for name in (".env.example", "scripts/hermes-install.sh", "scripts/test_hermes_install.sh"):
            self.assertFalse(any(n in (repo / name).read_text() for n in ENV_KEYS), name)
        doc = (repo / "docs/dev-test-mode.md").read_text()
        self.assertNotIn("`make hermes-config-apply` validates the pair", doc)  # the old activation advice
        self.assertIn("drop the `agent_ops_kit` keys", doc)
        self.assertIn("hermes config set agent_ops_kit.dev_test.backend compose", doc)


class SharedProfileConfigTest(unittest.TestCase):
    """A routed profile worker runs with HERMES_HOME=<root>/profiles/<name> and without the launch
    profile's .env names (Hermes' strip_launch_profile_env), so the environment aliases do not reach
    it. It reads the root config.yaml too: one `hermes config set` in the root home reaches the
    coordinator and every profile worker; a profile config that disagrees fails closed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dev-test-mode-root-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.profile = self.root / "profiles" / "coder"
        self.profile.mkdir(parents=True)

    def worker(self, **extra):
        return {"HERMES_HOME": str(self.profile), **extra}

    def test_root_config_reaches_a_profile_worker_without_the_environment(self):
        write_config(self.root, "false", "compose")
        self.assertEqual(tm.config_paths(self.worker()), [self.profile / "config.yaml", self.root / "config.yaml"])
        self.assertEqual(tm.local_setting(self.worker()), {"mode": "compose", "source": "config"})
        self.assertEqual(tm.check_worker("compose", self.worker())["mode"], "compose")
        with self.assertRaises(tm.ModeError):  # a default card on that worker is refused, not defaulted
            tm.check_worker("", self.worker())
        self.assertEqual(tm.resolve_intake(None, self.worker()), {"mode": "compose", "source": "config"})

    def test_profile_config_agreeing_or_conflicting(self):
        write_config(self.root, "false", "compose")
        write_config(self.profile, None, "compose")
        self.assertEqual(tm.local_setting(self.worker())["mode"], "compose")
        write_config(self.profile, "true")
        with self.assertRaises(tm.ModeError) as cm:
            tm.local_setting(self.worker())
        self.assertIn(str(self.root / "config.yaml"), str(cm.exception))
        (self.root / "config.yaml").unlink()  # the profile's own setting alone decides
        self.assertEqual(tm.local_setting(self.worker())["mode"], "dev_test_run")

    def test_root_home_reads_only_itself(self):
        write_config(self.profile, "false", "compose")  # a profile's config never leaks up to the root
        self.assertEqual(tm.config_paths({"HERMES_HOME": str(self.root)}), [self.root / "config.yaml"])
        self.assertEqual(tm.local_setting({"HERMES_HOME": str(self.root)})["mode"], "")

    def test_config_plan_from_a_profile_targets_the_shared_root_config(self):
        plan = tm.config_plan("false", "compose", self.worker())
        self.assertEqual(plan["config"], str(self.root / "config.yaml"))
        self.assertIn(f"env HERMES_HOME={self.root} hermes config set agent_ops_kit.dev_test.backend compose",
                      plan["commands"])
        self.assertFalse((self.root / "config.yaml").exists())  # a proposal: nothing written

    def test_invalid_root_config_fails_closed_in_the_worker(self):
        write_config(self.root, "false")
        with self.assertRaises(tm.ModeError):
            tm.local_setting(self.worker())


class CheckReportTest(unittest.TestCase):
    """`dev_test_mode.py check` reports the effective mode and where it came from: the config files it
    read (only the two agent_ops_kit.dev_test keys) and which of the two alias names are set. It
    never prints another environment variable, nor the aliases' values."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dev-test-mode-check-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.profile = self.root / "profiles" / "coder"
        self.profile.mkdir(parents=True)

    def check(self, env):
        r = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("dev_test_mode.py")), "check"],
                           env=env, capture_output=True, text=True, timeout=30)
        return r

    def test_effective_report_names_only(self):
        write_config(self.root, "false", "compose")
        env = clean_env(HERMES_HOME=str(self.profile), SOME_TOKEN="s3cr3t-value", **{tm.ENV_BACKEND: "compose"})
        r = self.check(env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["mode"], out["source"]), ("compose", "config+environment"))
        self.assertEqual(out["config"], [
            {"path": str(self.profile / "config.yaml"), "exists": False, "settings": {}},
            {"path": str(self.root / "config.yaml"), "exists": True,
             "settings": {"agent_ops_kit.dev_test.require_dev_test_run": "false",
                          "agent_ops_kit.dev_test.backend": "compose"}}])
        self.assertEqual(out["environment_names"], [tm.ENV_BACKEND])
        self.assertEqual(set(out), {"mode", "source", "config", "environment_names"})
        self.assertNotIn("s3cr3t-value", r.stdout + r.stderr)
        self.assertNotIn("SOME_TOKEN", r.stdout + r.stderr)
        self.assertNotIn("_config_version", r.stdout)  # nothing else of config.yaml

    def test_default_and_error(self):
        r = self.check(clean_env(HERMES_HOME=str(self.root)))
        out = json.loads(r.stdout)
        self.assertEqual((out["mode"], out["source"], out["environment_names"]), ("default", "default", []))
        r = self.check(clean_env(HERMES_HOME=str(self.root), **{tm.ENV_REQUIRE: "false"}))
        self.assertEqual((r.returncode, r.stdout), (3, ""))
        self.assertIn("needs AOK_TEST_BACKEND=compose", r.stderr)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_plan(fake, plan: dict) -> None:
    """Stand-in for the operator's `hermes kanban edit <id> --body` of each planned edit (the
    real CLI is exercised by the disposable-board probe, not here)."""
    for e in plan["edits"]:
        assert sha(fake.body(e["card"])) == e["old_sha256"], e["card"]
        fake.shows[e["card"]]["task"]["body"] = e["new_body"]


class BodyPlanTest(tk.StateDirMixin, unittest.TestCase):
    """An existing default-mode native-v1 request moves to an explicit mode only by the operator's
    supported `hermes kanban edit --body` of exactly planned bodies. The plan is read-only; nothing
    on the board (a comment, a record) changes a card's mode."""

    INTAKE = ["intake", "--title", "Build the iPad app", "--repo", tk.REPO_URL, "--lane", "mac", "--lane",
              "windows", "--key", "k", "--body", "full ask"]

    def setUp(self):
        super().setUp()
        env = no_mode_env()
        env.start()
        self.addCleanup(env.stop)
        self.fake = tk.FakeHermes()
        tk.graph(self.fake)  # t_1 code, t_2 mac, t_3 win, t_4 round, t_5 report: the default mode
        self.fake.shows["t_1"]["task"]["status"] = "blocked"
        self.before = {tid: self.fake.body(tid) for tid in self.fake.shows}

    def plan(self, *extra, mode="compose", task="t_1"):
        return tk.main_out(["dev-test-mode", "body-plan", task, "--mode", mode, *extra], self.fake)

    def compose_render(self):
        """The bodies a fresh compose-mode intake of the same request renders."""
        fresh = tk.FakeHermes()
        tk.graph(fresh, "--dev-test-mode", "compose")
        return {tid: fresh.body(tid) for tid in fresh.shows}

    def test_plan_is_read_only_exact_and_deterministic(self):
        calls = len(self.fake.native_calls)
        code, out = self.plan()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.fake.native_calls), calls)
        self.assertEqual(self.fake.cli_writes, [])
        self.assertEqual({t: self.fake.body(t) for t in self.fake.shows}, self.before)
        self.assertEqual((out["executed"], out["state"], out["mode"]), (False, "pending", "compose"))
        want = self.compose_render()
        edits = {e["card"]: e for e in out["edits"]}
        self.assertEqual(set(edits), {"t_1", "t_4"})  # code + round carry the request header; others unchanged
        for tid, e in edits.items():
            self.assertEqual(e["new_body"], want[tid])
            self.assertEqual(e["old_sha256"], sha(self.before[tid]))
            self.assertEqual(e["new_sha256"], sha(want[tid]))
            self.assertEqual(e["request_sha256"], sha(wf.request_of({"task": {"body": self.before[tid]}})))
            self.assertEqual(e["header_added"], {"Dev-test-mode": "compose"})
            self.assertIn(f"hermes kanban edit {tid} --body", e["command"])
        self.assertEqual(out["edits"][0]["card"], "t_1")  # the code card first
        self.assertEqual(self.plan()[1]["plan_sha256"], out["plan_sha256"])
        self.assertFalse({"authorization", "record"} & set(out) or any("authorization" in e for e in out["edits"]))

    def test_apply_then_the_whole_graph_is_rediscovered_once(self):
        shows = set(self.fake.shows)
        code, out = tk.main_out([*self.INTAKE, "--dev-test-mode", "compose", "--bind", "t_1"], self.fake)
        self.assertEqual(code, 4, out)  # before the edit: a mismatch naming the operator's body plan
        self.assertIn("dev-test-mode body-plan", out["blocker"])
        _, plan = self.plan()
        apply_plan(self.fake, plan)
        show = wf.card(self.fake, "t_1")
        self.assertEqual(wf.card_mode_of(show), ("compose", "header"))
        self.assertEqual(wf.request_of(show), wf.request_of({"task": {"body": self.before["t_1"]}}))
        creates = len(self.fake.of("create"))
        for argv, env in (([*self.INTAKE, "--dev-test-mode", "compose", "--bind", "t_1"], {}),
                          ([*self.INTAKE, "--bind", "t_1"], COMPOSE_ENV)):
            for _ in range(2):
                with mode_env(env):
                    code, out = tk.main_out(argv, self.fake)
                self.assertEqual(code, 0, out)
        self.assertEqual(len(self.fake.of("create")), creates)
        self.assertEqual(set(self.fake.shows), shows)
        # Without the mode the rerun is a mismatch: the card's mode never falls back to the default.
        self.assertEqual(tk.main_out([*self.INTAKE, "--bind", "t_1"], self.fake)[0], 4)
        code, again = self.plan()
        self.assertEqual((code, again["state"], again["edits"]), (0, "applied", []))

    def test_self_authorization_comments_change_nothing(self):
        h = wf.header_of(self.fake.shows["t_1"])
        rec = {"event": "migrate", "card": "t_1", "mode": "compose", "from": "default",
               "body_sha256": sha(self.before["t_1"]), "authorization": "Discord message 1"}
        for author in {h["coder"], h["orchestrator"]}:
            self.fake.native("kanban_comment", {"task_id": "t_1", "body": "dev-test-mode " + json.dumps(rec)},
                             author)
            self.fake.native("kanban_comment", {"task_id": "t_1", "body": "Operator authorizes Compose testing"},
                             author)
        self.assertEqual(wf.card_mode_of(wf.card(self.fake, "t_1")), ("", "default"))
        self.assertEqual(self.plan()[1]["state"], "pending")
        with mode_env(COMPOSE_ENV):  # a compose worker on the unchanged default card: refused, not defaulted
            self.assertEqual(tk.main_out([*self.INTAKE, "--bind", "t_1"], self.fake)[0], 4)
            with self.assertRaises(wf.CapabilityError):
                wf.dev_test_mode(wf.card(self.fake, "t_1"))

    def test_drift_and_conflicts_fail_closed(self):
        _, plan = self.plan()
        apply_plan(self.fake, plan)
        self.fake.shows["t_1"]["task"]["status"] = "ready"
        tk.push(self.fake)
        for tid in ("t_2", "t_3"):
            tk.validate(self.fake, tid, tk.evidence("fail", sha=tk.SHA, task=tid))
        round_body = self.fake.body("t_4")
        # The round card lost its mode line (or names another): its code card says compose -> refuse.
        for drift in (round_body.replace("Dev-test-mode: compose\n", ""),
                      round_body.replace("Dev-test-mode: compose", "Dev-test-mode: dev_test_run")):
            self.fake.shows["t_4"]["task"]["body"] = drift
            self.assertEqual(tk.main_out(["advance", "t_4"], self.fake)[0], 3)
            self.assertEqual(self.fake.by_title("[code 2]"), [])
        self.fake.shows["t_4"]["task"]["body"] = round_body
        # A one-byte body edit keeps the header's mode (no silent default) and is a rediscovery mismatch.
        self.fake.shows["t_1"]["task"]["body"] = self.fake.body("t_1") + "\n"
        self.assertEqual(wf.card_mode_of(wf.card(self.fake, "t_1")), ("compose", "header"))
        self.assertEqual(tk.main_out([*self.INTAKE, "--dev-test-mode", "compose", "--bind", "t_1"],
                                     self.fake)[0], 4)

    def test_out_dir_writes_exact_body_files(self):
        d = Path(tempfile.mkdtemp(dir=self._state))
        code, out = self.plan("--out-dir", str(d))
        self.assertEqual(code, 0, out)
        for e in out["edits"]:
            f = d / f"{e['card']}.body"
            self.assertEqual(f.read_bytes(), e["new_body"].encode("utf-8"))
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
            self.assertIn(str(f), e["command"])
            # The command keeps the body's trailing newline ($(...) alone would strip it).
            edit = f'hermes kanban edit {e["card"]} --body "$b"'
            self.assertTrue(e["command"].endswith(edit), e["command"])
            echo = d / "echo"
            r = subprocess.run(["sh", "-c", e["command"][:-len(edit)] + f'printf %s "$b" > {echo}'],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(echo.read_bytes(), f.read_bytes())
        self.assertEqual(self.plan("--out-dir", str(d))[0], 0)  # identical rerun
        (d / "t_1.body").write_text("other")
        self.assertEqual(self.plan("--out-dir", str(d))[0], 2)

    def test_refusals(self):
        self.fake.shows["t_1"]["task"]["status"] = "running"
        self.assertEqual(self.plan()[0], 2)
        self.fake.shows["t_1"]["task"]["status"] = "blocked"
        self.assertEqual(self.plan(task="t_4")[0], 2)  # not a code card
        with mode_env(DTR_ENV):  # this process's setting names another mode
            self.assertEqual(self.plan()[0], 3)
        self.fake.shows["t_4"]["task"]["status"] = "running"  # a card to edit is being worked
        self.assertEqual(self.plan()[0], 2)
        self.fake.shows["t_4"]["task"]["status"] = "todo"
        self.fake.shows["t_4"]["task"]["body"] = self.before["t_4"].replace("full ask", "other ask")
        self.assertEqual(self.plan()[0], 2)  # its request is not the code card's
        self.fake.shows["t_4"]["task"]["body"] = self.before["t_4"]
        self.fake.shows["t_1"]["task"]["body"] = self.before["t_1"].replace("Board-transport: native-v1\n", "")
        self.assertEqual(self.plan()[0], 2)  # not native-v1
        fake = tk.FakeHermes()
        tk.graph(fake, "--dev-test-mode", "dev_test_run", key="k9")
        fake.shows["t_1"]["task"]["status"] = "blocked"
        self.assertEqual(tk.main_out(["dev-test-mode", "body-plan", "t_1", "--mode", "compose"], fake)[0], 2)
        self.assertEqual(self.fake.cli_writes, [])


CANONICAL = "/home/hermes-dev/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py"


class BodyPlanScriptPathTest(tk.StateDirMixin, unittest.TestCase):
    """A body plan never repoints a live card's embedded `python3 <workflow.py>` commands: run from
    any other copy (a scratch clone that will be pruned) it refuses before writing a plan file. There
    is no path override; the plan runs from the exact script the cards already name."""

    def setUp(self):
        super().setUp()
        env = no_mode_env()
        env.start()
        self.addCleanup(env.stop)
        self.fake = tk.FakeHermes()
        tk.graph(self.fake)
        self.fake.shows["t_1"]["task"]["status"] = "blocked"

    def plan(self, *extra):
        return tk.main_out(["dev-test-mode", "body-plan", "t_1", "--mode", "compose", *extra], self.fake)

    def as_live(self, path=CANONICAL):
        """A copy of a real default-mode graph whose cards were rendered by the installed skill."""
        for show in self.fake.shows.values():
            show["task"]["body"] = show["task"]["body"].replace(str(wf.SCRIPT), path)
        return {tid: self.fake.body(tid) for tid in self.fake.shows}

    def test_exact_path_is_preserved(self):
        code, out = self.plan()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["edits"])
        for e in out["edits"]:
            self.assertEqual(wf.embedded_scripts(e["new_body"]), {str(wf.SCRIPT)})
            self.assertEqual(wf.embedded_scripts(self.fake.body(e["card"])), {str(wf.SCRIPT)})
            self.assertFalse(any("workflow.py" in ln for ln in e["removed_lines"]), e["removed_lines"])

    def test_other_copy_refuses_before_writing_plan_files(self):
        before = self.as_live()
        self.assertNotEqual(str(wf.SCRIPT), CANONICAL)
        d = Path(tempfile.mkdtemp(dir=self._state))
        for extra in ((), ("--out-dir", str(d))):
            self.assertEqual(self.plan(*extra)[0], 2)
        with self.assertRaisesRegex(wf.WorkflowError, "another workflow script path.*" + re.escape(CANONICAL)):
            wf.body_plan(self.fake, "t_1", "compose", str(d))
        self.assertEqual(list(d.iterdir()), [])
        self.assertEqual({t: self.fake.body(t) for t in self.fake.shows}, before)
        self.assertEqual(self.fake.cli_writes, [])
        with self.assertRaises(SystemExit):  # no override: the path is never guessed or rewritten
            wf.parser().parse_args(["dev-test-mode", "body-plan", "t_1", "--mode", "compose",
                                    "--script-path", CANONICAL])

    def test_one_mismatched_card_refuses_the_whole_plan(self):
        self.fake.shows["t_4"]["task"]["body"] = self.fake.body("t_4").replace(str(wf.SCRIPT), CANONICAL)
        d = Path(tempfile.mkdtemp(dir=self._state))
        self.assertEqual(self.plan("--out-dir", str(d))[0], 2)
        with self.assertRaisesRegex(wf.WorkflowError, "t_4: " + re.escape(CANONICAL)):
            wf.body_plan(self.fake, "t_1", "compose", str(d))
        self.assertEqual(list(d.iterdir()), [])

    def test_new_normal_cards_are_unaffected(self):
        self.as_live()  # existing live cards elsewhere on the board do not constrain a fresh intake
        fresh = tk.FakeHermes()
        tk.graph(fresh, key="k2")
        tk.graph(fresh, "--dev-test-mode", "compose", key="k3")
        for tid in fresh.shows:
            self.assertEqual(wf.embedded_scripts(fresh.body(tid)) - {str(wf.SCRIPT)}, set())
        self.assertTrue(any(wf.embedded_scripts(fresh.body(t)) for t in fresh.shows))


class BodyPlannedCompletionTest(ModeCoordinator, unittest.TestCase):
    """A default-mode --checkout code card after the operator applied the compose body plan:
    completion, preflight and block-check follow its header."""
    MODE_ENV = {}

    def setUp(self):
        super().setUp()
        status = self.fake.shows["t_1"]["task"]["status"]
        self.fake.shows["t_1"]["task"]["status"] = "blocked"
        self.fake.shows["t_1"]["runs"][-1]["status"] = "ended"
        code, plan = tk.main_out(["dev-test-mode", "body-plan", "t_1", "--mode", "compose"], self.fake)
        self.assertEqual(code, 0, plan)
        apply_plan(self.fake, plan)
        self.fake.shows["t_1"]["task"]["status"] = status
        self.fake.shows["t_1"]["runs"][-1]["status"] = "running"

    def test_updated_card_follows_the_compose_rules(self):
        code, pre = nw.checkout_command("preflight", "t_1", self.co, run=self.fake, scan_runner=tc.SCAN)
        self.assertEqual(code, 0, pre)
        self.assertIn("never call dev_test_run", self.fake.body("t_1"))
        self.ready_except_db()
        self.assertTrue(self.view()["dev_test_mode"]["db_tests_required"])
        with self.assertRaises(wf.WorkflowError):
            self.db_ev("dev_test_run", "dev_test_run(profile=x)")
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 4)
        code, out = self.w("block-check", reason="capability: go test; no dev_test_run tool; missing a profile")
        self.assertEqual(code, 2)
        self.assertEqual(self.db_ev("compose", "go test -p 1 ./...")[0], 0)
        self.assertEqual(nw.completion_handoff("t_1", self.co, self.fake)[0], 0)
        with mode_env(DTR_ENV):
            self.assertEqual(tk.main_out(["completion", "status", "t_1", "--repo-dir", self.co], self.fake)[0], 3)


if __name__ == "__main__":
    unittest.main()
