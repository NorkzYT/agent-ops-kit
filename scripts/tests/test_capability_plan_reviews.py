"""gstack.plan-eng-review and gstack.plan-design-review (C3): reviews of current plan artifacts.

support-billing-eng-review reviews the spec rendered by support-billing-spec; trial-guide-plan-review
reviews the trial lean PRD, design brief and design package. Both re-read the artifacts by digest, quote
them verbatim, run every section/pass (none skipped), calibrate confidence, and record decisions that
only a recorded requester answer settles. A review never asks discovery questions again and approves
no remedy.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_discovery import DiscoveryCase, ctx, errors, section  # noqa: E402
from workflow_helpers import EVALS, REG, cr, mutate, repack, wf  # noqa: E402


def fnd(a, fid):
    return next(f for f in a["findings"] if f["id"] == fid)


class EngReview(DiscoveryCase):
    scenario, analysis = "support-billing-eng-review", "analysis-review"

    def test_renders_all_sections_calibrated_findings_failure_modes_and_test_plan(self):
        out = self.render(self.a)
        rep = (out / "eng-review.md").read_text()
        self.assertIn("SYNTHETIC fixture data", rep)
        scope = section(rep, "Scope challenge")
        self.assertIn("3 files, 0 new services: complexity gate clear", scope)
        self.assertIn("reuse: Europe/Berlin business-hours calendar", scope)
        for sec in ("1. Architecture", "2. Code quality", "3. Tests", "4. Performance"):
            self.assertIn(f"## {sec}", rep)
        self.assertIn("No issues found", section(rep, "2. Code quality"))
        self.assertIn("[CRITICAL] (confidence: 8/10) `f-unstaffed`", section(rep, "1. Architecture"))
        self.assertIn("Medium confidence, verify this is actually an issue", section(rep, "3. Tests"))
        self.assertNotIn("f-holiday-load", section(rep, "4. Performance"))
        self.assertIn("f-holiday-load", section(rep, "Suppressed findings"))
        self.assertIn("`fm-unstaffed`", section(rep, "Failure modes"))
        self.assertIn("CRITICAL GAP", section(rep, "Failure modes"))
        self.assertIn("Approval readiness: PENDING (d-fallback)", rep)
        self.assertIn("Review agreement is not approval", rep)
        plan = (out / "test-plan.md").read_text()
        for h in ("Critical Paths", "Edge Cases", "Regression", "Pending Decisions"):
            self.assertIn(f"## {h}", plan)
        self.assertIn("`cp-unstaffed`", plan)
        self.assertIn("d-fallback", section(plan, "Pending Decisions"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")
        kinds = {x["location"]: x["kind"] for x in cr.load_json(out / "artifacts.json")["artifacts"]}
        self.assertEqual((kinds["eng-review.md"], kinds["test-plan.md"]), ("eng-review", "eng-review"))

    def test_every_section_is_evaluated_and_consistent(self):
        self.assertTrue(self.errs(lambda b: b["sections"].pop("performance")))
        self.assert_error(self.errs(lambda b: b["sections"]["architecture"].update(status="no-issues")),
                          r"section architecture says no-issues but has findings f-unstaffed, f-retag")
        self.assert_error(self.errs(lambda b: b["sections"]["code-quality"].update(status="findings")),
                          r"section code-quality says findings but has none")

    def test_confidence_is_calibrated_to_quoted_evidence(self):
        self.assert_error(self.errs(lambda b: fnd(b, "f-dst").update(confidence=8)),
                          r"f-dst is unverified \(no verbatim quote or capture\): confidence must be 5 or lower")
        self.assert_error(self.errs(lambda b: fnd(b, "f-retag")["evidence"][0].update(text="when the rules are re-run")),
                          r"f-retag: quoted text is not in t-support-spec.spec")
        self.assert_error(self.errs(lambda b: fnd(b, "f-holiday-load").update(confidence=2)),
                          r"f-holiday-load: confidence 1-2 is only for a speculative critical finding")

    def test_silent_untested_unhandled_failure_is_a_critical_gap(self):
        self.assert_error(self.errs(lambda b: fnd(b, "f-unstaffed").update(severity="informational")),
                          r"critical gap fm-unstaffed .* needs a critical finding")
        self.assert_error(self.errs(lambda b: b["test_plan"].update(critical_paths=b["test_plan"]["critical_paths"][:1] + b["test_plan"]["critical_paths"][2:])),
                          r"failure mode fm-unstaffed has no test, planned test or recorded acceptance")
        self.assert_error(self.errs(lambda b: b["test_plan"]["critical_paths"][0]["covers"].append("ac-nothing")),
                          r"cp-offhours covers ac-nothing, which is not a failure mode, finding or ID in the reviewed artifacts")

    def test_critical_paths_cannot_cover_header_statement_or_artifact_ids(self):
        for bogus in ("example-ops", "t-support-spec", "pack-support-spec", "s-hours", "t-support-prd.lean-prd"):
            self.assert_error(self.errs(lambda b, x=bogus: b["test_plan"]["critical_paths"][0]["covers"].append(x)),
                              rf"cp-offhours covers {re.escape(bogus)}, which is not a failure mode, finding or ID in the reviewed artifacts")
        self.assertEqual(self.errs(lambda b: b["test_plan"]["critical_paths"][0]["covers"].append("ac-boundary")), [])

    def test_findings_approve_no_remedy_until_the_requester_answers(self):
        accepted = mutate(self.a, lambda b: fnd(b, "f-unstaffed").update(disposition="accepted"))
        self.assert_error(errors(accepted, self.pack, self.store), r"f-unstaffed is accepted but decision d-fallback is pending")
        self.assert_error(self.errs(lambda b: fnd(b, "f-retag").update(disposition="accepted")),
                          r"f-retag is accepted without a decision")
        answered = mutate(accepted, lambda b: b["decisions"][0].update(status="answered", answer={"option_id": "fallback-billing", "source_id": "P1"}))
        b, pack = repack(self.tmp, EVALS / self.scenario, answered, lambda d: d["items"][0].update(
            excerpt=d["items"][0]["excerpt"] + "\nSupport lead, 2026-11-05:\nd-fallback: fallback-billing"))
        self.assertEqual(errors(b, pack, self.store), [])
        files, _ = wf.render_analysis(b, ctx(pack, self.store))
        # Answers are pack excerpts, not a trusted receipt: readiness is reported unverified (review 2026-11-05).
        self.assertIn("Approval readiness: ANSWERS RECORDED, NOT RECEIPT-VERIFIED (f-unstaffed via d-fallback)", files["eng-review.md"])
        self.assertNotIn("Approval readiness: PASS", files["eng-review.md"])

    def test_complexity_gate_needs_a_structure_decision(self):
        self.assert_error(self.errs(lambda b: b["scope_challenge"].update(files_touched=9)),
                          r"complexity gate tripped \(9 files, 0 new services\): record a structure decision")
        self.assert_error(self.errs(lambda b: b["scope_challenge"]["count_basis"].update(text="Twelve files in four services.")),
                          r"scope count basis: quoted text is not in t-support-spec.spec")

    def test_plan_review_takes_current_artifacts_and_asks_no_discovery_questions(self):
        self.assertTrue(self.errs(lambda b: b.update(questions=[{"topic": "users", "text": "Who are the users?"}])))
        self.assert_error(self.errs(lambda b: b["reviewed_artifacts"][0].update(digest="0" * 64)),
                          r"reviewed artifact t-support-spec.spec: kind/digest differs from the index")
        self.assert_error(self.errs(lambda b: b["reviewed_artifacts"][0].update(artifact_id="pack-support-spec", kind="evidence-index",
                                                                                digest=self.store.get("pack-support-spec")["content_digest"])),
                          r"pack-support-spec is a evidence-index, not a plan")


class DesignPlanReview(DiscoveryCase):
    scenario, analysis = "trial-guide-plan-review", "analysis-review"

    def test_renders_rating_seven_passes_state_table_and_journey(self):
        out = self.render(self.a)
        rep = (out / "design-plan-review.md").read_text()
        self.assertIn("Initial design completeness: 7/10", rep)
        passes = section(rep, "Passes")
        for p, r in (("information-architecture", 9), ("interaction-states", 5), ("journey", 6), ("ai-slop", 10),
                     ("design-system", 10), ("responsive-accessibility", 8), ("unresolved-decisions", 6)):
            self.assertIn(f"{p}: {r}/10", passes)
        table = section(rep, "Interaction state coverage")
        self.assertIn("| Setup guide | Loading your workspace.", table)
        self.assertIn("MISSING", table)
        self.assertIn("n/a (not access-controlled)", table)
        self.assertIn("Leaves the guide and later reopens it from Help | confident it remembered | NOT SPECIFIED", section(rep, "Journey"))
        self.assertIn("present: `t-trial-design.design-package`", rep)
        self.assertIn("Mockups: not generated", rep)
        self.assertIn("d-resume", section(rep, "Unresolved decisions"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")

    def test_ratings_need_gaps_and_ten_means_no_findings(self):
        self.assert_error(self.errs(lambda b: b["passes"]["journey"].pop("gap")), r"pass journey rated 6/10 names no gap")
        self.assert_error(self.errs(lambda b: b["passes"]["interaction-states"].update(rating=10)),
                          r"pass interaction-states rated 10/10 but has findings")
        self.assertTrue(self.errs(lambda b: b["passes"].pop("ai-slop")))

    def test_missing_states_and_unspecified_journey_steps_cannot_score_ten(self):
        def move(b):
            for f in b["findings"]:
                if f["section"] == "interaction-states":
                    f["section"] = "unresolved-decisions"
            b["passes"]["interaction-states"] = {"rating": 10}
        self.assert_error(self.errs(move), r"interaction states missing \(Setup guide: partial, permission_denied")
        self.assert_error(self.errs(lambda b: b["passes"].update(journey={"rating": 10})),
                          r"journey step 'Leaves the guide and later reopens it from Help' is not specified by the plan")
        self.assert_error(self.errs(lambda b: b["journey"][0]["plan_specifies"].update(text="A friendly mascot greets you.")),
                          r"journey step 'Lands on the empty workspace with the guide open': quoted text is not in")

    def test_slop_hits_cap_the_ai_slop_pass(self):
        hit = {"rule": "generic three-card hero", "evidence": {"artifact_id": "t-trial-design.design-brief", "text": "Aesthetic brutally-minimal"}, "resolved": False}
        self.assert_error(self.errs(lambda b: b["slop_hits"].append(hit)), r"unresolved AI-slop hit caps the ai-slop pass below 8")

    def test_mockups_and_design_system_claims(self):
        self.assert_error(self.errs(lambda b: b.update(mockups="requested")), r"mockup generation is paid: it needs the spend approval")
        ok = mutate(self.a, lambda b: (b.update(mockups="requested"), b["next_action"]["approvals_needed"].append("spend")))
        self.assertEqual(errors(ok, self.pack, self.store), [])
        self.assert_error(self.errs(lambda b: b["design_system"].update(artifact_id="t-trial-prd.lean-prd")),
                          r"design system t-trial-prd.lean-prd is not a reviewed design package or design brief")
        self.assert_error(self.errs(lambda b: b.update(reviewed_artifacts=[b["reviewed_artifacts"][2]], design_system={"status": "absent"})),
                          r"quoted text is not in|is not a reviewed artifact")

    def test_the_two_plan_reviews_differ_because_their_inputs_differ(self):
        eng, ep, es = __import__("test_capability_discovery").load("support-billing-eng-review", "analysis-review")
        e_files, errs = wf.render_analysis(eng, ctx(ep, es))
        self.assertEqual(errs, [])
        d_files, errs = wf.render_analysis(self.a, ctx(self.pack, self.store))
        self.assertEqual(errs, [])
        self.assertIn("Off-hours billing", e_files["eng-review.md"])
        self.assertNotIn("Off-hours billing", d_files["design-plan-review.md"])


class PlanReviewInputs(DiscoveryCase):
    scenario, analysis = "support-billing-eng-review", "analysis-review"

    def test_reviewed_spec_bytes_equal_a_fresh_spec_render(self):
        a, pack, store = __import__("test_capability_discovery").load("support-billing-spec", "analysis-feature")
        files, errs = wf.render_analysis(a, ctx(pack, store))
        self.assertEqual(errs, [])
        for name in ("spec.md", "issue-draft.md"):
            self.assertEqual((EVALS / self.scenario / "inputs" / name).read_text(), files[name])
        for name in ("design-brief.md", "design-package.md", "artifacts.json"):
            self.assertEqual((EVALS / "trial-guide-plan-review" / "inputs" / name).read_bytes(),
                             (EVALS / "trial-guide-review" / "inputs" / name).read_bytes())


if __name__ == "__main__":
    import unittest
    unittest.main()
