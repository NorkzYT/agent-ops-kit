"""Offline tests for scripts/knowledge-learn.py.

A temp HERMES_HOME with installed profiles and synced raw knowledge, a stub
``hermes`` (learn / fail / sleep / noop / tamper modes) and a stub
``agent.learn_prompt``. Nothing touches the real profiles, /mnt/books, .env or
a model.

Proves: --status separates raw synced knowledge from the learned
``<profile>-knowledge`` skill and writes nothing; --dry-run runs and writes
nothing; a run invokes ``hermes -p <p> chat --query-file ... --oneshot`` once
per file with the official /learn prompt, explicit turn/budget limits and no
terminal toolset; per-unit logs and state; a second run is a no-op; a changed
file is re-learned alone; only the exact skill name counts; failures retry and
then block; a hung run is killed at the timeout and stays pending; a source
changed during a run stops the profile; SIGTERM kills Hermes and records no
attempt; a held lock skips the profile; a broken prompt builder never runs
Hermes; books are never modified.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "knowledge-learn.py"


def load_module():
    spec = importlib.util.spec_from_file_location("knowledge_learn_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kl = load_module()

STUB_HERMES = r'''#!/usr/bin/env python3
import json, os, re, sys, time
a = sys.argv[1:]
prof = a[a.index("-p") + 1]
prompt = open(a[a.index("--query-file") + 1], encoding="utf-8").read()
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": a, "prompt": prompt}) + "\n")
home = os.path.join(os.environ["HERMES_HOME"], "profiles", prof)
mode = os.environ.get("STUB_MODE", "learn")
src = re.search(r"from ONE source file:\n\n(.+)\n", prompt).group(1)
slug = re.search(r"`references/([a-z0-9-]+)-<chapter", prompt).group(1)
if mode == "fail":
    print("provider error"); sys.exit(3)
if mode in ("sleep", "partial"):
    if mode == "partial":                       # saves one more chapter, then hangs
        refs = os.path.join(home, "skills", "knowledge", prof + "-knowledge", "references")
        os.makedirs(refs, exist_ok=True)
        n = len(os.listdir(refs)) + 1
        open(os.path.join(os.path.dirname(refs), "SKILL.md"), "a").write("- %s\n" % slug)
        open(os.path.join(refs, "%s-ch%02d.md" % (slug, n)), "w").write("notes\n")
    open(os.environ["STUB_PIDFILE"], "w").write(str(os.getpid()))
    time.sleep(120)
if mode == "noop":
    print("did nothing"); sys.exit(0)
if mode == "tamper":
    open(src, "a").write("tampered")
skill = os.path.join(home, "skills", "knowledge", prof + "-knowledge")
os.makedirs(os.path.join(skill, "references"), exist_ok=True)
md = os.path.join(skill, "SKILL.md")
body = open(md).read() if os.path.exists(md) else "---\nname: %s-knowledge\n---\n# KB\n## Sources\n" % prof
open(md, "w").write(body + "- %s\n" % slug)
open(os.path.join(skill, "references", slug + "-ch01.md"), "w").write("notes\n")
print("saved skill " + prof + "-knowledge")
'''

STUB_LEARN_PROMPT = '''
def build_learn_prompt(user_request):
    return "[/learn] stub official prompt\\n\\nTHE REQUEST:\\n" + user_request + "\\nEND\\n"
'''


def tree_hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class KnowledgeLearnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kl-test-"))
        self.home = self.tmp / "hermes"
        self.profiles = self.home / "profiles"
        # alpha: two books (one in a folder with spaces); beta: empty knowledge
        k = self.profiles / "alpha" / "knowledge"
        (k / "Great Book, Vol 1").mkdir(parents=True)
        (k / "Great Book, Vol 1" / "Great Book, Vol 1 -- Author.md").write_text("# Ch1\nalpha one\n")
        (k / "second.txt").write_text("second book $(rm -rf /) `x`\n")
        (self.profiles / "beta" / "knowledge").mkdir(parents=True)
        # stub hermes + stub learn prompt
        bindir = self.tmp / "bin"
        bindir.mkdir()
        self.hermes = bindir / "hermes"
        self.hermes.write_text(STUB_HERMES)
        self.hermes.chmod(0o755)
        agent = self.tmp / "hermes-agent" / "agent"
        agent.mkdir(parents=True)
        (agent / "__init__.py").write_text("")
        (agent / "learn_prompt.py").write_text(STUB_LEARN_PROMPT)
        self.calls = self.tmp / "calls.jsonl"
        self.env_patch = {
            "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.hermes),
            "HERMES_AGENT_DIR": str(self.tmp / "hermes-agent"), "HERMES_PYTHON": sys.executable,
            "STUB_CALLS": str(self.calls), "STUB_PIDFILE": str(self.tmp / "pid"),
        }
        self.saved_env = {k: os.environ.get(k) for k in list(self.env_patch) + ["STUB_MODE"]}
        os.environ.update(self.env_patch)
        os.environ.pop("STUB_MODE", None)
        self.books_before = tree_hashes(k)

    def tearDown(self):
        for key, val in self.saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers --------------------------------------------------------------
    def args(self, **kw):
        base = dict(dry_run=False, timeout=60, max_turns=7, max_attempts=2, limit=0, jobs=1,
                    force=False, retry=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def learn(self, name="alpha", **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = kl.learn_profile(name, self.args(**kw))
        return res, buf.getvalue()

    def call_list(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def state(self, name="alpha"):
        return json.loads((self.profiles / name / "knowledge-learn" / "state.json").read_text())

    def assert_books_untouched(self):
        self.assertEqual(tree_hashes(self.profiles / "alpha" / "knowledge"), self.books_before)

    # tests ----------------------------------------------------------------
    def test_status_separates_raw_from_learned_and_writes_nothing(self):
        r = kl.readiness("alpha")
        self.assertEqual((r["status"], r["raw_files"], r["learned"], r["pending"]), ("needs-learn", 2, 0, 2))
        self.assertEqual(r["skill"], "alpha-knowledge")
        self.assertEqual(kl.readiness("beta")["status"], "no-knowledge")
        self.assertEqual(kl.readiness("ghost")["status"], "not-installed")
        self.assertFalse((self.profiles / "alpha" / "knowledge-learn").exists())
        out = subprocess.run([sys.executable, str(SCRIPT), "--status", "--json", "alpha", "ghost"],
                             capture_output=True, text=True, check=True)
        rows = json.loads(out.stdout)
        self.assertEqual([x["status"] for x in rows], ["needs-learn", "not-installed"])
        self.assertFalse((self.profiles / "alpha" / "knowledge-learn").exists())

    def test_only_exact_skill_name_counts(self):
        other = self.profiles / "alpha" / "skills" / "knowledge" / "alpha-books"
        (other / "references").mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: alpha-books\ndescription: knowledge-base\n---\n")
        (other / "references" / "x.md").write_text("x")
        hidden = self.profiles / "alpha" / "skills" / ".backup" / "alpha-knowledge"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text("---\nname: alpha-knowledge\n---\n")
        self.assertEqual(kl.readiness("alpha")["status"], "needs-learn")

    def test_dry_run_writes_and_runs_nothing(self):
        res, out = self.learn(dry_run=True)
        self.assertEqual(res["outcome"], "dry-run")
        self.assertEqual(self.call_list(), [])
        self.assertFalse((self.profiles / "alpha" / "knowledge-learn").exists())
        self.assertIn("--query-file", out)
        self.assertIn("would learn second.txt", out)
        self.assertIn("/learn prompt builds", out)

    def test_learns_each_file_with_official_prompt_then_is_idempotent(self):
        res, out = self.learn()
        self.assertEqual((res["outcome"], res["learned"], res["failed"]), ("ok", 2, 0))
        calls = self.call_list()
        self.assertEqual(len(calls), 2)
        for c in calls:
            argv = c["argv"]
            self.assertEqual(argv[:3], ["-p", "alpha", "chat"])
            for flag in ("--oneshot", "-Q", "--query-file"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
            self.assertEqual(argv[argv.index("--run-budget") + 1], "45")
            self.assertEqual(argv[argv.index("-t") + 1], "file,skills,todo")
            self.assertNotIn("terminal", argv[argv.index("-t") + 1])
            self.assertTrue(c["prompt"].startswith("[/learn] stub official prompt"))
            self.assertIn("The skill name is exactly `alpha-knowledge`, category `knowledge`", c["prompt"])
        self.assertNotIn("second book", "".join(c["prompt"] for c in calls))   # paths only, never book text
        st = self.state()
        self.assertEqual({u["status"] for u in st["units"].values()}, {"learned"})
        logs = list((self.profiles / "alpha" / "knowledge-learn" / "logs").glob("*.log"))
        self.assertEqual(len(logs), 2)
        self.assertTrue(all("# result learned rc=0" in p.read_text() for p in logs))
        self.assertEqual(len((self.profiles / "alpha" / "knowledge-learn" / "runs.jsonl").read_text().splitlines()), 2)
        r = kl.readiness("alpha")
        self.assertEqual((r["status"], r["learned"], r["skill_path"]), ("ready", 2, "skills/knowledge/alpha-knowledge"))

        res, out = self.learn()
        self.assertEqual(res["learned"], 0)
        self.assertEqual(len(self.call_list()), 2, "second run must not call hermes")
        self.assertIn("nothing to learn", out)
        self.assert_books_untouched()

    def test_changed_file_is_relearned_alone(self):
        self.learn()
        (self.profiles / "alpha" / "knowledge" / "second.txt").write_text("second edition\n")
        self.assertEqual(kl.readiness("alpha")["status"], "partial")
        res, _ = self.learn()
        self.assertEqual(res["learned"], 1)
        calls = self.call_list()
        self.assertEqual(len(calls), 3)
        self.assertIn("second.txt", calls[-1]["prompt"])
        self.assertEqual(kl.readiness("alpha")["status"], "ready")

    def test_limit_and_resume(self):
        res, _ = self.learn(limit=1)
        self.assertEqual(res["learned"], 1)
        r = kl.readiness("alpha")
        self.assertEqual((r["status"], r["learned"], r["pending"]), ("partial", 1, 1))
        res, _ = self.learn()
        self.assertEqual(res["learned"], 1)
        self.assertEqual(kl.readiness("alpha")["status"], "ready")

    def test_failures_retry_then_block(self):
        os.environ["STUB_MODE"] = "fail"
        res, _ = self.learn(limit=1)
        self.assertEqual((res["outcome"], res["failed"]), ("incomplete", 1))
        unit = next(iter(self.state()["units"].values()))
        self.assertEqual((unit["status"], unit["rc"], unit["attempts"]), ("failed", 3, 1))
        self.learn(limit=1)                                    # attempt 2 of 2 -> blocked
        r = kl.readiness("alpha")
        self.assertEqual((r["blocked"], r["status"]), (1, "needs-learn"))
        n = len(self.call_list())
        os.environ["STUB_MODE"] = "learn"
        res, out = self.learn()
        self.assertIn("skip blocked", out)
        self.assertEqual(len(self.call_list()), n + 1)          # only the other file ran
        res, _ = self.learn(retry=True)
        self.assertEqual(res["learned"], 1)
        self.assertEqual(kl.readiness("alpha")["status"], "ready")

    def test_noop_run_is_unverified_not_learned(self):
        os.environ["STUB_MODE"] = "noop"
        res, _ = self.learn(limit=1)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(next(iter(self.state()["units"].values()))["status"], "no-skill")
        os.environ["STUB_MODE"] = "learn"
        self.learn(limit=1)
        os.environ["STUB_MODE"] = "noop"
        res, _ = self.learn(limit=1)
        statuses = sorted(u["status"] for u in self.state()["units"].values())
        self.assertEqual(statuses, ["learned", "unverified"])

    def test_timeout_kills_hermes_and_keeps_unit_pending(self):
        os.environ["STUB_MODE"] = "sleep"
        start = time.time()
        res, _ = self.learn(limit=1, timeout=2)
        self.assertLess(time.time() - start, 30)
        self.assertEqual(res["failed"], 1)
        unit = next(iter(self.state()["units"].values()))
        self.assertEqual(unit["status"], "timeout")
        pid = int((self.tmp / "pid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(kl.readiness("alpha")["pending"], 2)

    def test_timeout_with_progress_resumes_without_blocking(self):
        os.environ["STUB_MODE"] = "partial"
        for n in (1, 2, 3):                               # more than max_attempts=2
            res, _ = self.learn(limit=1, timeout=2)
            unit = self.state()["units"]["second.txt"]
            self.assertEqual((unit["status"], unit["refs"], unit["attempts"]), ("timeout", n, 0))
        self.assertEqual(kl.readiness("alpha")["blocked"], 0)
        os.environ["STUB_MODE"] = "learn"
        self.learn(limit=1)
        self.assertEqual(self.state()["units"]["second.txt"]["status"], "learned")

    def test_sigterm_kills_hermes_and_records_nothing(self):
        env = dict(os.environ, STUB_MODE="sleep")
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "alpha", "--timeout", "60"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pidfile = self.tmp / "pid"
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.1)
        proc.terminate()
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertIn("stay pending", err)
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        self.assertFalse((self.profiles / "alpha" / "knowledge-learn" / "state.json").exists())
        self.assertEqual(kl.readiness("alpha")["pending"], 2)
        self.assertEqual(len(self.call_list()), 1)

    def test_source_modified_during_run_stops_profile(self):
        os.environ["STUB_MODE"] = "tamper"
        res, out = self.learn()
        self.assertEqual(res["outcome"], "source-modified")
        self.assertEqual(len(self.call_list()), 1)
        self.assertIn("stopping this profile", out)

    def test_held_lock_skips_profile(self):
        d = self.profiles / "alpha" / "knowledge-learn"
        d.mkdir()
        with (d / ".lock").open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self.assertTrue(kl.readiness("alpha")["running"])
            res, _ = self.learn()
        self.assertEqual(res["outcome"], "locked")
        self.assertEqual(self.call_list(), [])

    def test_broken_prompt_builder_never_runs_hermes(self):
        os.environ["HERMES_AGENT_DIR"] = str(self.tmp / "missing")
        for dry in (False, True):
            res, _ = self.learn(dry_run=dry)
            self.assertEqual(res["outcome"], "prompt-error")
        self.assertEqual(self.call_list(), [])

    def test_unique_slugs(self):
        slugs = kl.unit_slugs(["a/Same Name.md", "b/Same Name.md", "c/other.txt"])
        self.assertEqual(len(set(slugs.values())), 3)
        self.assertEqual(slugs["c/other.txt"], "other")

    def test_cli_requires_profile_and_validates(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, str(SCRIPT), "../etc"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, str(SCRIPT), "alpha", "--timeout", "5"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.call_list(), [])

    def test_cli_end_to_end_all(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "alpha", "beta", "--timeout", "60", "--jobs", "2"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("alpha", r.stdout)
        self.assertRegex(r.stdout, r"alpha\s+ready")
        self.assertRegex(r.stdout, r"beta\s+no-knowledge")
        self.assert_books_untouched()

    def test_all_skips_hermes_internal_profiles_but_explicit_name_still_works(self):
        # A stray ``skills`` profile (Hermes' own skills dir name) with raw files
        # is not a book-backed domain profile: --all / NAME=all must skip it.
        (self.profiles / "skills" / "knowledge").mkdir(parents=True)
        (self.profiles / "skills" / "knowledge" / "ref.md").write_text("# ref\n")
        (self.profiles / "knowledge" / "knowledge").mkdir(parents=True)
        (self.profiles / "knowledge" / "knowledge" / "ref.md").write_text("# ref\n")
        names = kl.book_backed_profiles()
        self.assertIn("alpha", names)
        self.assertNotIn("skills", names)
        self.assertNotIn("knowledge", names)
        for argv in (["--status", "--json", "--all"], ["--status", "--json", "all"], ["--status", "--json"]):
            out = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, check=True)
            got = [x["profile"] for x in json.loads(out.stdout)]
            self.assertIn("alpha", got)
            self.assertNotIn("skills", got)
        r = subprocess.run([sys.executable, str(SCRIPT), "all", "--dry-run"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotRegex(r.stdout, r"(?m)^\s*skills\b")
        # Named explicitly, the profile is handled like any other.
        self.assertEqual(kl.readiness("skills")["status"], "needs-learn")
        res, _ = self.learn("skills")
        self.assertEqual(res["learned"], 1)
        self.assertEqual([c["argv"][c["argv"].index("-p") + 1] for c in self.call_list()], ["skills"])


if __name__ == "__main__":
    unittest.main()
