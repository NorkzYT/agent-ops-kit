"""Milestone C3 registry records: the six remaining gstack method adapters (office-hours, spec,
plan-eng-review, plan-design-review, review, qa) are source-implemented, cite only the pinned,
hash-verified upstream files and carry the MIT notice. With C1's design consultation and design
review, all eight requested gstack methods are adapters; none is installed, executed or accepted.
Planning stays minimal: a bug is not sent through office hours, a spec reuses the accepted PRD and
plan reviews take current artifacts without discovery stages."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_milestone_b import cap  # noqa: E402
from test_capability_plan import artifact, envelope, index, plan, selection, stage, stage_ids  # noqa: E402
from workflow_helpers import LOCK, REG, REPO, cr  # noqa: E402

GSTACK = "622e8b1f76d293b647eca7b33d9dd060ab618b21"
VERIFIED = {
    "gstack.office-hours": ["office-hours/SKILL.md", "office-hours/sections/design-and-handoff.md",
                            "office-hours/sections/phase-2a-startup-diagnostic.md"],
    "gstack.spec": ["spec/SKILL.md", "spec/sections/gate-and-file.md"],
    "gstack.plan-eng-review": ["plan-eng-review/SKILL.md", "plan-eng-review/sections/review-sections.md"],
    "gstack.plan-design-review": ["plan-design-review/SKILL.md", "plan-design-review/sections/review-sections.md"],
    "gstack.review": ["review/SKILL.md", "review/checklist.md"],
    "gstack.qa": ["qa/SKILL.md", "qa/sections/qa-patterns.md", "qa/sections/system-functional.md"],
}
EIGHT = list(VERIFIED) + ["gstack.design-consultation", "gstack.design-review"]


class C3Records(unittest.TestCase):
    def test_all_eight_requested_gstack_methods_are_source_implemented_and_defined_only(self):
        for cid in EIGHT:
            with self.subTest(cid=cid):
                c = cap(cid)
                self.assertEqual(c["lifecycle"], "source-implemented")
                for p in (f"hermes/skills/{c['exposed_skill_name']}/SKILL.md", "scripts/capability_workflow.py",
                          "scripts/capability_methods.py", "hermes/capabilities/schemas/workflow-outputs.schema.json"):
                    self.assertIn(p, c["implementation"]["paths"])
                r = cr.evaluate_readiness(c, repo=REPO)
                self.assertEqual(r["dimensions"]["defined"]["state"], "yes")
                for dim in ("installed", "learned_current", "tools_available", "authorized", "runtime_accepted", "executed", "outcome_verified"):
                    self.assertNotEqual(r["dimensions"][dim]["state"], "yes", dim)
                self.assertFalse(r["executable_now"])
                self.assertEqual(c["execution"]["writer"], "none")
                self.assertNotIn("source-modification", c["side_effects"])
                self.assertFalse(c["governance"]["auto_update"])
        self.assertNotIn("gstack.ship", {c["id"] for c in REG["capabilities"]})

    def test_sources_are_exactly_the_verified_pinned_files_with_the_mit_notice(self):
        lidx = cr.lock_index(LOCK)
        for cid, files in VERIFIED.items():
            with self.subTest(cid=cid):
                c = cap(cid)
                self.assertEqual(c["source"]["revision"], GSTACK)
                self.assertEqual(sorted(c["source"]["paths"]), sorted(files))
                for p in files:
                    self.assertEqual(lidx[("gstack", p)]["status"], "selected")
                self.assertEqual(c["source"]["content_digest"], cr.upstream_digest(lidx, "gstack", files))
                skill = REPO / "hermes/skills" / c["exposed_skill_name"]
                notice = (skill / "references/gstack-license.md").read_text()
                self.assertIn("Copyright (c) 2026 Garry Tan", notice)
                self.assertIn("The above copyright notice and this permission notice shall be included", notice)
                self.assertIn(GSTACK, notice)
                mm = (skill / "references/method-map.md").read_text()
                for p in files:
                    self.assertIn(p, notice)
                    self.assertIn(p, mm)

    def test_upstream_runtime_side_effects_stay_disabled(self):
        for cid in VERIFIED:
            text = " ".join(cap(cid)["governance"]["disabled_upstream_behaviors"]).lower()
            for b in ("preamble", "update_check", "telemetry", "outside", "co-author"):
                self.assertIn(b, text, (cid, b))
        self.assertIn("issue filing", " ".join(cap("gstack.spec")["governance"]["disabled_upstream_behaviors"]).lower())
        self.assertIn("fix-first", " ".join(cap("gstack.review")["governance"]["disabled_upstream_behaviors"]).lower())
        qa = " ".join(cap("gstack.qa")["governance"]["disabled_upstream_behaviors"]).lower()
        for b in ("fix loop", "cookie", "browser"):
            self.assertIn(b, qa)
        self.assertIn("mockup", " ".join(cap("gstack.plan-design-review")["governance"]["disabled_upstream_behaviors"]).lower())

    def test_skills_never_tell_the_agent_to_run_upstream_tooling_or_write(self):
        for cid in VERIFIED:
            text = (REPO / "hermes/skills" / cap(cid)["exposed_skill_name"] / "SKILL.md").read_text()
            for forbidden in ("gstack-config", "gstack-update", "~/.claude", "$B ", "aside repl", "git commit", "git push",
                              "gh issue create", "Co-Authored-By", "codex"):
                self.assertNotIn(forbidden, text, (cid, forbidden))

    def test_registry_still_validates(self):
        self.assertEqual(cr.validate_registry(REG, LOCK, repo=REPO), [])


class C3Planning(unittest.TestCase):
    """ROU-01 for the C3 adapters: smallest plans, artifact reuse, no extra planning stages."""

    def test_simple_bug_review_adds_no_office_hours_spec_or_prd(self):
        change = artifact("a-diff", "code-change-handoff", "diff --git a/x b/x")
        p = plan(selection("existing.engineering-path", "gstack.review"), arts=index(change),
                 env=envelope(input_artifacts=[{"artifact_id": "a-diff", "digest": change["content_digest"]}]))
        self.assertEqual(sorted(stage_ids(p)), ["existing.engineering-path", "gstack.review"])
        self.assertEqual(stage(p, "gstack.review")["execution"]["writer"], "none")

    def test_spec_reuses_the_accepted_prd_and_plan_review_follows_without_discovery(self):
        prd = artifact("a-prd", "lean-prd", "accepted PRD")
        p = plan(selection("gstack.spec", "gstack.plan-eng-review"), arts=index(prd),
                 env=envelope(input_artifacts=[{"artifact_id": "a-prd", "digest": prd["content_digest"]}]))
        self.assertEqual(stage_ids(p), ["gstack.spec", "gstack.plan-eng-review"])
        req = next(i for i in stage(p, "gstack.spec")["inputs"] if i["name"] == "requirements")
        self.assertEqual(req["satisfied_by"]["artifact_id"], "a-prd")
        for absent in ("kit.problem-definition", "pm.product-discovery", "gstack.office-hours"):
            self.assertNotIn(absent, stage_ids(p))

    def test_office_hours_reuses_an_accepted_discovery_brief(self):
        brief = artifact("a-disc", "discovery-brief", "accepted discovery")
        p = plan(selection("gstack.office-hours"), arts=index(brief),
                 env=envelope(input_artifacts=[{"artifact_id": "a-disc", "digest": brief["content_digest"]}]))
        self.assertEqual(stage_ids(p), ["gstack.office-hours"])
        prob = next(i for i in stage(p, "gstack.office-hours")["inputs"] if i["name"] == "problem")
        self.assertEqual(prob["satisfied_by"]["artifact_id"], "a-disc")

    def test_c3_stages_are_no_longer_blocked_as_unimplemented(self):
        for cid in VERIFIED:
            p = plan(selection(cid))
            self.assertFalse(any("not implemented" in b for s in p["stages"] for b in s["blockers"]), cid)


if __name__ == "__main__":
    unittest.main()
