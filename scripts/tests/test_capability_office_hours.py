"""gstack.office-hours (C3): adapted startup diagnostic and builder session over accepted problem/discovery artifacts.

trial-office-hours reuses the accepted trial discovery brief and problem brief: the status-quo question is
covered by a verbatim quote of the discovery brief instead of being asked again, demand evidence is kept
apart from opinion, premises and the approach decision need the requester's recorded answer, and the
smart-routing table decides which forcing questions a stage needs (a builder session needs none).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_discovery import DiscoveryCase, ctx, errors, section  # noqa: E402
from workflow_helpers import EVALS, REG, cr, mutate, repack, wf  # noqa: E402

SCEN = EVALS / "trial-office-hours"


def fq(a, q):
    return next(x for x in a["forcing_questions"] if x["question"] == q)


class OfficeHoursStartup(DiscoveryCase):
    scenario, analysis = "trial-office-hours", "analysis-startup"

    def errs_of(self, a, pack=None):
        return errors(a, pack or self.pack, self.store)

    def test_renders_routed_questions_demand_premises_and_pending_approach(self):
        out = self.render(self.a)
        brief = (out / "opportunity-brief.md").read_text()
        self.assertIn("SYNTHETIC fixture data", brief)
        fqs = section(brief, "Forcing questions")
        self.assertIn("Stage has-users routes: q2-status-quo, q4-narrowest-wedge, q5-observation", fqs)
        self.assertRegex(fqs, r"q2-status-quo: covered by `t-trial-discovery.discovery-brief`: \"14 of 40 reviewed trial support chats")
        self.assertRegex(fqs, r"q5-observation: open")
        demand = section(brief, "Demand evidence")
        self.assertIn("`s-agency`", demand)
        self.assertIn("Opinion, not demand", demand)
        self.assertIn("`s-sales`", demand)
        prem = section(brief, "Premises")
        self.assertIn("`p-problem` agreed (H2)", prem)
        self.assertIn("`p-distribution` proposed: needs the requester", prem)
        rec = section(brief, "Recommended approach")
        self.assertIn("a-fake-door", rec)
        self.assertIn("not approved", rec)
        self.assertIn("minimal-viable", section(brief, "Approaches considered"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")
        pending = " ".join(result["unknowns"])
        self.assertIn("decision approach", pending)
        self.assertIn("premise p-distribution", pending)
        index = cr.load_json(out / "artifacts.json")
        brief_art = next(x for x in index["artifacts"] if x["location"] == "opportunity-brief.md")
        self.assertEqual(brief_art["kind"], "opportunity-brief")
        self.assertIn("t-trial-discovery.discovery-brief", [r["artifact_id"] for r in brief_art["source_refs"]])

    def test_stage_routing_decides_which_questions_are_required(self):
        bad = mutate(self.a, lambda b: b["forcing_questions"].remove(fq(b, "q4-narrowest-wedge")))
        self.assert_error(self.errs_of(bad), r"stage has-users routes q4-narrowest-wedge but the analysis does not address it")
        # pre-product needs Q1-Q3 instead
        bad = mutate(self.a, lambda b: b.update(product_stage="pre-product"))
        self.assert_error(self.errs_of(bad), r"stage pre-product routes q1-demand")
        # an unrouted question may be answered, but is never left open as a demand on the requester
        extra = mutate(self.a, lambda b: b["forcing_questions"].append({"question": "q6-future-fit", "status": "open", "topic": "future"}))
        self.assert_error(self.errs_of(extra), r"q6-future-fit is not routed for stage has-users; do not leave it open")

    def test_covered_questions_quote_a_reused_artifact_verbatim(self):
        bad = mutate(self.a, lambda b: fq(b, "q2-status-quo")["covered_by"].update(text="most trial chats ask how to start"))
        self.assert_error(self.errs_of(bad), r"q2-status-quo: quoted text is not in t-trial-discovery.discovery-brief")
        bad = mutate(self.a, lambda b: b.update(reused_artifacts=[r for r in b["reused_artifacts"] if r["kind"] != "discovery-brief"]))
        self.assert_error(self.errs_of(bad), r"q2-status-quo: t-trial-discovery.discovery-brief is not a reused artifact")

    def test_open_questions_are_asked_once_and_not_for_supplied_facts(self):
        bad = mutate(self.a, lambda b: b.update(questions=[]))
        self.assert_error(self.errs_of(bad), r"q5-observation is open but no question with topic watched-session is asked")
        bad = mutate(self.a, lambda b: (fq(b, "q5-observation").update(topic="wedge"),
                                        b["questions"][0].update(topic="wedge")))
        self.assert_error(self.errs_of(bad), r"asks for a fact the supplied evidence already covers")

    def test_interest_is_not_demand(self):
        bad = mutate(self.a, lambda b: b["demand"]["evidence_statement_ids"].append("s-sales"))
        self.assert_error(self.errs_of(bad), r"demand evidence s-sales is assumption, not observed behavior")
        bad = mutate(self.a, lambda b: next(s for s in b["statements"] if s["id"] == "s-rebuild").update(
            text="3 of 12 interviewed trial teams said they are interested in templates."))
        self.assert_error(self.errs_of(bad), r"demand evidence s-rebuild reads as interest \('interested'\), not demand")
        bad = mutate(self.a, lambda b: b["demand"].update(status="not-evidenced"))
        self.assert_error(self.errs_of(bad), r"demand is not-evidenced but lists evidence")
        bad = mutate(self.a, lambda b: b["demand"].update(status="evidenced", evidence_statement_ids=[]))
        self.assert_error(self.errs_of(bad), r"demand is evidenced but cites no behavior")

    def test_premises_and_the_approach_need_the_requesters_recorded_answer(self):
        bad = mutate(self.a, lambda b: b["premises"][2].update(status="agreed", source_id="H2"))
        self.assert_error(self.errs_of(bad), r"premise p-distribution: H2 does not record the line 'p-distribution: agree'")
        bad = mutate(self.a, lambda b: b["premises"][2].update(status="agreed"))
        self.assert_error(self.errs_of(bad), r"premise p-distribution is agreed but cites no recorded answer")
        answered = mutate(self.a, lambda b: b["decisions"][0].update(status="answered", answer={"option_id": "a-fake-door", "source_id": "H2"}))
        self.assert_error(self.errs_of(answered), r"decision approach: H2 does not record the line 'approach: a-fake-door'")
        # with the requester's recorded answer the decision settles and only the open premise/question remain
        b, pack = repack(self.tmp, SCEN, answered, lambda d: d["items"][1].update(
            excerpt=d["items"][1]["excerpt"] + "\napproach: a-fake-door\np-distribution: agree"))
        b["premises"][2].update(status="agreed", source_id="H2")
        self.assertEqual(self.errs_of(b, pack), [])
        c = ctx(pack, self.store)
        files, _ = wf.render_analysis(b, c)
        rec = section(files["opportunity-brief.md"], "Recommended approach")
        self.assertIn("requester answer recorded in H2", rec)
        self.assertIn("no trusted approval receipt", rec)
        self.assertNotIn("approved by the requester", rec)
        needs = wf.KINDS["office-hours"].needs_input(b, c)
        self.assertFalse(any("decision approach" in n for n in needs), needs)

    def test_recorded_answers_match_whole_option_ids_and_never_read_as_receipts(self):
        # Review 2026-11-05: 'approach: a' matched inside 'approach: a-fake-door' (unanchored substring).
        pack = wf.Pack(doc={"binding": {"data_classification": "synthetic"}}, digest="0" * 64, root=None, captures={},
                       items={"H9": {"source_id": "H9", "kind": "document", "classification": "synthetic",
                                     "excerpt": "Requester answers:\napproach: a-fake-door\np-x: agree"}})
        a = {"binding": {"data_classification": "synthetic"}}
        self.assertEqual(wf.recorded_answer("d", "H9", "approach", "a-fake-door", a, pack), [])
        self.assertTrue(wf.recorded_answer("d", "H9", "approach", "a", a, pack))
        self.assertTrue(wf.recorded_answer("d", "H9", "x", "agree", a, pack))

    def test_a_recorded_answer_is_a_whole_line_never_a_negated_or_embedded_mention(self):
        # Final review 2026-11-05: 'not approach: a-fake-door' counted as recording that answer.
        a = {"binding": {"data_classification": "synthetic"}}
        for excerpt in ("Requester: not approach: a-fake-door", "we rejected approach: a-fake-door",
                        "approach: a-fake-door is not what we want", "Requester: approach: a-fake-door.",
                        "APPROACH: A-FAKE-DOOR", "approach: a-fake-door, not really"):
            with self.subTest(excerpt=excerpt):
                pack = wf.Pack(doc={}, digest="0" * 64, root=None, captures={},
                               items={"H9": {"source_id": "H9", "kind": "document", "classification": "synthetic",
                                             "excerpt": "Requester answers:\n" + excerpt}})
                self.assertEqual(wf.recorded_answer("d", "H9", "approach", "a-fake-door", a, pack),
                                 ["d: H9 does not record the line 'approach: a-fake-door'"])
        ok = wf.Pack(doc={}, digest="0" * 64, root=None, captures={},
                     items={"H9": {"source_id": "H9", "kind": "document", "classification": "synthetic",
                                   "excerpt": "Requester answers:\n  approach: a-fake-door  \nmore notes"}})
        self.assertEqual(wf.recorded_answer("d", "H9", "approach", "a-fake-door", a, ok), [])

    def test_alternatives_include_minimal_and_ideal_and_the_decision_offers_them(self):
        bad = mutate(self.a, lambda b: b["alternatives"][1].update(role="lateral"))
        self.assert_error(self.errs_of(bad), r"alternatives need exactly one ideal-architecture approach")
        bad = mutate(self.a, lambda b: b["decisions"][0]["options"].pop())
        self.assert_error(self.errs_of(bad), r"decision approach must offer exactly the alternatives")
        bad = mutate(self.a, lambda b: b["recommendation"].update(alternative_id="a-import"))
        self.assert_error(self.errs_of(bad), r"recommendation a-import differs from decision approach's recommendation a-fake-door")

    def test_success_criteria_are_measurable(self):
        bad = mutate(self.a, lambda b: b["success_criteria"][0].update(metric="Onboarding feels easier"))
        self.assert_error(self.errs_of(bad), r"success criterion sc-clicks is vague \('easier'\)")


class OfficeHoursBuilder(DiscoveryCase):
    scenario, analysis = "trial-office-hours", "analysis-startup"

    def builder(self):
        def fn(b):
            b.update(mode="builder", product_stage=None, forcing_questions=[], demand=None, questions=[])
        return mutate(self.a, fn)

    def test_builder_session_does_not_demand_the_startup_diagnostic(self):
        b = self.builder()
        self.assertEqual(errors(b, self.pack, self.store), [])
        files, errs = wf.render_analysis(b, ctx(self.pack, self.store))
        self.assertEqual(errs, [])
        brief = files["opportunity-brief.md"]
        self.assertIn("Builder session: the startup diagnostic is not required", brief)
        self.assertNotIn("Stage has-users routes", brief)
        startup, _ = wf.render_analysis(self.a, ctx(self.pack, self.store))
        self.assertNotEqual(startup["opportunity-brief.md"], brief)

    def test_builder_mode_refuses_startup_fields_and_startup_needs_a_stage(self):
        bad = mutate(self.builder(), lambda b: b.update(forcing_questions=self.a["forcing_questions"]))
        self.assert_error(errors(bad, self.pack, self.store), r"builder mode does not run the forcing questions")
        bad = mutate(self.a, lambda b: b.update(product_stage=None))
        self.assert_error(errors(bad, self.pack, self.store), r"startup mode needs a product stage")


class OfficeHoursInputs(DiscoveryCase):
    scenario, analysis = "trial-office-hours", "analysis-startup"

    def test_reused_inputs_are_the_accepted_trial_chain_bytes(self):
        for name in ("artifacts.json", "discovery-brief.md", "input-1-problem-brief.md"):
            self.assertEqual((SCEN / "inputs" / name).read_bytes(),
                             (EVALS / "trial-activation-prd" / "inputs" / name).read_bytes(), name)


if __name__ == "__main__":
    import unittest
    unittest.main()
