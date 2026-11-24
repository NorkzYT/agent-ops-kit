"""Pinned native Hermes patch artifacts (hermes/native-patches) and their read-only verifier.

Run: python3 -m unittest discover -s scripts/tests -p 'test_native_patch.py'

Hermetic and offline: the shipped artifact is checked statically, the verifier's checks run on
in-memory bytes, and CLI runs read only the shipped artifact with HOME/HERMES_HOME pointed at
fixture-named paths that are never created and PATH emptied (so any git call would fail). No temp
files, no ~/.hermes, no installed runtime, no git repositories.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "hermes-native-patch.py"
ART = REPO / "hermes" / "native-patches" / "aux-model-floor"

_spec = importlib.util.spec_from_file_location("hermes_native_patch", HELPER)
hnp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hnp)

C = "a" * 40
BASE = "b" * 40
TREE = "c" * 40
PATCH = (f"From {C} Mon Sep 17 00:00:00 2001\nFrom: t <t@example.invalid>\nSubject: [PATCH] fix\n\n---\n"
         "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-FLOOR = False\n+FLOOR = True\n"
         "-- \n").encode()


def manifest(patch=PATCH, **over):
    m = {"schema": "hermes-native-patch/v1", "name": "synthetic", "status": "candidate-disabled",
         "enabled": False, "auto_apply": False, "review": {"deployment_approved": False},
         "upstream": {"base": BASE, "base_tree": "d" * 40},
         "patch": {"file": "0001-fix.patch", "sha256": hashlib.sha256(patch).hexdigest(), "result_tree": TREE},
         "reviewed_source": {"commit": C, "tree": TREE, "parent": BASE}}
    m.update(over)
    return m


ENTRIES = ["0001-fix.patch", "README.md", "manifest.json"]


def check(m, patch=PATCH, entries=ENTRIES):
    return hnp.check_patch(hnp.parse_manifest(json.dumps(m).encode()), entries, patch)


def run_cli(*args):
    fixture = REPO / "scripts" / "tests" / ".native-patch-fixture-home-never-created"
    env = {"HOME": str(fixture), "HERMES_HOME": str(fixture / ".hermes"), "PATH": "",
           "GIT_DIR": str(fixture / "git"), "GIT_INDEX_FILE": str(fixture / "index"), "LC_ALL": "C"}
    r = subprocess.run([sys.executable, "-I", str(HELPER), *args], capture_output=True, env=env)
    assert not fixture.exists()
    return r


class ShippedArtifact(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((ART / "manifest.json").read_text())

    def test_disabled_and_pinned(self):
        m = self.m
        self.assertIs(m["enabled"], False)
        self.assertIs(m["auto_apply"], False)
        self.assertIs(m["review"]["deployment_approved"], False)
        self.assertEqual(m["upstream"]["base"], "6b21d0564de0492e2ebd0d8f63fca86c03b5c892")
        self.assertEqual(m["upstream"]["base_tree"], "31134041c2353045891fd394da206bac911e5002")
        self.assertEqual(m["reviewed_source"]["commit"], "57a36ef6a0720c30a80573cd40e8274f8cc81c36")
        self.assertEqual(m["reviewed_source"]["tree"], "3075a8f13ae6fb983be150abfd0cb31a8773768d")
        self.assertEqual(m["patch"]["result_tree"], m["reviewed_source"]["tree"])
        self.assertEqual(m["patch"]["sha256"], "283bcb722a82934ba0ec769239d6be284f2a8cc867ab93efb214486d1fb6b935")

    def test_single_patch_bytes_match(self):
        patches = sorted(ART.glob("*.patch"))
        self.assertEqual([p.name for p in patches], [self.m["patch"]["file"]])
        data = patches[0].read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.m["patch"]["sha256"])

    def test_separate_from_legacy_runtime_patches(self):
        legacy = json.loads((REPO / "hermes" / "runtime-patches" / "manifest.json").read_text())
        names = {p["file"] for p in legacy["patches"]}
        self.assertNotIn(self.m["patch"]["file"], names)
        self.assertFalse((REPO / "hermes" / "runtime-patches" / self.m["patch"]["file"]).exists())
        self.assertNotEqual(legacy["base"], self.m["upstream"]["base"])

    def test_kit_config_does_not_claim_native_coverage(self):
        tmpl = (REPO / "hermes" / "config.yaml.tmpl").read_text()
        self.assertNotIn("gpt_model_floor", tmpl)
        self.assertFalse(self.m["runtime_config"]["kit_template_sets_it"])

    def test_no_write_execute_surface(self):
        for path in (REPO / "scripts").glob("*.sh"):
            self.assertNotIn("native-patches", path.read_text(errors="replace"), path.name)
        mk = (REPO / "Makefile").read_text()
        self.assertNotRegex(mk, r"hermes-native-patch-(apply|test)\b")
        for line in mk.splitlines():
            if "hermes-native-patch.py" in line:
                self.assertNotRegex(line, r"restart|install|sudo|--src|apply|SRC")
        src = HELPER.read_text()
        for forbidden in ("import subprocess", "os.system", "os.exec", "os.spawn", "shutil", "tempfile",
                          "write_text", "write_bytes", "O_WRONLY", "O_RDWR", "O_CREAT"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_docs_have_no_stale_apply_instructions(self):
        for path in (ART / "README.md", REPO / "docs" / "hermes-source-management.md",
                     REPO / "docs" / "model-routing.md"):
            text = path.read_text()
            self.assertNotRegex(text, r"hermes-native-patch-(apply|test)|native-patch\.py (apply|test)", path.name)
            self.assertNotRegex(text, r"git apply --index", path.name)


    def test_test_evidence_is_recorded_exactly(self):
        readme = (ART / "README.md").read_text()
        tests = self.m["verification"]["tests"]
        for text in (readme, tests):
            self.assertNotIn("no result", text)
            for fact in ("155", "154", "1859 tests", "test_run_agent", "300", "277 passed", "209 passed"):
                self.assertIn(fact, text, fact)

    def test_reproduce_recipe_is_safe_to_paste(self):
        readme = (ART / "README.md").read_text()
        recipe = readme.split("## Reproducing the tree", 1)[1].split("## Regenerating", 1)[0]
        blocks = recipe.split("```bash\n")[1:]
        self.assertEqual(len(blocks), 2, "placeholder assignment and recipe are separate blocks")
        placeholder, body = (b.split("```", 1)[0] for b in blocks)
        self.assertIn("/path/outside/every/hermes/home", placeholder)
        self.assertNotIn("/path/outside", body)
        # Nothing may exit the reader's interactive shell: the recipe is one subshell.
        self.assertTrue(body.startswith("(\n") and body.rstrip().endswith(")"), body)
        self.assertNotRegex(body.split("(", 1)[0], r"\bexit\b")
        # The network fetch happens only after the verifier succeeded and its output re-hashed.
        self.assertLess(body.index("verify --emit-patch"), body.index("git fetch"))
        self.assertLess(body.index(self.m["patch"]["sha256"]), body.index("git fetch"))
        self.assertNotRegex(body, r"--emit-patch\s*\|")

    def test_readme_states_o_nofollow_scope(self):
        readme = (ART / "README.md").read_text()
        self.assertIn("final path component", readme)
        self.assertIn("final path component", HELPER.read_text())


class VerifierCLI(unittest.TestCase):
    def test_verify_shipped_artifact_without_git_or_home(self):
        r = run_cli("verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"verified (offline, read-only)", r.stdout)
        self.assertIn(b"283bcb722a82934ba0ec769239d6be284f2a8cc867ab93efb214486d1fb6b935", r.stdout)

    def test_emit_patch_is_exact_verified_bytes(self):
        r = run_cli("verify", "--emit-patch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, (ART / "0001-fix-enforce-auxiliary-model-floor-before-dispatch.patch").read_bytes())
        self.assertIn(b"verified", r.stderr)

    def test_apply_and_test_subcommands_removed(self):
        for cmd in ("apply", "test"):
            with self.subTest(cmd=cmd):
                r = run_cli(cmd, "--src", str(REPO))
                self.assertEqual(r.returncode, 2)
                self.assertIn(b"invalid choice", r.stderr)
        r = run_cli("verify", "--src", str(REPO))
        self.assertEqual(r.returncode, 2)

    def test_missing_dir_fails_cleanly(self):
        r = run_cli("verify", "--dir", str(REPO / "scripts" / "tests" / "no-such-artifact"))
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"artifact directory unreadable", r.stderr)


class Checks(unittest.TestCase):
    def test_good_synthetic(self):
        self.assertEqual(check(manifest()), ["mod.py"])

    def test_manifest_gates(self):
        for key, val in {"enabled": True, "auto_apply": True, "schema": "other",
                         "review": {"deployment_approved": True}}.items():
            with self.subTest(key=key), self.assertRaisesRegex(hnp.Fail, key.split("_")[0]):
                check(manifest(**{key: val}))
        bad = manifest()
        bad["reviewed_source"]["parent"] = "e" * 40
        with self.assertRaisesRegex(hnp.Fail, "parent"):
            check(bad)
        bad = manifest()
        bad["patch"]["file"] = "../x.patch"
        with self.assertRaisesRegex(hnp.Fail, "plain"):
            check(bad)

    def test_name_is_a_plain_slug(self):
        for bad in (None, 7, "", "Aux", "../x", "a/b", "x\x1b[2J", "-x", "a" * 65):
            with self.subTest(name=bad), self.assertRaisesRegex(hnp.Fail, "invalid: name is not a plain"):
                check(manifest(name=bad))

    def test_name_matches_artifact_directory(self):
        m = hnp.parse_manifest(json.dumps(manifest()).encode())
        hnp.check_dir_name(m, "/x/native-patches/synthetic")
        hnp.check_dir_name(m, "/x/native-patches/synthetic/")
        with self.assertRaisesRegex(hnp.Fail, "artifact directory name 'other'"):
            hnp.check_dir_name(m, "/x/native-patches/other")

    def test_duplicate_keys_and_nonfinite(self):
        for raw in (b'{"schema": "a", "schema": "b"}', b'{"x": NaN}', b"[]", b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(hnp.Fail):
                hnp.parse_manifest(raw)

    def test_hash_mismatch(self):
        with self.assertRaisesRegex(hnp.Fail, "sha256"):
            check(manifest(), patch=PATCH + b"\n")

    def test_extra_or_unknown_entries(self):
        with self.assertRaisesRegex(hnp.Fail, "extra patch"):
            check(manifest(), entries=ENTRIES + ["0002-extra.patch"])
        with self.assertRaisesRegex(hnp.Fail, "unexpected files"):
            check(manifest(), entries=ENTRIES + ["apply.sh"])

    def test_patch_shape(self):
        cases = {
            "header": PATCH.replace(C.encode(), b"f" * 40, 1),
            "more than one": PATCH + f"From {'e' * 40} Mon Sep 17 00:00:00 2001\n".encode(),
            "CR": PATCH.replace(b"\n", b"\r\n"),
            "signature": PATCH + b"2.43.0\n",
            "binary": PATCH + b"GIT binary patch\n",
            "no file diffs": PATCH.split(b"diff --git")[0],
        }
        for label, data in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(hnp.Fail, label):
                check(manifest(patch=data), patch=data)


if __name__ == "__main__":
    unittest.main()
