"""gstack.spec (C3): a backlog-ready local spec and issue draft built on the accepted lean PRD.

support-billing-spec reuses the accepted billing lean PRD: every acceptance criterion traces to a PRD
requirement ID (or, without a PRD, to a statement in the spec), is testable and is covered by the testing
plan. Filing is never done here: publication needs its own approval and a secret-shaped string refuses
the whole draft. A simple bug spec needs no PRD or discovery, only a root cause and a regression test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_discovery import DiscoveryCase, ctx, errors, section  # noqa: E402
from workflow_helpers import EVALS, REG, cr, mutate, wf  # noqa: E402

SCEN = EVALS / "support-billing-spec"


def ac(a, aid):
    return next(x for x in a["acceptance_criteria"] if x["id"] == aid)


class SpecFeature(DiscoveryCase):
    scenario, analysis = "support-billing-spec", "analysis-feature"

    def e(self, fn):
        return self.errs(fn)

    def test_renders_spec_and_local_issue_draft_traced_to_the_prd(self):
        out = self.render(self.a)
        spec = (out / "spec.md").read_text()
        self.assertIn("SYNTHETIC fixture data", spec)
        trace = section(spec, "Traceability")
        self.assertIn("`ac-offhours` → `r-route`, `f-route` (t-support-prd.lean-prd)", trace)
        self.assertIn("Out of scope", spec)
        self.assertIn("Billing reply macros (r-macros)", spec)
        self.assertIn("dep-rule ← dep-queue", section(spec, "Dependencies"))
        issue = (out / "issue-draft.md").read_text()
        for h in ("Context", "Current State", "Proposed Change", "Acceptance Criteria", "Testing Plan",
                  "Rules for the implementing agent", "Rollback Plan", "Effort Estimate"):
            self.assertIn(f"## {h}", issue)
        self.assertIn("Local draft: nothing was filed", issue)
        self.assertIn("- [ ] `ac-boundary` Given a ticket tagged billing is created on a weekday at exactly 17:00", issue)
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")  # the public-holiday question is open
        kinds = {x["location"]: x["kind"] for x in cr.load_json(out / "artifacts.json")["artifacts"]}
        self.assertEqual((kinds["spec.md"], kinds["issue-draft.md"]), ("spec", "spec"))

    def test_criteria_trace_to_accepted_requirements(self):
        self.assert_error(self.e(lambda b: ac(b, "ac-boundary").update(traces_to=["r-refunds"])),
                          r"ac-boundary traces to r-refunds, which is neither a requirement ID in a reused requirements artifact nor a statement")
        self.assert_error(self.e(lambda b: ac(b, "ac-boundary").update(traces_to=[])), r"ac-boundary traces to nothing")

    def test_header_statement_and_artifact_ids_are_not_requirements(self):
        # the reused PRD backticks its task/company/project, evidence pack, statement and reused-artifact IDs;
        # only the IDs it renders as requirement list items (features, release items, key results...) count
        for bogus in ("example-ops", "example-b", "t-support-prd", "pack-support-prd", "b-gap",
                      "t-support-billing.problem-brief", "synthetic"):
            self.assert_error(self.e(lambda b, x=bogus: ac(b, "ac-boundary").update(traces_to=[x])),
                              rf"ac-boundary traces to {re.escape(bogus)}, which is neither a requirement ID")
        for real in ("r-route", "f-rota", "kr-billing", "ng-auto"):
            self.assertEqual(self.e(lambda b, x=real: ac(b, "ac-boundary").update(traces_to=[x])), [])

    def test_criteria_are_testable_and_covered_by_the_plan(self):
        self.assert_error(self.e(lambda b: ac(b, "ac-daytime").update(then="the queue assignment is better")),
                          r"ac-daytime: 'then' is vague \('better'\)")
        self.assert_error(self.e(lambda b: b["testing_plan"][1].update(covers=["ac-offhours"])),
                          r"ac-boundary is not covered by the testing plan")
        self.assert_error(self.e(lambda b: b["testing_plan"][0]["covers"].append("ac-missing")), r"testing plan covers unknown criterion ac-missing")
        self.assert_error(self.e(lambda b: b["testing_plan"][1].update(level="e2e")),
                          r"ac-boundary is a unit criterion but no unit test in the plan covers it")

    def test_current_state_is_verified_or_labeled_unknown(self):
        self.assert_error(self.e(lambda b: b["current_state_statement_ids"].append("s-why")),
                          r"current state s-why is an assumption; state verified facts or label it unknown")

    def test_feature_spec_names_out_of_scope_and_rollback(self):
        self.assert_error(self.e(lambda b: b["scope"].update(out=[])), r"a feature spec names what is out of scope")
        self.assert_error(self.e(lambda b: b.update(rollback=None)), r"a feature spec needs a rollback plan")

    def test_dependencies_are_known_and_acyclic(self):
        self.assert_error(self.e(lambda b: b["dependencies"][0].update(depends_on=["dep-rule"])), r"dependency cycle")
        self.assert_error(self.e(lambda b: b["dependencies"][1].update(depends_on=["dep-x"])), r"dep-rule depends on unknown dep-x")

    def test_filing_is_local_and_publication_needs_its_own_approval(self):
        self.assert_error(self.e(lambda b: b["filing"].update(publish_requested=True)),
                          r"publishing the issue needs the publication approval")
        ok = mutate(self.a, lambda b: (b["filing"].update(publish_requested=True), b["next_action"]["approvals_needed"].append("publication")))
        self.assertEqual(errors(ok, self.pack, self.store), [])
        needs = wf.KINDS["spec"].needs_input(ok, ctx(self.pack, self.store))
        self.assertIn("approval needed before the next action: publication", needs)
        bad = mutate(self.a, lambda b: b["filing"].update(target="github"))
        self.assertTrue(errors(bad, self.pack, self.store))

    def test_secret_shaped_text_fails_the_redaction_gate(self):
        self.assert_error(self.e(lambda b: b["rules_for_implementer"].append("Use api_key=sk-live-4f9a8b7c6d5e4f3a2b1c for the sandbox.")),
                          r"redaction gate: rules_for_implementer/3 holds a secret-shaped string")
        self.assert_error(self.e(lambda b: b.update(proposed_change="Copy -----BEGIN RSA PRIVATE KEY----- into the rule.")),
                          r"redaction gate: proposed_change")


class SpecBug(DiscoveryCase):
    scenario, analysis = "support-billing-spec", "analysis-feature"

    def bug(self):
        def fn(b):
            b["reused_artifacts"] = []
            b["statements"] += [
                {"id": "s-bug", "class": "synthetic", "text": "A billing ticket created at 17:00 on Monday stayed unassigned for 15 hours.", "source_ids": ["S3"]},
                {"id": "s-h-edge", "class": "hypothesis", "text": "The Billing queue rule ends at 17:00 exclusive while no rule starts at 17:00, so the ticket matches neither.", "source_ids": ["S1", "S3"]}]
            b.update(title="Billing tickets created at exactly 17:00 stay unassigned", framing="bug", why_statement_ids=["s-bug"],
                     root_cause={"symptom_statement_ids": ["s-bug"], "cause_statement_ids": ["s-h-edge"]},
                     scope={"in": ["Boundary condition of the billing routing rule"], "out": []}, rollback=None, dependencies=[],
                     proposed_change="Make the 17:00 boundary belong to exactly one rule.", questions=[],
                     acceptance_criteria=[{"id": "ac-1700", "given": "a ticket tagged billing is created at 17:00 on a weekday", "when": "the routing rules run",
                                           "then": "the ticket is assigned to exactly 1 queue", "test_level": "unit", "traces_to": ["s-bug"], "regression": True}],
                     testing_plan=[{"level": "unit", "what": "Rule evaluation at 16:59, 17:00 and 17:01.", "covers": ["ac-1700"]}],
                     files=[{"path": "helpdesk/routing/billing.json", "change": "modify", "why": "Boundary."}])
        return mutate(self.a, fn)

    def test_simple_bug_needs_no_prd_or_discovery_only_cause_and_regression(self):
        b = self.bug()
        self.assertEqual(errors(b, self.pack, self.store), [])
        files, errs = wf.render_analysis(b, ctx(self.pack, self.store))
        self.assertEqual(errs, [])
        self.assertIn("Root cause (unconfirmed: hypothesis)", files["spec.md"])
        self.assertNotIn("t-support-prd.lean-prd", files["spec.md"])
        feature, _ = wf.render_analysis(self.a, ctx(self.pack, self.store))
        self.assertNotEqual(feature["issue-draft.md"], files["issue-draft.md"])
        self.assert_error(errors(mutate(b, lambda x: x["acceptance_criteria"][0].update(regression=False)), self.pack, self.store),
                          r"a bug spec needs a regression criterion")
        self.assert_error(errors(mutate(b, lambda x: x.update(root_cause=None)), self.pack, self.store), r"a bug spec needs a root cause")
        self.assert_error(errors(mutate(self.a, lambda x: x.update(root_cause=b["root_cause"])), self.pack, self.store),
                          r"only a bug spec carries a root cause")


class SpecInputs(DiscoveryCase):
    scenario, analysis = "support-billing-spec", "analysis-feature"

    def test_reused_prd_is_the_accepted_billing_chain_bytes(self):
        for name in ("artifacts.json", "lean-prd.md"):
            self.assertEqual((SCEN / "inputs" / name).read_bytes(), (EVALS / "billing-rota-design" / "inputs" / name).read_bytes())


if __name__ == "__main__":
    import unittest
    unittest.main()
