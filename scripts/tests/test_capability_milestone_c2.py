"""Milestone C2 registry record: pm.competitor-research is a source-implemented PM Skills method adapter that
cites only the hash-verified upstream files, carries the MIT notice, has no network path in its validator,
and whose skill documents commands that actually run. Source-implemented says nothing about installation,
runtime acceptance or execution."""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_milestone_b import cap  # noqa: E402
from workflow_helpers import EVALS, LOCK, REG, REPO, TempDirCase, cr  # noqa: E402

CID = "pm.competitor-research"
PM_SKILLS = "2a12295a7717569baa7be181b4cfebe3ca38a6bb"
# Exactly the PM Skills files in the pinned, hash-verified method evidence bundle for this milestone.
VERIFIED = ["pm-market-research/commands/competitive-analysis.md", "pm-market-research/skills/competitor-analysis/SKILL.md"]
SKILL = REPO / "hermes/skills/pm-competitor-research"


class CompetitorResearchRecord(TempDirCase):
    def test_record_is_source_implemented_and_defined_only(self):
        c = cap(CID)
        self.assertEqual(c["lifecycle"], "source-implemented")
        for p in ("hermes/skills/pm-competitor-research/SKILL.md", "scripts/capability_workflow.py", "scripts/capability_research.py",
                  "hermes/capabilities/schemas/workflow-outputs.schema.json", "hermes/capabilities/schemas/evidence-pack.schema.json"):
            self.assertIn(p, c["implementation"]["paths"])
        r = cr.evaluate_readiness(c, repo=REPO)
        self.assertEqual(r["dimensions"]["defined"]["state"], "yes")
        for dim in ("installed", "tools_available", "authorized", "runtime_accepted", "executed", "outcome_verified"):
            self.assertEqual(r["dimensions"][dim]["state"], "unknown", dim)
        self.assertFalse(r["executable_now"])
        self.assertEqual(c["execution"]["writer"], "none")
        self.assertNotIn("source-modification", c["side_effects"])
        self.assertIn("external-research-if-private-context", [a["action"] for a in c["approvals"]])
        self.assertFalse(c["governance"]["auto_update"])
        self.assertIn("Validator and renderer: none", c["governance"]["network_behavior"])

    def test_every_source_implemented_record_covers_the_shared_helpers(self):
        for c in REG["capabilities"]:
            if c["lifecycle"] == "source-implemented":
                self.assertIn("scripts/capability_research.py", c["implementation"]["paths"], c["id"])
        self.assertEqual(cr.validate_registry(REG, LOCK, repo=REPO), [])

    def test_sources_are_exactly_the_verified_pinned_files_with_notice(self):
        c = cap(CID)
        lidx = cr.lock_index(LOCK)
        self.assertEqual(c["source"]["revision"], PM_SKILLS)
        self.assertEqual(sorted(c["source"]["paths"]), sorted(VERIFIED))
        for p in VERIFIED:
            self.assertEqual(lidx[("pm-skills", p)]["status"], "selected")
        self.assertEqual(c["source"]["content_digest"], cr.upstream_digest(lidx, "pm-skills", VERIFIED))
        notice = (SKILL / "references/pm-skills-license.md").read_text()
        self.assertIn("Copyright (c) 2026 Pawel Huryn", notice)
        self.assertIn("The above copyright notice and this permission notice shall be included", notice)
        self.assertIn(PM_SKILLS, notice)
        mm = (SKILL / "references/method-map.md").read_text()
        for p in VERIFIED:
            self.assertIn(p, notice)
            self.assertIn(p, mm)
        self.assertIn("market-segments", mm)  # dropped: not in the verified bundle

    def test_validator_modules_have_no_network_or_shell_imports(self):
        for name in ("capability_research.py", "capability_workflow.py"):
            tree = ast.parse((REPO / "scripts" / name).read_text())
            mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            for bad in ("socket", "urllib3", "requests", "http", "subprocess", "httpx", "aiohttp"):
                self.assertNotIn(bad, mods, (name, bad))
            self.assertNotIn("urlopen", (REPO / "scripts" / name).read_text())

    def test_skill_commands_run_as_documented_on_the_shipped_examples(self):
        text = (SKILL / "SKILL.md").read_text()
        block = re.search(r"```bash\n(.*?)```", text, re.S).group(1)
        cmds = [ln.split() for ln in block.strip().splitlines()]
        self.assertEqual(len(cmds), 3)
        snaps = self.tmp / "snapshots"
        snaps.mkdir()
        d = EVALS / "crew-scheduling-competitors"  # full captures: no snapshot binding, the snapshot dir is simply unused
        subst = {"$KIT/scripts/capability_workflow.py": "scripts/capability_workflow.py", "$ANALYSIS": str(d / "analysis-scan.json"),
                 "$EVIDENCE": str(d / "evidence-pack.json"), "$TODAY": "2026-11-04", "$SNAPSHOTS": str(snaps), "$OUT": str(self.tmp / "out")}
        for cmd in cmds:
            self.assertEqual(cmd[0], "python3")
            r = self.run_cli(*[subst.get(x, x) for x in cmd[1:]])
            self.assertEqual(r.returncode, 0, (cmd, r.stdout, r.stderr))
        self.assertTrue((self.tmp / "out" / "competitor-report.md").is_file())
        for forbidden in ("pip install", "curl ", "wget ", "git commit", "/competitive-analysis ", "$ARGUMENTS", "~/.claude"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
