"""scripts/apply-dev-workflow.py: narrow skill + coder SOUL rollout into a temp Hermes home.

Every test builds its own throwaway Hermes home; nothing touches ~/.hermes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "apply-dev-workflow.py"
SKILL_SRC = REPO / "hermes" / "skills" / "kanban-workflow"
CODER_SOUL = REPO / "hermes" / "profiles" / "coder" / "SOUL.md"
SKILL_REL = Path("skills/agent-ops-kit/kanban-workflow")

spec = importlib.util.spec_from_file_location("apply_dev_workflow", SCRIPT)
adw = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adw
spec.loader.exec_module(adw)


def source_files() -> list[Path]:
    return [Path(rel) for rel in adw.SKILL_FILES]


def snapshot(root: Path) -> dict[str, tuple]:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            st = os.lstat(p)
            data = p.read_bytes() if p.is_file() and not p.is_symlink() else None
            snap[str(p.relative_to(root))] = (st.st_mode, st.st_mtime_ns, data)
    return snap


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "hermes"
        self.home.mkdir(mode=0o700)
        # Things the helper must never read or write.
        (self.home / "config.yaml").write_text("model: keep\n")
        (self.home / ".env").write_text("SECRET=keep\n")
        (self.home / "SOUL.md").write_text("default soul\n")
        (self.home / "honcho.json").write_text("{}\n")
        (self.home / "plugins" / "model-router").mkdir(parents=True)
        (self.home / "plugins" / "model-router" / "__init__.py").write_text("# keep\n")
        (self.home / "memories").mkdir()
        (self.home / "memories" / "MEMORY.md").write_text("keep\n")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        env["HOME"] = self.tmp.name
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--hermes-home", str(self.home), *args],
            capture_output=True, text=True, env=env, check=False,
        )

    def skill(self) -> Path:
        return self.home / SKILL_REL


class PlanTests(Base):
    def test_plan_is_the_default_and_writes_nothing(self):
        before = snapshot(self.home)
        for args in ((), ("--plan",)):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("nothing written", r.stdout)
            for rel in source_files():
                self.assertIn(f"create {self.skill() / rel} sha256={sha(SKILL_SRC / rel)}", r.stdout)
        self.assertEqual(snapshot(self.home), before)

    def test_plan_reports_paths_hashes_and_status_only(self):
        r = self.run_cli()
        for line in r.stdout.splitlines()[:-1]:
            self.assertRegex(line, r"^(create|update|unchanged|skip) /")
        self.assertNotIn("SECRET", r.stdout + r.stderr)
        self.assertNotIn("restart", (r.stdout + r.stderr).lower())


class ApplyTests(Base):
    def test_apply_copies_exact_bytes_without_bytecode_and_is_idempotent(self):
        r = self.run_cli("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in source_files():
            dst = self.skill() / rel
            self.assertEqual(dst.read_bytes(), (SKILL_SRC / rel).read_bytes(), rel)
            self.assertEqual(os.access(dst, os.X_OK), os.access(SKILL_SRC / rel, os.X_OK))
        self.assertFalse(list(self.skill().rglob("__pycache__")))
        self.assertFalse(list(self.skill().rglob("*.pyc")))
        self.assertFalse(list(self.skill().rglob("*.bak-*")))
        self.assertFalse(list(self.skill().rglob(".*.tmp-*")))

        before = snapshot(self.home)
        again = self.run_cli("--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(all(l.startswith(("unchanged", "skip")) for l in again.stdout.splitlines()))
        self.assertEqual(snapshot(self.home), before)

    def test_update_backs_up_old_content_and_preserves_unknown_files(self):
        self.run_cli("--apply")
        target = self.skill() / "scripts" / "workflow.py"
        target.write_text("old workflow\n")
        extra = self.skill() / "scripts" / "local_extra.py"
        extra.write_text("operator file\n")
        stale = self.skill() / "NOTES.md"
        stale.write_text("not in the source list\n")

        r = self.run_cli("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(target.read_bytes(), (SKILL_SRC / "scripts" / "workflow.py").read_bytes())
        backups = list(target.parent.glob("workflow.py.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old workflow\n")
        self.assertIn(f"backup={backups[0]}", r.stdout)
        old_sha = hashlib.sha256(b"old workflow\n").hexdigest()
        self.assertIn(f"was={old_sha}", r.stdout)
        self.assertEqual(extra.read_text(), "operator file\n")
        self.assertEqual(stale.read_text(), "not in the source list\n")

    def test_scope_never_touches_config_env_honcho_plugins_memories_or_default_soul(self):
        keep = ("config.yaml", ".env", "SOUL.md", "honcho.json", "plugins", "memories")
        before = {k: v for k, v in snapshot(self.home).items() if k.split("/")[0] in keep}
        (self.home / "profiles" / "coder").mkdir(parents=True)
        r = self.run_cli("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = {k: v for k, v in snapshot(self.home).items() if k.split("/")[0] in keep}
        self.assertEqual(after, before)
        top = sorted(p.name for p in self.home.iterdir())
        self.assertEqual(top, sorted(keep + ("profiles", "skills")))
        self.assertEqual(sorted(p.name for p in (self.home / "skills").iterdir()), ["agent-ops-kit"])
        self.assertEqual(sorted(p.name for p in (self.home / "skills" / "agent-ops-kit").iterdir()),
                         ["kanban-workflow"])
        self.assertEqual(sorted(p.name for p in (self.home / "profiles" / "coder").iterdir()), ["SOUL.md"])

    def test_coder_soul_only_when_the_profile_exists_or_is_named(self):
        r = self.run_cli("--apply")
        self.assertIn("skip", r.stdout)
        self.assertFalse((self.home / "profiles").exists())

        (self.home / "profiles" / "coder").mkdir(parents=True)
        r = self.run_cli("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.home / "profiles/coder/SOUL.md").read_bytes(), CODER_SOUL.read_bytes())

        other = self.home / "profiles" / "worker-coder"
        other.mkdir()
        (other / "SOUL.md").write_text("old\n")
        r = self.run_cli("--apply", "--coder-home", str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((other / "SOUL.md").read_bytes(), CODER_SOUL.read_bytes())
        self.assertEqual([p.read_text() for p in other.glob("SOUL.md.bak-*")], ["old\n"])

    def test_coder_home_must_exist_and_sit_below_the_hermes_home(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        for path in (outside, self.home / "profiles" / "absent", self.home):
            before = snapshot(self.home)
            r = self.run_cli("--apply", "--coder-home", str(path))
            self.assertEqual(r.returncode, 1, path)
            self.assertIn("refused", r.stderr)
            self.assertEqual(snapshot(self.home), before)
        self.assertEqual(list(outside.iterdir()), [])


class RefusalTests(Base):
    def assert_refused_without_writes(self, *args: str) -> str:
        before = snapshot(self.home)
        r = self.run_cli("--apply", *args)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("refused", r.stderr)
        self.assertEqual(snapshot(self.home), before)
        return r.stderr

    def test_symlinked_directory_is_refused(self):
        real = Path(self.tmp.name) / "real-skills"
        real.mkdir()
        (self.home / "skills").mkdir()
        os.symlink(real, self.home / "skills" / "agent-ops-kit")
        self.assert_refused_without_writes()
        self.assertEqual(list(real.iterdir()), [])
        self.assertNotEqual(self.run_cli().returncode, 0)  # plan refuses too

    def test_symlinked_hermes_root_is_refused(self):
        link = Path(self.tmp.name) / "hermes-link"
        os.symlink(self.home, link)
        r = subprocess.run([sys.executable, str(SCRIPT), "--apply", "--hermes-home", str(link)],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.home / "skills").exists())

    def test_symlinked_file_is_refused_before_any_write(self):
        self.run_cli("--apply")
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("do not touch\n")
        (self.skill() / "SKILL.md").write_text("stale\n")
        target = self.skill() / "scripts" / "workflow.py"
        target.unlink()
        os.symlink(victim, target)
        self.assert_refused_without_writes()
        self.assertEqual(victim.read_text(), "do not touch\n")
        self.assertEqual((self.skill() / "SKILL.md").read_text(), "stale\n")

    def test_special_file_is_refused(self):
        self.run_cli("--apply")
        target = self.skill() / "SKILL.md"
        target.unlink()
        os.mkfifo(target)
        self.assert_refused_without_writes()

    def test_world_writable_directory_or_file_is_refused(self):
        self.run_cli("--apply")
        for path in (self.home, self.home / "skills", self.skill() / "scripts",
                     self.skill() / "SKILL.md"):
            mode = os.stat(path).st_mode & 0o777
            os.chmod(path, mode | 0o002)
            try:
                (self.skill() / "scripts" / "workflow.py").write_text("stale\n")
                self.assertIn("world-writable", self.assert_refused_without_writes())
            finally:
                os.chmod(path, mode)

    def test_wrong_owner_is_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(adw.os, "getuid", return_value=os.getuid() + 1), \
                redirect_stdout(out), redirect_stderr(err):
            rc = adw.main(["--apply", "--hermes-home", str(self.home)])
        self.assertEqual(rc, 1)
        self.assertIn("owned by uid", err.getvalue())
        self.assertFalse((self.home / "skills").exists())


class SourceListTests(Base):
    def fixture_repo(self) -> Path:
        repo = Path(self.tmp.name) / "repo"
        root = repo / adw.SKILL_SRC
        for rel in adw.SKILL_FILES:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(f"tracked {rel}\n")
        # Untracked stray content next to the tracked files: never deployed.
        (root / "scripts" / "stray.py").write_text("import os; os.remove('x')\n")
        (root / "notes").mkdir()
        (root / "notes" / "draft.md").write_text("draft\n")
        (root / "scripts" / "__pycache__").mkdir()
        (root / "scripts" / "__pycache__" / "workflow.cpython-312.pyc").write_bytes(b"\0")
        return repo

    def test_allowlist_is_exactly_the_tracked_skill_files(self):
        tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", str(SKILL_SRC)], capture_output=True,
                                 text=True, check=True).stdout.split()
        self.assertEqual(sorted(str(Path(p).relative_to(SKILL_SRC.relative_to(REPO))) for p in tracked),
                         sorted(adw.SKILL_FILES))

    def test_stray_untracked_source_files_are_never_copied(self):
        repo = self.fixture_repo()
        out = io.StringIO()
        with mock.patch.object(adw, "REPO", repo), redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(adw.main(["--apply", "--hermes-home", str(self.home)]), 0)
        deployed = sorted(str(p.relative_to(self.skill())) for p in self.skill().rglob("*") if p.is_file())
        self.assertEqual(deployed, sorted(adw.SKILL_FILES))
        self.assertNotIn("stray", out.getvalue())
        self.assertNotIn("draft", out.getvalue())

    def test_missing_allowlisted_file_refuses_before_any_write(self):
        repo = self.fixture_repo()
        (repo / adw.SKILL_SRC / "scripts" / "repo_lease.py").unlink()
        with mock.patch.object(adw, "REPO", repo), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(adw.main(["--apply", "--hermes-home", str(self.home)]), 1)
        self.assertFalse((self.home / "skills").exists())

    def test_injected_source_list_is_validated(self):
        repo = self.fixture_repo()
        self.assertEqual([i.parts for i in adw.source_items(repo, ("notes/draft.md",))],
                         [adw.SKILL_DEST + ("notes", "draft.md")])
        for bad in ("../../etc/passwd", "/etc/passwd", ""):
            with self.assertRaises(adw.Refused, msg=bad):
                adw.source_items(repo, (bad,))
        real = Path(self.tmp.name) / "elsewhere"
        real.mkdir()
        (real / "x.py").write_text("x\n")
        os.symlink(real, repo / adw.SKILL_SRC / "linked")
        with self.assertRaises(adw.Refused):
            adw.source_items(repo, ("linked/x.py",))


class SourceTests(unittest.TestCase):
    def test_helper_shells_out_to_nothing_and_imports_no_installer(self):
        text = SCRIPT.read_text()
        for banned in ("import subprocess", "os.system", "os.popen", "hermes-install",
                       "config.yaml.tmpl", "scripts/lib.sh", "gateway"):
            self.assertNotIn(banned, text)

    def test_makefile_targets_invoke_only_the_helper(self):
        make = (REPO / "Makefile").read_text()
        for target, flag in (("hermes-dev-workflow-plan", "--plan"),
                             ("hermes-dev-workflow-apply", "--apply")):
            block = make.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
            recipe = [l.strip() for l in block.splitlines()[1:] if l.startswith("\t")]
            self.assertEqual(len(recipe), 1, recipe)
            self.assertTrue(recipe[0].startswith(f"@python3 scripts/apply-dev-workflow.py {flag}"), recipe)


if __name__ == "__main__":
    unittest.main()
