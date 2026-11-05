"""Installed-Hermes compatibility checker (scripts/capability_compat.py) on SYNTHETIC Hermes trees.

The checker parses Hermes source as text; these tests build fake hermes-agent trees in temp
dirs, so nothing here reads the live install. The real-install run is separate evidence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import capability_compat as cc  # noqa: E402

MANAGER = textwrap.dedent('''\
    import re
    MAX_NAME_LENGTH = 64
    MAX_DESCRIPTION_LENGTH = 1024
    MAX_SKILL_CONTENT_CHARS = 100_000
    MAX_SKILL_FILE_BYTES = 1_048_576
    VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
''')
UTILS = 'SKILL_PROMPT_DESC_LIMIT = 60\nSKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))\n'
LINTER = textwrap.dedent('''\
    import re
    _FORBIDDEN_FILES = ("README.md", ".env")
    _EXPECTED_SECTIONS = ("When to Use", "When to use")
    _BODY_SOFT_BUDGET_CHARS = 24_000
    def _check_frontmatter(fm, d):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", fm["name"]):
            pass
''')


def fake_agent(root: Path, **override) -> Path:
    files = {"tools/skill_manager_tool.py": MANAGER, "agent/skill_utils.py": UTILS, "tools/skill_linter.py": LINTER}
    files.update(override)
    for rel, text in files.items():
        p = root / "agent-src" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if text is not None:
            p.write_text(text)
    return root / "agent-src"


class CompatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rules_are_read_as_literals_without_importing(self):
        rules = cc.installed_rules(fake_agent(self.d, **{"tools/skill_manager_tool.py": MANAGER + "raise SystemExit('imported')\n"}))
        self.assertEqual(rules["MAX_SKILL_FILE_BYTES"], 1_048_576)
        self.assertEqual(rules["VALID_NAME_RE"], "^[a-z0-9][a-z0-9._-]*$")
        self.assertEqual(rules["LINTER_NAME_RE"], "[a-z0-9][a-z0-9_-]*")
        self.assertEqual(rules["SKILL_SUPPORT_DIRS"], frozenset({"references", "templates", "assets", "scripts"}))

    def test_every_capability_skill_meets_the_synthetic_rules(self):
        rep = cc.report(fake_agent(self.d), cc.capability_skills(), [], None)
        self.assertEqual(len(rep["skills_checked"]), 12)
        self.assertEqual(rep["findings"], [])
        self.assertTrue(rep["compatible"])

    def test_missing_rule_is_unknown_not_a_pass(self):
        agent = fake_agent(self.d, **{"agent/skill_utils.py": "X = 1\n"})
        rep = cc.report(agent, ["gstack-qa"], [], None)
        self.assertIn(("unknown", "SKILL_PROMPT_DESC_LIMIT"), {(f["level"], f["rule"]) for f in rep["findings"]})
        self.assertFalse(rep["compatible"])

    def test_tighter_rules_produce_findings(self):
        agent = fake_agent(self.d, **{"agent/skill_utils.py": UTILS.replace("60", "20"),
                                      "tools/skill_manager_tool.py": MANAGER.replace("1024", "30")})
        rep = cc.report(agent, ["gstack-qa"], [], None)
        rules = {(f["level"], f["rule"]) for f in rep["findings"]}
        self.assertIn(("advisory", "description-width"), rules)
        self.assertIn(("error", "description-length"), rules)
        self.assertFalse(rep["compatible"])

    def test_broken_skill_is_flagged(self):
        skill = self.d / "repo" / "hermes" / "skills" / "bad"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: Bad Name\ndescription: x\n---\n\nSee references/missing.md\n")
        (skill / "README.md").write_text("x")
        found = {(f["level"], f["rule"]) for f in cc.check_skill(skill, cc.installed_rules(fake_agent(self.d)))}
        for item in [("error", "name-format"), ("advisory", "name-dir-mismatch"), ("advisory", "forbidden-file"),
                     ("advisory", "dangling-reference"), ("advisory", "missing-section"), ("advisory", "missing-metadata")]:
            self.assertIn(item, found)

    def test_collisions_with_installed_skills_are_reported(self):
        roots = self.d / "skills"
        same = roots / "agent-ops-kit" / "gstack-qa"
        same.mkdir(parents=True)
        shutil.copyfile(REPO / "hermes/skills/gstack-qa/SKILL.md", same / "SKILL.md")
        other = roots / "devops" / "review"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: gstack-review\ndescription: someone else's\n---\nbody\n")
        support = roots / "x" / "references" / "gstack-spec"
        support.mkdir(parents=True)
        (support / "SKILL.md").write_text("---\nname: gstack-spec\ndescription: d\n---\nb\n")
        rep = cc.report(fake_agent(self.d), ["gstack-qa", "gstack-review", "gstack-spec"], [roots], None)
        got = {(c["skill"], c["identical"]) for c in rep["collisions"]}
        self.assertEqual(got, {("gstack-qa", True), ("gstack-review", False)})
        self.assertFalse(rep["compatible"], "a different skill with the same name would be ambiguous in Hermes")

    def test_plugin_drift_is_reported(self):
        plugin = self.d / "plugin"
        shutil.copytree(REPO / "hermes/plugins/model-router", plugin, ignore=shutil.ignore_patterns("__pycache__"))
        rep = cc.report(fake_agent(self.d), ["gstack-qa"], [], plugin)
        self.assertEqual((rep["model_router_plugin"]["differs"], rep["model_router_plugin"]["has_capability_seam"]), ([], True))
        (plugin / "specialists.py").write_text("# old\n")
        rep = cc.report(fake_agent(self.d), ["gstack-qa"], [], plugin)
        self.assertEqual((rep["model_router_plugin"]["differs"], rep["model_router_plugin"]["has_capability_seam"]),
                         (["specialists.py"], False))

    def test_cli_exit_codes(self):
        agent = fake_agent(self.d)
        r = subprocess.run([sys.executable, str(REPO / "scripts/capability_compat.py"), "--hermes-agent", str(agent)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["compatible"])
        r = subprocess.run([sys.executable, str(REPO / "scripts/capability_compat.py"), "--hermes-agent", str(self.d / "nope")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
