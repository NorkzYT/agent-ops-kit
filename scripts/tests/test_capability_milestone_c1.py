"""Milestone C1 registry records: gstack design consultation and design review are source-implemented
method adapters (no upstream runtime), citing only verified upstream files and carrying the MIT notice.
Source-implemented says nothing about installation, runtime acceptance or execution."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_milestone_b import cap  # noqa: E402
from workflow_helpers import LOCK, REG, REPO, cr  # noqa: E402

C1 = ("gstack.design-consultation", "gstack.design-review")
GSTACK = "622e8b1f76d293b647eca7b33d9dd060ab618b21"
# Exactly the gstack files in the pinned, hash-verified method evidence bundle for this milestone.
VERIFIED = {"gstack.design-consultation": ["design-consultation/SKILL.md", "design-consultation/sections/proposal-and-preview.md"],
            "gstack.design-review": ["design-review/SKILL.md", "design-review/sections/craft-audit.md"]}


class DesignRecordsTest(unittest.TestCase):
    def test_records_are_source_implemented_and_defined_only(self):
        for cid in C1:
            with self.subTest(cid=cid):
                c = cap(cid)
                self.assertEqual(c["lifecycle"], "source-implemented")
                for p in (f"hermes/skills/{c['exposed_skill_name']}/SKILL.md", "scripts/capability_workflow.py",
                          "scripts/capability_design.py", "hermes/capabilities/schemas/workflow-outputs.schema.json"):
                    self.assertIn(p, c["implementation"]["paths"])
                r = cr.evaluate_readiness(c, repo=REPO)
                self.assertEqual(r["dimensions"]["defined"]["state"], "yes")
                for dim in ("installed", "tools_available", "authorized", "runtime_accepted", "executed", "outcome_verified"):
                    self.assertEqual(r["dimensions"][dim]["state"], "unknown", dim)
                self.assertFalse(r["executable_now"])
                self.assertEqual(c["execution"]["writer"], "none")
                self.assertNotIn("source-modification", c["side_effects"])
                self.assertFalse(c["governance"]["auto_update"])

    def test_sources_are_exactly_the_verified_pinned_files(self):
        lidx = cr.lock_index(LOCK)
        for cid in C1:
            c = cap(cid)
            with self.subTest(cid=cid):
                self.assertEqual(c["source"]["revision"], GSTACK)
                self.assertEqual(sorted(c["source"]["paths"]), sorted(VERIFIED[cid]))
                for p in c["source"]["paths"]:
                    self.assertEqual(lidx[("gstack", p)]["status"], "selected")
                self.assertEqual(c["source"]["content_digest"], cr.upstream_digest(lidx, "gstack", c["source"]["paths"]))
                skill = REPO / "hermes/skills" / c["exposed_skill_name"]
                notice = (skill / "references/gstack-license.md").read_text()
                self.assertIn("Copyright (c) 2026 Garry Tan", notice)
                self.assertIn("The above copyright notice and this permission notice shall be included", notice)
                self.assertIn(GSTACK, notice)
                for p in VERIFIED[cid]:
                    self.assertIn(p, notice)
                    self.assertIn(p, (skill / "references/method-map.md").read_text())

    def test_upstream_runtime_behaviors_stay_disabled(self):
        for cid in C1:
            text = " ".join(cap(cid)["governance"]["disabled_upstream_behaviors"]).lower()
            for b in ("preamble", "update_check", "telemetry", "outside-model"):
                self.assertIn(b, text, cid)
        self.assertIn("web font", " ".join(cap("gstack.design-consultation")["governance"]["disabled_upstream_behaviors"]).lower())
        review = " ".join(cap("gstack.design-review")["governance"]["disabled_upstream_behaviors"]).lower()
        for b in ("source fix phase", "browser", "cookie"):
            self.assertIn(b, review)

    def test_skills_never_tell_the_agent_to_run_upstream_tooling(self):
        for cid in C1:
            text = (REPO / "hermes/skills" / cap(cid)["exposed_skill_name"] / "SKILL.md").read_text()
            for forbidden in ("gstack-config", "gstack-update", "~/.claude", "$B ", "aside repl", "git commit", "open \"$PREVIEW"):
                self.assertNotIn(forbidden, text, (cid, forbidden))

    def test_registry_still_validates(self):
        self.assertEqual(cr.validate_registry(REG, LOCK, repo=REPO), [])


if __name__ == "__main__":
    unittest.main()
