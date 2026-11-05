"""Milestone B registry records: the three product capabilities are source-implemented.

Source-implemented means defined by a registry record whose implementation
digest matches the files in this checkout. It says nothing about installation,
runtime acceptance or execution: every other readiness dimension stays unknown.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_helpers import LOCK, REG, REPO, cr  # noqa: E402

MILESTONE_B = ("kit.problem-definition", "pm.product-discovery", "pm.lean-prd")


def cap(cid, reg=REG):
    return next(c for c in reg["capabilities"] if c["id"] == cid)


class SourceImplementedRecordsTest(unittest.TestCase):
    def test_each_milestone_b_capability_claims_its_skill_and_is_defined_only(self):
        for cid in MILESTONE_B:
            with self.subTest(cid=cid):
                c = cap(cid)
                self.assertEqual(c["lifecycle"], "source-implemented")
                skill = f"hermes/skills/{c['exposed_skill_name']}/SKILL.md"
                self.assertIn(skill, c["implementation"]["paths"])
                self.assertIn("scripts/capability_workflow.py", c["implementation"]["paths"])
                r = cr.evaluate_readiness(c, repo=REPO)
                self.assertEqual(r["dimensions"]["defined"]["state"], "yes")
                for dim in ("installed", "tools_available", "authorized", "runtime_accepted", "executed", "outcome_verified"):
                    self.assertEqual(r["dimensions"][dim]["state"], "unknown", dim)
                self.assertFalse(r["executable_now"])

    def test_every_kit_skill_file_is_claimed_by_some_record(self):
        claimed = {p for c in REG["capabilities"] for p in c["implementation"]["paths"]}
        for name, rel in cr.kit_skill_names(REPO).items():
            exposed = [c for c in REG["capabilities"] if c["exposed_skill_name"] == name]
            if exposed:
                with self.subTest(skill=name):
                    self.assertIn(rel, claimed)

    def test_editing_a_skill_after_the_record_makes_it_undefined(self):
        for cid in MILESTONE_B:
            with self.subTest(cid=cid):
                c = copy.deepcopy(cap(cid))
                c["implementation"]["digest"] = "0" * 64
                self.assertEqual(cr.evaluate_readiness(c, repo=REPO)["dimensions"]["defined"]["state"], "no")
                reg = copy.deepcopy(REG)
                cap(cid, reg)["implementation"]["digest"] = "0" * 64
                self.assertTrue(any("implementation digest mismatch" in e for e in cr.validate_registry(reg, LOCK, repo=REPO)))

    def test_adapted_records_cite_only_verified_selected_upstream_files(self):
        lidx = cr.lock_index(LOCK)
        for cid in ("pm.product-discovery", "pm.lean-prd"):
            c = cap(cid)
            with self.subTest(cid=cid):
                self.assertEqual(c["source"]["revision"], "2a12295a7717569baa7be181b4cfebe3ca38a6bb")
                for p in c["source"]["paths"]:
                    self.assertEqual(lidx[("pm-skills", p)]["status"], "selected")
                notice = REPO / f"hermes/skills/{c['exposed_skill_name']}/references/pm-skills-license.md"
                text = notice.read_text()
                self.assertIn("Copyright (c) 2026 Pawel Huryn", text)
                self.assertIn("The above copyright notice and this permission notice shall be included", text)
                self.assertIn("2a12295a7717569baa7be181b4cfebe3ca38a6bb", text)


if __name__ == "__main__":
    unittest.main()
