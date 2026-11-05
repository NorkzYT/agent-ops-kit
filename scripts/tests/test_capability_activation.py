"""Milestone E: exact selected-file manifest, inert bundle, rehearsal-only apply/rollback.

Every apply here targets a NEW private disposable home created in a temp dir (marker + nonce);
the live Hermes home is never a target (the guard refuses it). Default CLI paths of the
installed capability root are exercised from that disposable home with explicit fixture
inputs from this source tree. Nothing is installed into a live home, restarted or published.
"""

from __future__ import annotations

import ast
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

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capability_activation as ca  # noqa: E402

MANIFEST = ca.build_manifest()
LOCK = json.loads((REPO / "hermes/capabilities/upstreams.lock.json").read_text())
ROOT = ca.ROOT_DEST


def tree(root: Path) -> dict:
    """{relative path: (kind, sha256 or '', mode)} snapshot (bytes, modes, absence)."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        kind = "link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file"
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if kind == "file" else ""
        out[str(p.relative_to(root))] = (kind, digest, stat.S_IMODE(st.st_mode))
    return out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cls_tmp = tempfile.TemporaryDirectory()
        cls.bundle = Path(cls.cls_tmp.name) / "bundle"
        ca.write_bundle(cls.bundle)
        cls.msha = hashlib.sha256((cls.bundle / "manifest.json").read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.cls_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.home = self.d / "rehearsal-home"
        self.nonce = ca.make_rehearsal_home(self.home)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self) -> Path:
        self.n += 1
        return self.d / f"journal-{self.n}.json"

    def apply(self, **kw):
        return ca.apply(self.home, self.bundle, self.nonce, self.msha, self.journal(), sys.executable, **kw)


class ManifestTest(unittest.TestCase):
    def test_committed_manifest_equals_the_computed_one(self):
        self.assertEqual(ca.MANIFEST_PATH.read_bytes(), ca.manifest_bytes(MANIFEST),
                         "regenerate: python3 scripts/capability_activation.py manifest > hermes/capabilities/activation/manifest.json")
        self.assertEqual(ca._registry().schema_errors(MANIFEST, "activation-manifest.schema.json", "m"), [])

    def test_exact_selection_twelve_skills_relative_destinations(self):
        reg = json.loads((REPO / "hermes/capabilities/registry.json").read_text())
        exposed = sorted(c["exposed_skill_name"] for c in reg["capabilities"] if c["lifecycle"] == "source-implemented")
        self.assertEqual(MANIFEST["skills"], exposed)
        self.assertEqual(len(exposed), 12)
        for f in MANIFEST["files"]:
            self.assertFalse(f["destination"].startswith("/") or ".." in f["destination"].split("/"))
            self.assertTrue(f["destination"].startswith((ca.SKILLS_DEST + "/", ROOT + "/")))
            self.assertEqual(f["mode"], "0644")
            self.assertEqual(f["sha256"], hashlib.sha256((REPO / f["source"]).read_bytes()).hexdigest())
        skill_files = {f["source"] for f in MANIFEST["files"] if f["role"] == "skill"}
        on_disk = {p.relative_to(REPO).as_posix() for s in exposed for p in (REPO / "hermes/skills" / s).rglob("*") if p.is_file()}
        self.assertEqual(skill_files, on_disk, "whole skill directories, nothing else under skills/")
        for name in ("autonomous-engineering", "kanban-workflow", "document-converter"):
            self.assertFalse(any(f["destination"].startswith(f"{ca.SKILLS_DEST}/{name}/") for f in MANIFEST["files"]),
                             "existing kit skills are not re-installed by this bundle")

    def test_dependency_closure_of_the_capability_root(self):
        dests = {f["destination"] for f in MANIFEST["files"]}
        reg = json.loads((REPO / "hermes/capabilities/registry.json").read_text())
        for c in reg["capabilities"]:
            for rel in c["implementation"]["paths"]:
                self.assertIn(f"{ROOT}/{rel}", dests, f"{c['id']} digest needs {rel}")
        for schema in (REPO / "hermes/capabilities/schemas").glob("*.json"):
            self.assertIn(f"{ROOT}/hermes/capabilities/schemas/{schema.name}", dests)
        bundled = [f for f in MANIFEST["files"] if f["destination"].startswith(f"{ROOT}/scripts/") and f["source"].endswith(".py")]
        for f in bundled:
            src = (REPO / f["source"]).read_text()
            names = {n.names[0].name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)
                     and n.names[0].name.startswith("capability")}
            names |= set(re.findall(r'"((?:capability|config)[\w-]*\.py)"', src))
            for n in names:
                fname = n if n.endswith(".py") else f"{n}.py"
                self.assertIn(f"{ROOT}/scripts/{fname}", dests, f"{f['source']} needs {fname}")
        for skill in MANIFEST["skills"]:
            text = (REPO / "hermes/skills" / skill / "SKILL.md").read_text()
            for script in set(re.findall(r"scripts/([\w-]+\.py)", text)):
                self.assertIn(f"{ROOT}/scripts/{script}", dests, f"{skill} calls {script}")
        self.assertIn(f"{ROOT}/LICENSE", dests)

    def test_bundle_is_inert_original_with_notices(self):
        upstream = {f["sha256"] for f in LOCK["files"] if f.get("sha256")}
        for f in MANIFEST["files"]:
            self.assertNotIn(f["sha256"], upstream, f"{f['source']} is a raw upstream file")
            name = f["destination"].rsplit("/", 1)[-1]
            self.assertFalse(name.endswith((".sh", ".bash", ".js", ".ts")) or name in ("plugin.yaml", "setup", "hooks"), name)
            self.assertNotIn("/hooks/", f["destination"])
            self.assertNotIn("/bin/", f["destination"])
        for skill in MANIFEST["skills"]:
            refs = REPO / "hermes/skills" / skill / "references"
            front = (REPO / "hermes/skills" / skill / "SKILL.md").read_text().split("\n---", 1)[0]
            self.assertNotRegex(front, r"(?m)^(hooks|startup|on_load|setup|preamble)\s*:")
            if skill.startswith("gstack-"):
                notice = (refs / "gstack-license.md").read_text()
                self.assertIn("MIT", notice)
                self.assertIn("Garry Tan", notice)
            elif skill.startswith("pm-"):
                self.assertIn("MIT", (refs / "pm-skills-license.md").read_text())
        self.assertFalse(any("pna" in f["source"].lower() for f in MANIFEST["files"]))

    def test_prerequisite_pin_is_recorded_not_installed(self):
        js = MANIFEST["prerequisites"]["jsonschema"]
        self.assertEqual(js["range"], ">=4.10.3,<5")
        self.assertEqual({r["jsonschema"] for r in js["resolved"]}, {"4.10.3", "4.26.0"})
        self.assertIn("none", js["install"])
        req = (REPO / "scripts/requirements-capabilities.txt").read_text()
        self.assertIn("jsonschema>=4.10.3,<5", req)


class BundleIntegrityTest(Base):
    def copy(self):
        dst = self.d / "copy"
        shutil.copytree(self.bundle, dst)
        return dst

    def test_bundle_matches_and_is_mode_0644_without_symlinks(self):
        manifest, msha = ca.load_bundle(self.bundle)
        self.assertEqual(msha, self.msha)
        for p in (self.bundle / "files").rglob("*"):
            self.assertFalse(p.is_symlink())
            if p.is_file():
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)
        with self.assertRaises(ca.ActivationError):
            ca.write_bundle(self.bundle)  # existing directory: refused

    def test_tampered_extra_or_mode_changed_bundles_are_refused(self):
        rel = MANIFEST["files"][0]["destination"]
        for label, mutate in [
            ("bytes", lambda b: (b / "files" / rel).write_text("tampered")),
            ("extra", lambda b: (b / "files" / ROOT / "extra.py").write_text("x")),
            ("mode", lambda b: os.chmod(b / "files" / rel, 0o755)),
            ("symlink", lambda b: os.symlink("/etc/passwd", b / "files" / ROOT / "link")),
            ("traversal", lambda b: (b / "manifest.json").write_text(
                (b / "manifest.json").read_text().replace(f'"{rel}"', '"../../escape"', 1))),
        ]:
            with self.subTest(label):
                b = self.copy()
                mutate(b)
                with self.assertRaises((ca.ActivationError, ValueError)):
                    ca.load_bundle(b)
                shutil.rmtree(b)


class TargetGuardTest(Base):
    def test_only_a_verified_disposable_home_is_accepted(self):
        self.assertEqual(ca.verify_rehearsal_home(self.home, self.nonce), self.home)
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(self.home, "0" * 32)  # wrong nonce
        os.chmod(self.home, 0o755)
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(self.home, self.nonce)  # not private
        os.chmod(self.home, 0o700)
        (self.home / "config.yaml").write_text("x: 1\n")
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(self.home, self.nonce)  # looks like a real home
        os.unlink(self.home / "config.yaml")
        plain = self.d / "plain"
        plain.mkdir(mode=0o700)
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(plain, self.nonce)  # no marker
        link = self.d / "link"
        os.symlink(self.home, link)
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(link, self.nonce)

    def test_live_homes_are_refused_even_with_a_marker(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            with self.assertRaises(ca.ActivationError) as cm:
                ca.verify_rehearsal_home(self.home, self.nonce)
            self.assertIn("real Hermes home", str(cm.exception))
        inner = self.home / "nested"
        nonce = ca.make_rehearsal_home(inner)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            with self.assertRaises(ca.ActivationError):
                ca.verify_rehearsal_home(inner, nonce)  # inside a live home
        real = Path(ca.pwd.getpwuid(os.getuid()).pw_dir) / ".hermes"
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(real, self.nonce)

    def test_cli_has_no_default_target(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "capability_activation.py"), "apply", "--bundle", str(self.bundle)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--target", r.stderr)


class ApplyRollbackTest(Base):
    def test_apply_verify_idempotent_rollback_restores_the_exact_preimage(self):
        (self.home / "unrelated.txt").write_text("keep me")
        before = tree(self.home)
        plan = ca.preflight(self.home, self.bundle, self.nonce, sys.executable)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["would_write"], len(MANIFEST["files"]))
        j1 = self.journal()
        journal = ca.apply(self.home, self.bundle, self.nonce, self.msha, j1, sys.executable)
        self.assertEqual(journal["status"], "applied")
        self.assertEqual(sum(1 for e in journal["entries"] if e["action"] == "created"), len(MANIFEST["files"]))
        self.assertTrue(ca.verify_installed(self.home, self.bundle)["ok"])
        for f in MANIFEST["files"]:
            p = self.home / f["destination"]
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)
            self.assertEqual(p.read_bytes(), (REPO / f["source"]).read_bytes())
        self.assertFalse(list(self.home.rglob("*.tmp")))
        after = tree(self.home)
        again = self.apply()
        self.assertEqual({e["action"] for e in again["entries"]}, {"unchanged"})
        self.assertEqual(tree(self.home), after, "a repeated apply writes nothing")
        report = ca.rollback(self.home, self.nonce, j1, self.d / "rollback.json", self.bundle)
        self.assertEqual((report["status"], len(report["removed"])), ("rolled-back", len(MANIFEST["files"])))
        self.assertEqual(tree(self.home), before, "bytes, modes and absence restored; unrelated file kept")

    def test_collisions_refuse_before_any_write(self):
        dest = self.home / MANIFEST["files"][0]["destination"]
        dest.parent.mkdir(parents=True)
        dest.write_text("someone else's file")
        before = tree(self.home)
        with self.assertRaises(ca.ActivationError) as cm:
            self.apply()
        self.assertIn("differs", str(cm.exception))
        self.assertEqual(tree(self.home), before)
        shutil.rmtree(self.home / "agent-ops-kit", ignore_errors=True)
        shutil.rmtree(self.home / "skills", ignore_errors=True)
        other = self.home / "skills" / "devops" / "qa-elsewhere"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: gstack-qa\ndescription: a different skill\n---\nbody\n")
        before = tree(self.home)
        with self.assertRaises(ca.ActivationError) as cm:
            self.apply()
        self.assertIn("already uses the name gstack-qa", str(cm.exception))
        self.assertEqual(tree(self.home), before)

    def test_symlinked_parent_is_refused(self):
        outside = self.d / "outside"
        outside.mkdir()
        os.symlink(outside, self.home / "skills")
        with self.assertRaises(ca.ActivationError):
            self.apply()
        self.assertEqual(list(outside.iterdir()), [])

    def test_wrong_manifest_digest_or_missing_jsonschema_refuses(self):
        before = tree(self.home)
        with self.assertRaises(ca.ActivationError):
            ca.apply(self.home, self.bundle, self.nonce, "0" * 64, self.journal(), sys.executable)
        fake = self.d / "nopython"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o700)
        with self.assertRaises(ca.ActivationError) as cm:
            ca.apply(self.home, self.bundle, self.nonce, self.msha, self.journal(), str(fake))
        self.assertIn("not importable", str(cm.exception))
        self.assertEqual(tree(self.home), before)

    def test_caught_error_mid_apply_is_reverted_to_the_preimage(self):
        # A caught exception only; SIGKILL/power loss leaves an in-progress journal (see docs).
        before = tree(self.home)
        j = self.d / "j-fail.json"
        with self.assertRaises(ca.ActivationError):
            ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable, _fail_after=5)
        self.assertEqual(tree(self.home), before)
        journal = json.loads(j.read_text())
        self.assertEqual(journal["status"], "failed-reverted")
        self.assertEqual(len(journal["revert"]["removed"]), 5)

    def test_rollback_refuses_changed_files_and_keeps_new_neighbours(self):
        j = self.journal()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        changed = self.home / MANIFEST["files"][3]["destination"]
        changed.write_text("newer work by someone else")
        snap = tree(self.home)
        with self.assertRaises(ca.ActivationError):
            ca.rollback(self.home, self.nonce, j, self.d / "rb1.json", self.bundle)
        self.assertEqual(tree(self.home), snap, "a refused rollback removes nothing")
        changed.write_bytes((self.bundle / "files" / MANIFEST["files"][3]["destination"]).read_bytes())
        neighbour = self.home / ca.SKILLS_DEST / "gstack-qa" / "notes.md"
        neighbour.write_text("added later")
        ca.rollback(self.home, self.nonce, j, self.d / "rb2.json", self.bundle)
        self.assertTrue(neighbour.exists())
        self.assertFalse((self.home / ROOT).exists())


def rewrite_bundle(bundle: Path, rel: str, data: bytes) -> str:
    """Turn ``bundle`` into a different, internally consistent bundle; returns its manifest sha256."""
    (bundle / "files" / rel).write_bytes(data)
    m = json.loads((bundle / "manifest.json").read_text())
    for f in m["files"]:
        if f["destination"] == rel:
            f["sha256"], f["size"] = hashlib.sha256(data).hexdigest(), len(data)
    m["bundle_digest"] = hashlib.sha256(json.dumps(m["files"], sort_keys=True).encode()).hexdigest()
    raw = ca.manifest_bytes(m)
    (bundle / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class RollbackReplayAndPartialTest(Base):
    """Review 2026-11-05: a journal stayed 'applied' after rollback (replayable against a later
    apply), a same-user journal could name any file under the allowed roots, and an OSError mid-
    removal left an empty report."""

    def test_a_rolled_back_journal_cannot_be_replayed_against_a_later_apply(self):
        j1 = self.journal()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, j1, sys.executable)
        ca.rollback(self.home, self.nonce, j1, self.d / "rb1.json", self.bundle)
        self.assertEqual(json.loads(j1.read_text())["status"], "rolled-back")
        j2 = self.journal()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, j2, sys.executable)
        snap = tree(self.home)
        with self.assertRaises(ca.ActivationError):
            ca.rollback(self.home, self.nonce, j1, self.d / "rb2.json", self.bundle)
        self.assertEqual(tree(self.home), snap, "the stale journal removed nothing")

    def test_journal_entries_must_be_files_of_the_approved_bundle(self):
        j = self.journal()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        victim = self.home / ca.SKILLS_DEST / "kanban-workflow" / "SKILL.md"
        victim.parent.mkdir(parents=True)
        victim.write_text("pre-existing kit skill")
        victim.chmod(0o644)
        doc = json.loads(j.read_text())
        doc["entries"] = [{"destination": f"{ca.SKILLS_DEST}/kanban-workflow/SKILL.md", "preimage": "absent",
                           "action": "created", "sha256": hashlib.sha256(victim.read_bytes()).hexdigest(), "mode": "0644"}]
        doc["created_dirs"] = []
        forged = self.d / "forged.json"
        forged.write_text(json.dumps(doc))
        with self.assertRaises(ca.ActivationError):
            ca.rollback(self.home, self.nonce, forged, self.d / "rb.json", self.bundle)
        self.assertTrue(victim.is_file())

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_a_failure_mid_removal_is_reported_not_left_empty(self):
        j = self.journal()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        locked = self.home / ca.SKILLS_DEST / "gstack-qa"
        locked.chmod(0o555)
        try:
            out = self.d / "rb.json"
            with self.assertRaises(ca.ActivationError):
                ca.rollback(self.home, self.nonce, j, out, self.bundle)
            rep = json.loads(out.read_text())
            self.assertEqual(rep["status"], "partial")
            self.assertTrue(rep["removed"])
            self.assertTrue(rep["errors"])
            self.assertEqual(json.loads(j.read_text())["status"], "rollback-partial")
        finally:
            locked.chmod(0o755)


class ApprovedContentAtWriteTest(Base):
    def own_bundle(self):
        b = self.d / "own-bundle"
        shutil.copytree(self.bundle, b)
        return b

    def test_bundle_replaced_after_preflight_is_refused_before_the_journal(self):
        b = self.own_bundle()
        rel = MANIFEST["files"][4]["destination"]
        real = ca.preflight

        def preflight_then_swap(*a, **kw):
            plan = real(*a, **kw)
            rewrite_bundle(b, rel, b"swapped after preflight\n")
            return plan

        before, j = tree(self.home), self.d / "j-swap.json"
        with mock.patch.object(ca, "preflight", preflight_then_swap):
            with self.assertRaises(ca.ActivationError) as cm:
                ca.apply(self.home, b, self.nonce, self.msha, j, sys.executable)
        self.assertIn("approved", str(cm.exception))
        self.assertEqual(tree(self.home), before)
        self.assertFalse(os.path.lexists(j))

    def test_bundled_bytes_changed_during_apply_are_not_written(self):
        b = self.own_bundle()
        rel = MANIFEST["files"][6]["destination"]
        real, calls = ca.load_bundle, []

        def load_then_tamper(bundle):
            out = real(bundle)
            calls.append(1)
            if len(calls) == 2:  # the reload inside apply: change bytes after every check
                (b / "files" / rel).write_bytes(b"tampered mid-apply\n")
            return out

        before, j = tree(self.home), self.d / "j-tamper.json"
        with mock.patch.object(ca, "load_bundle", load_then_tamper):
            with self.assertRaises(ca.ActivationError):
                ca.apply(self.home, b, self.nonce, self.msha, j, sys.executable)
        self.assertEqual(tree(self.home), before)
        self.assertEqual(json.loads(j.read_text())["status"], "failed-reverted")


class JournalSafetyTest(Base):
    """Rollback trusts nothing in the journal: forged or unsafe journals are refused before any
    unlink/rmdir. os.unlink/os.rmdir are inert recording mocks while a forged journal is used, so a
    would-delete path outside the fixture is never touched even if a check regressed."""

    def setUp(self):
        super().setUp()
        self.j = self.journal()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, self.j, sys.executable)
        self.good = json.loads(self.j.read_text())

    def forged(self, journal) -> Path:
        p = self.d / f"forged-{self.n}.json"
        self.n += 1
        p.write_text(journal if isinstance(journal, str) else json.dumps(journal))
        return p

    def assert_refused(self, journal_path, out=None):
        out = out or self.d / f"report-{self.n}.json"
        self.n += 1
        snap, calls = tree(self.home), []
        with mock.patch("os.unlink", side_effect=lambda *a, **k: calls.append(("unlink", a))), \
                mock.patch("os.rmdir", side_effect=lambda *a, **k: calls.append(("rmdir", a))):
            with self.assertRaises((ca.ActivationError, OSError)):
                ca.rollback(self.home, self.nonce, journal_path, out, self.bundle)
        self.assertEqual(calls, [], "nothing may be removed for a refused journal")
        self.assertEqual(tree(self.home), snap)
        return out

    def mutate(self, fn):
        j = json.loads(json.dumps(self.good))
        fn(j)
        return j

    def test_forged_paths_dirs_and_schema_are_refused(self):
        created = next(i for i, e in enumerate(self.good["entries"]) if e["action"] == "created")
        bad_dest = ["../outside.txt", "/etc/passwd", f"{ca.SKILLS_DEST}/../../x", f"{ca.SKILLS_DEST}/./x",
                    f"{ca.SKILLS_DEST}//x", "plugins/model-router/__init__.py", ca.SKILLS_DEST, f"{ROOT}/x\\..",
                    f"{ROOT}/" + "a" * 300, f"{ROOT}/x\n"]
        cases = [(f"dest {d!r}", (lambda d: lambda j: j["entries"][created].update(destination=d))(d)) for d in bad_dest]
        bad_dirs = ["..", "/tmp", "plugins", f"{ca.SKILLS_DEST}/../..", f"{ROOT}/not-an-ancestor-of-any-entry", "", "."]
        cases += [(f"dir {d!r}", (lambda d: lambda j: j["created_dirs"].append(d))(d)) for d in bad_dirs]
        cases += [
            ("duplicate entry", lambda j: j["entries"].append(dict(j["entries"][created]))),
            ("duplicate dir", lambda j: j["created_dirs"].append(j["created_dirs"][0])),
            ("extra top key", lambda j: j.update(extra=1)),
            ("extra entry key", lambda j: j["entries"][created].update(force=True)),
            ("missing key", lambda j: j.pop("created_dirs")),
            ("schema_version type", lambda j: j.update(schema_version="1")),
            ("entries type", lambda j: j.update(entries={})),
            ("bad digest", lambda j: j["entries"][created].update(sha256="x" * 64)),
            ("bad manifest digest", lambda j: j.update(manifest_sha256="0")),
            ("bad mode", lambda j: j["entries"][created].update(mode="0755")),
            ("bad action", lambda j: j["entries"][created].update(action="deleted")),
            ("unchanged entry with a digest", lambda j: j["entries"].append(
                {"destination": f"{ROOT}/z", "preimage": "identical", "action": "unchanged", "sha256": "0" * 64})),
            ("status", lambda j: j.update(status="failed-partial")),
            ("other target", lambda j: j.update(target=str(self.d))),
        ]
        for label, fn in cases:
            with self.subTest(label):
                self.assert_refused(self.forged(self.mutate(fn)))
        raw = self.j.read_text().replace('"status": "applied"', '"status": "applied", "status": "applied"', 1)
        with self.subTest("duplicate JSON key"):
            self.assert_refused(self.forged(raw))
        with self.subTest("not JSON"):
            self.assert_refused(self.forged("{"))

    def test_unsafe_journal_files_are_refused(self):
        link = self.d / "journal-link.json"
        os.symlink(self.j, link)
        hard = self.d / "journal-hard.json"
        os.link(self.j, hard)
        big = self.forged(json.dumps(self.good) + " " * (ca.MAX_JOURNAL + 1))
        real_dir = self.d / "real"
        real_dir.mkdir()
        shutil.copy(self.j, real_dir / "j.json")
        os.symlink(real_dir, self.d / "dirlink")
        for label, p in [("symlink", link), ("hard link", hard), ("oversize", big),
                         ("symlinked parent", self.d / "dirlink" / "j.json"), ("journal inside target", None)]:
            with self.subTest(label):
                if p is None:
                    p = self.home / "j.json"
                    shutil.copy(self.j, p)
                self.assert_refused(p)

    def test_symlinked_component_at_rollback_is_never_followed(self):
        outside = self.d / "outside"
        shutil.move(str(self.home / "agent-ops-kit"), str(outside))
        os.symlink(outside, self.home / "agent-ops-kit")
        before_outside = tree(outside)
        with self.assertRaises(ca.ActivationError):
            ca.rollback(self.home, self.nonce, self.j, self.d / "rb.json", self.bundle)
        self.assertEqual(tree(outside), before_outside, "files behind a symlinked parent stay")
        self.assertTrue((self.home / ca.SKILLS_DEST / "gstack-qa" / "SKILL.md").is_file(), "refusal removes nothing")

    def test_report_path_is_checked_before_any_removal(self):
        taken = self.d / "taken.json"
        taken.write_text("keep")
        os.symlink(self.d, self.d / "outlink")
        for label, out in [("exists", taken), ("dangling symlink", self.d / "dangling.json"),
                           ("symlinked parent", self.d / "outlink" / "r.json"), ("inside target", self.home / "r.json"),
                           ("missing parent", self.d / "nope" / "r.json"), ("is the journal", self.j)]:
            if label == "dangling symlink":
                os.symlink(self.d / "nowhere", out)
            with self.subTest(label):
                self.assert_refused(self.j, out=out)
        self.assertEqual(taken.read_text(), "keep")
        report = ca.rollback(self.home, self.nonce, self.j, self.d / "ok.json", self.bundle)
        self.assertEqual(report["status"], "rolled-back")
        self.assertEqual(json.loads((self.d / "ok.json").read_text())["status"], "rolled-back")


class JournalWriteTest(Base):
    def test_journal_update_never_follows_a_planted_temp_symlink(self):
        # Review 2026-11-05 (act7): the temp name is randomized, so the symlink is planted at the
        # exact name the journal update will use (os.urandom made predictable for this apply).
        victim = self.d / "victim.txt"
        victim.write_text("do not overwrite")
        j = self.d / "j.json"
        planted = self.d / f".j.json.{os.getpid()}.{'00' * 8}.tmp"
        os.symlink(victim, planted)
        before = tree(self.home)
        with mock.patch.object(ca.os, "urandom", lambda n: b"\0" * n):
            with self.assertRaises(ca.ActivationError) as cm:
                ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual(victim.read_text(), "do not overwrite")
        self.assertTrue(os.path.islink(planted), "the planted symlink was neither followed nor replaced")
        self.assertFalse(stat.S_ISLNK(os.lstat(j).st_mode))
        self.assertEqual(tree(self.home), before, "the apply was reverted when its journal could not be completed")

    def test_journal_or_target_under_a_symlinked_parent_is_refused(self):
        real = self.d / "jdir"
        real.mkdir()
        os.symlink(real, self.d / "jlink")
        before = tree(self.home)
        with self.assertRaises(ca.ActivationError):
            ca.apply(self.home, self.bundle, self.nonce, self.msha, self.d / "jlink" / "j.json", sys.executable)
        self.assertEqual(tree(self.home), before)
        self.assertEqual(list(real.iterdir()), [])
        os.symlink(self.d, self.d / "tlink")
        with self.assertRaises(ca.ActivationError):
            ca.verify_rehearsal_home(self.d / "tlink" / "rehearsal-home", self.nonce)


def add_bundle_file(bundle: Path, rel: str, data: bytes, role: str = "skill") -> str:
    """Add one consistent file to ``bundle``; returns the new manifest sha256."""
    p = bundle / "files" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    p.chmod(0o644)
    m = json.loads((bundle / "manifest.json").read_text())
    m["files"].append({"source": "hermes/skills/x", "destination": rel, "role": role,
                       "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": "0644",
                       "capability_ids": []})
    m["files"].sort(key=lambda f: f["destination"])
    m["bundle_digest"] = hashlib.sha256(json.dumps(m["files"], sort_keys=True).encode()).hexdigest()
    raw = ca.manifest_bytes(m)
    (bundle / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class RoundTwoReviewTest(Base):
    """Review round 2 (2026-11-05): act4, act5, act6, act8, act3."""

    def own_bundle(self, name="b2"):
        b = self.d / name
        shutil.copytree(self.bundle, b)
        return b

    def test_bundle_destinations_rollback_would_reject_are_refused_before_apply(self):
        for i, rel in enumerate([f"{ca.SKILLS_DEST}/gstack-qa/references/.notes.md",
                                 f"{ca.SKILLS_DEST}/gstack-qa/./notes.md",
                                 f"{ROOT}/scripts/-dash.py"]):
            with self.subTest(rel):
                b = self.own_bundle(f"norm-{i}")
                msha = add_bundle_file(b, rel, b"extra\n")
                with self.assertRaises(ca.ActivationError) as cm:
                    ca.load_bundle(b)
                self.assertIn("normalized", str(cm.exception))
                before = tree(self.home)
                with self.assertRaises(ca.ActivationError):
                    ca.apply(self.home, b, self.nonce, msha, self.journal(), sys.executable)
                self.assertEqual(tree(self.home), before)

    def test_manifest_skills_must_equal_the_bundled_skill_directories(self):
        b = self.own_bundle()
        m = json.loads((b / "manifest.json").read_text())
        m["skills"].remove("gstack-qa")
        (b / "manifest.json").write_bytes(ca.manifest_bytes(m))
        with self.assertRaises(ca.ActivationError) as cm:
            ca.load_bundle(b)
        self.assertIn("skills", str(cm.exception))
        b = self.own_bundle("b3")
        add_bundle_file(b, f"{ca.SKILLS_DEST}/unlisted-skill/SKILL.md", b"---\nname: unlisted-skill\n---\n")
        with self.assertRaises(ca.ActivationError):
            ca.load_bundle(b)

    def fake_python(self, pyver, jsver):
        p = self.d / f"py-{pyver}-{jsver}"
        p.write_text(f"#!/bin/sh\necho {pyver}\necho {jsver}\n")
        p.chmod(0o700)
        return str(p)

    def test_python_prerequisite_is_compared_and_pre_releases_are_refused(self):
        for pyver, jsver, ok in [("3.12.3", "4.17.3", True), ("3.9.18", "4.17.3", False),
                                 ("3.10.0rc1", "4.17.3", False), ("3.12.3", "4.10.3rc1", False),
                                 ("3.12.3", "5.0.0a1", False), ("3.12.3", "4.10.2", False),
                                 ("3.12.3", "4.26.0", True)]:
            with self.subTest(python=pyver, jsonschema=jsver):
                plan = ca.preflight(self.home, self.bundle, self.nonce, self.fake_python(pyver, jsver))
                self.assertEqual(plan["prerequisites"]["ok"], ok, plan["prerequisites"])
                self.assertEqual(plan["ok"], ok)

    def test_symlinked_intermediate_planted_after_preflight_creates_nothing_outside(self):
        outside = self.d / "outside"
        outside.mkdir()
        real = ca.preflight

        def preflight_then_plant(*a, **kw):
            plan = real(*a, **kw)
            os.symlink(outside, self.home / "skills")
            return plan

        j = self.d / "j-plant.json"
        with mock.patch.object(ca, "preflight", preflight_then_plant):
            with self.assertRaises(ca.ActivationError):
                ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        self.assertEqual(list(outside.iterdir()), [], "no directory or file created through the symlink")
        os.unlink(self.home / "skills")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [ca.MARKER])
        self.assertEqual(json.loads(j.read_text())["status"], "failed-reverted")

    def test_verify_refuses_files_reached_through_a_symlinked_parent(self):
        ca.apply(self.home, self.bundle, self.nonce, self.msha, self.journal(), sys.executable)
        self.assertTrue(ca.verify_installed(self.home, self.bundle)["ok"])
        moved = self.d / "moved-skills"
        shutil.move(str(self.home / "skills"), str(moved))
        os.symlink(moved, self.home / "skills")
        rep = ca.verify_installed(self.home, self.bundle)
        self.assertFalse(rep["ok"])
        self.assertTrue(all(d.startswith("skills/") for d in rep["mismatched"]))
        self.assertTrue(rep["mismatched"])
        os.symlink(self.home, self.d / "home-link")
        with self.assertRaises(ca.ActivationError):
            ca.verify_installed(self.d / "home-link", self.bundle)

    def test_keyboard_interrupt_mid_apply_is_reverted_and_re_raised(self):
        before, j = tree(self.home), self.d / "j-int.json"
        real, calls = ca._write_new, []

        def write_then_interrupt(dest, data, *rest):
            calls.append(dest)
            if len(calls) == 6:
                raise KeyboardInterrupt
            return real(dest, data, *rest)

        with mock.patch.object(ca, "_write_new", write_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        self.assertEqual(tree(self.home), before)
        journal = json.loads(j.read_text())
        self.assertEqual(journal["status"], "failed-reverted")
        self.assertEqual(len(journal["revert"]["removed"]), 5)
        self.assertIn("KeyboardInterrupt", journal["error"])


class PartialTruthTest(Base):
    """Final review: a failure after removals or between link and record never reports a clean
    revert or an empty removal list; an interrupted window is reported as unknown, not reverted."""

    def test_directory_pass_error_after_file_removals_keeps_the_removed_list(self):
        j = self.journal()
        journal = ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        n = sum(1 for e in journal["entries"] if e["action"] == "created")
        real_unlink, real_chain, unlinks = os.unlink, ca._open_dir_chain, []

        def counting_unlink(*a, **k):
            unlinks.append(a)
            return real_unlink(*a, **k)

        def chain(t, parts):
            if len(unlinks) >= n:  # the directory pass: a component changed under us
                raise ca.ActivationError("component replaced (test)")
            return real_chain(t, parts)

        out = self.d / "rb.json"
        with mock.patch.object(ca.os, "unlink", counting_unlink), mock.patch.object(ca, "_open_dir_chain", chain):
            with self.assertRaises(ca.ActivationError):
                ca.rollback(self.home, self.nonce, j, out, self.bundle)
        rep = json.loads(out.read_text())
        self.assertEqual(rep["status"], "partial")
        self.assertEqual(len(rep["removed"]), n)
        self.assertEqual(rep["refused"], [])
        self.assertTrue(rep["errors"])
        self.assertEqual(json.loads(j.read_text())["status"], "rollback-partial")

    def test_temp_unlink_failure_after_link_is_not_reported_as_reverted(self):
        before, j = tree(self.home), self.d / "j-tmp.json"
        real_unlink, seen = os.unlink, []

        def unlink(path, *a, **k):
            if str(path).endswith(".tmp") and str(self.home) in str(path):
                seen.append(path)
                if len(seen) == 3:
                    raise PermissionError(13, "Permission denied (test)")
            return real_unlink(path, *a, **k)

        with mock.patch.object(ca.os, "unlink", unlink):
            with self.assertRaises(ca.ActivationError):
                ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        journal = json.loads(j.read_text())
        self.assertEqual(journal["status"], "failed-partial")
        left = journal["revert"]["cleanup_unknown"]
        self.assertEqual(len(left), 2, left)  # the linked destination and its temp name
        for rel in left:
            self.assertTrue(os.path.lexists(self.home / rel), rel)
        self.assertEqual(len(journal["revert"]["removed"]), 2)
        remaining = set(tree(self.home)) - set(before)
        self.assertTrue(all(r in left or any(x.startswith(r + "/") for x in left) for r in remaining), remaining)

    def test_interrupt_right_after_link_is_reported_unknown_not_reverted(self):
        before, j = tree(self.home), self.d / "j-link.json"
        real_link, n = os.link, []

        def link_then_interrupt(src, dst, *a, **k):
            real_link(src, dst, *a, **k)
            n.append(dst)
            if len(n) == 3:
                raise KeyboardInterrupt  # lands after the syscall succeeded

        with mock.patch.object(ca.os, "link", link_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        journal = json.loads(j.read_text())
        self.assertEqual(journal["status"], "failed-partial")
        self.assertEqual(len(journal["revert"]["cleanup_unknown"]), 1)
        self.assertTrue(os.path.lexists(self.home / journal["revert"]["cleanup_unknown"][0]))
        self.assertEqual(len(journal["revert"]["removed"]), 2)
        self.assertFalse(list(self.home.rglob("*.tmp")))

    def test_directories_made_before_a_failing_open_are_recorded_and_removed(self):
        before, j = tree(self.home), self.d / "j-dir.json"
        real_open, made = os.open, []
        real_mkdir = os.mkdir

        def mkdir(*a, **k):
            real_mkdir(*a, **k)
            made.append(a[0])

        def open_(path, flags, *a, **k):
            if made and len(made) == 1 and k.get("dir_fd") is not None and path == made[0] \
                    and flags & os.O_DIRECTORY:
                made.append("failed")
                raise PermissionError(13, "Permission denied (test)")
            return real_open(path, flags, *a, **k)

        with mock.patch.object(ca.os, "mkdir", mkdir), mock.patch.object(ca.os, "open", open_):
            with self.assertRaises(ca.ActivationError):
                ca.apply(self.home, self.bundle, self.nonce, self.msha, j, sys.executable)
        journal = json.loads(j.read_text())
        self.assertEqual(len(journal["created_dirs"]), 1, journal["created_dirs"])
        self.assertEqual(journal["revert"]["dirs_removed"], journal["created_dirs"])
        self.assertEqual(journal["status"], "failed-reverted")
        self.assertEqual(tree(self.home), before)

    def test_interpreter_probe_timeout_is_a_structured_refusal(self):
        with mock.patch.object(ca.subprocess, "run", side_effect=subprocess.TimeoutExpired(["py"], 60)):
            r = ca.check_prerequisites(sys.executable)
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["reason"])
        plan = None
        with mock.patch.object(ca.subprocess, "run", side_effect=subprocess.TimeoutExpired(["py"], 60)):
            plan = ca.preflight(self.home, self.bundle, self.nonce, sys.executable)
        self.assertFalse(plan["ok"])


class InstalledRootRehearsalTest(Base):
    """Default CLI paths of the installed capability root, run from the disposable home with
    explicit fixture inputs from this source tree (the fixture source root)."""

    def run_root(self, script, *args, **kw):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.d / "user-home"), "HERMES_HOME": str(self.home),
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(self.d)}
        return subprocess.run([sys.executable, str(self.home / ROOT / "scripts" / script), *args], capture_output=True,
                              text=True, env=env, timeout=120, cwd=str(self.d), **kw)

    def test_default_paths_resolve_inside_the_installed_root(self):
        (self.d / "user-home").mkdir()
        ca.apply(self.home, self.bundle, self.nonce, self.msha, self.journal(), sys.executable)
        r = self.run_root("capability_registry.py", "validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["errors"], [])
        r = self.run_root("capability_registry.py", "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            self.assertEqual(parts[3], "no" if parts[2] == "planned" else "yes", line)
        env_file = self.d / "task.json"
        env_file.write_text(json.dumps({"schema_version": 1, "task_id": "t-r", "request_id": "r-r",
                                        "requester_ref": "requester:example-a-operator", "destination_ref": "discord-thread:example-a-ops",
                                        "company_id": "example-a", "project_id": "example-web", "companies_involved": ["example-a"],
                                        "intent": "rehearsal", "requested_outcome": "A plan.", "data_classification": "synthetic",
                                        "egress_class": "deny", "input_artifacts": [], "acceptance_criteria": ["ok"],
                                        "workflow_version": "0.1.0", "recovery_generation": 0}))
        r = self.run_root("capability-plan.py", "--task", str(env_file), "--capability", "pm.lean-prd", "--format", "summary")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kit.problem-definition", r.stdout)
        ev = REPO / "hermes/capabilities/evals/trial-activation-causes"
        r = self.run_root("capability_workflow.py", "validate", "--analysis", str(ev / "analysis-causal.json"),
                          "--evidence", str(ev / "evidence-pack.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = self.d / "rendered"
        r = self.run_root("capability_workflow.py", "render", "--analysis", str(ev / "analysis-causal.json"),
                          "--evidence", str(ev / "evidence-pack.json"), "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((out / "artifacts.json").is_file())
        cases = REPO / "hermes/capabilities/evals/routing/fixture-cases.json"
        case = json.loads(cases.read_text())["cases"][2]
        (self.d / "prompt.txt").write_text(case["task_text"])
        args = ("--task", str(env_file), "--text-file", str(self.d / "prompt.txt"), "--fixture", str(cases), "--selection-only")
        r = self.run_root("capability-select.py", *args)
        self.assertEqual(r.returncode, 2, "no model-router plugin in this home yet")
        self.assertIn("not a model-router plugin directory", r.stderr)
        # Test staging only: the plugin installs through its own existing path, not this bundle.
        shutil.copytree(REPO / "hermes/plugins/model-router", self.home / "plugins" / "model-router",
                        ignore=shutil.ignore_patterns("__pycache__"))
        r = self.run_root("capability-select.py", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["capability_ids"], ["existing.engineering-path"])
        self.assertFalse(list((self.d / "user-home").iterdir()), "nothing written to the user's HOME")


if __name__ == "__main__":
    unittest.main()
