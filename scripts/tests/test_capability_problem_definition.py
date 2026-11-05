"""PD-01..PD-05: the original problem-definition workflow on varying synthetic scenarios.

Analyses are coordinator-authored synthetic demos (labeled so in every artifact);
these tests exercise the contract validator, the renderer and the documented
skill commands. Mutations show each rubric rule rejects a concrete defect.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_helpers import EVALS, REG, REPO, TempDirCase, cr, errors, mutate, scenario, statement, wf  # noqa: E402


def section(md: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}[^\n]*\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    return m.group(1) if m else ""


class PD01VagueProblemToDecision(TempDirCase):
    def setUp(self):
        super().setUp()
        self.a, self.pack = scenario("support-billing-backlog", "analysis-quick")

    def test_valid_quick_brief_renders_decision_user_evidence_alternatives_measure_and_next_test(self):
        out = self.render(self.a, self.pack)
        md = (out / "decision-brief.md").read_text()
        self.assertIn("SYNTHETIC fixture data", md)
        self.assertIn("Should the support lead change billing ticket coverage", section(md, "Decision"))
        self.assertIn("Customers with billing questions", section(md, "Problem"))
        self.assertIn("**synthetic** `s-staff`", section(md, "Evidence"))
        self.assertIn("**hypothesis** `s-h-coverage`", section(md, "Assumptions, hypotheses and estimates"))
        self.assertNotIn("hypothesis", section(md, "Evidence"))
        self.assertEqual(len(re.findall(r"^- `alt-", section(md, "Alternatives"), re.M)), 3)
        self.assertIn("target <= 12 hours within 28 days", section(md, "Success measure"))
        self.assertIn("(measure, cheap, owner support lead)", section(md, "Next action"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")  # one material question remains
        self.assertTrue(all(c["classification"] != "observed" for c in result["claims"]))
        index = cr.load_json(out / "artifacts.json")
        self.assertEqual(cr.schema_errors(index, "artifact.schema.json", "idx"), [])
        self.assertEqual(cr.verify_artifact_contents(index, out), [])
        self.assertEqual({x["review_status"] for x in index["artifacts"]}, {"draft"})

    def test_known_code_bug_is_sent_to_the_engineering_path(self):
        self.assert_error(errors(mutate(self.a, lambda b: b.update(problem_type="code-defect")), self.pack),
                          "existing.engineering-path")

    def test_invented_number_is_rejected(self):
        bad = mutate(self.a, lambda b: statement(b, "s-symptom").update(
            text="Billing tickets had a median first response of 40 hours versus 9 hours for other tickets in October 2026."))
        self.assert_error(errors(bad, self.pack), r"s-symptom: number 40 is not in its cited sources")

    def test_question_for_a_fact_the_evidence_holds_is_rejected(self):
        bad = mutate(self.a, lambda b: b["questions"].append({"topic": "staffing", "text": "How many agents know billing?"}))
        self.assert_error(errors(bad, self.pack), "staffing.*already covers")

    def test_contacting_customers_needs_the_outreach_approval(self):
        bad = mutate(self.a, lambda b: b["next_action"].update(involves_external_contact=True))
        self.assert_error(errors(bad, self.pack), "outreach approval")

    def test_fixture_fact_cannot_be_labeled_observed(self):
        bad = mutate(self.a, lambda b: statement(b, "s-staff").update({"class": "observed"}))
        errs = errors(bad, self.pack)
        self.assert_error(errs, "synthetic task has no observed facts")
        self.assert_error(errs, "cites synthetic data")

    def test_input_fields_cannot_approve_or_grant(self):
        for field, value in (("approved", True), ("review_status", "accepted"), ("permissions_granted", ["shell"])):
            with self.subTest(field=field):
                self.assert_error(errors(mutate(self.a, lambda b: b.update({field: value})), self.pack), "Additional properties")

    def test_analysis_must_cite_the_exact_supplied_pack(self):
        bad = mutate(self.a, lambda b: b["evidence"].update(digest="0" * 64))
        self.assert_error(errors(bad, self.pack), "does not match the supplied pack")
        bad = mutate(self.a, lambda b: b["binding"].update(company_id="example-a"))
        self.assert_error(errors(bad, self.pack), "binding")


class PD02CausalDiscipline(TempDirCase):
    def setUp(self):
        super().setUp()
        self.a, self.pack = scenario("trial-activation-causes", "analysis-causal")

    def node(self, b, nid):
        return next(n for n in b["why_tree"]["nodes"] if n["id"] == nid)

    def test_branched_tree_with_interaction_caveat_and_experiment_plan_passes(self):
        out = self.render(self.a, self.pack)
        md = (out / "problem-brief.md").read_text()
        tree = section(md, "Why tree (branches allowed; no fixed depth)")
        self.assertEqual(len(re.findall(r"^- `w-", tree, re.M)), 3)  # three root branches, one an explicit unknown
        self.assertIn("[unknown-link]", tree)
        self.assertIn("compound", section(md, "Partition"))
        self.assertIn("randomized", section(md, "Next action"))
        self.assertNotIn("experiment-supported", md)

    def test_correlation_cannot_be_promoted_to_experimental_proof(self):
        bad = mutate(self.a, lambda b: self.node(b, "w-email").update(edge="experiment-supported"))
        self.assert_error(errors(bad, self.pack), "no cited source is an experiment")

    def test_missing_cause_is_not_a_fact(self):
        bad = mutate(self.a, lambda b: self.node(b, "w-separate").update(edge="observed-association"))
        self.assert_error(errors(bad, self.pack), "unknown cause is labeled observed-association")

    def test_forced_linear_five_whys_with_invented_links_is_rejected(self):
        def linearize(b):
            b["statements"] += [{"id": f"t-why{i}", "text": f"Assumed reason level {i}.", "class": "assumption", "source_ids": []}
                                for i in range(2, 6)]
            b["why_tree"]["nodes"] = [{"id": "z1", "parent": "root", "statement_id": "t-email", "edge": "observed-association",
                                       "counter_statement_ids": []}] + [
                {"id": f"z{i}", "parent": f"z{i - 1}", "statement_id": f"t-why{i}", "edge": "observed-association",
                 "counter_statement_ids": []} for i in range(2, 6)]
        errs = errors(mutate(self.a, linearize), self.pack)
        self.assertEqual(len([e for e in errs if "observed-association needs observed/synthetic statements" in e]), 4)

    def test_branching_causes_need_an_interaction_assessment(self):
        bad = mutate(self.a, lambda b: b["partition"].pop("interaction_assessment"))
        self.assert_error(errors(bad, self.pack), "interaction_assessment")

    def test_causal_hypothesis_needs_a_falsification_test(self):
        bad = mutate(self.a, lambda b: self.node(b, "w-email-why").pop("test"))
        self.assert_error(errors(bad, self.pack), "needs a falsification test")

    def test_arithmetic_is_executed_not_estimated(self):
        def wrong(b):
            next(c for c in b["computations"] if c["id"] == "k-not-delayed")["result"] = 38.0
        self.assert_error(errors(mutate(self.a, wrong), self.pack), r"k-not-delayed: stated result 38.0 but executing .* gives 37.6")

    def test_expressions_cannot_call_or_import(self):
        def evil(b):
            b["computations"][0]["expression"] = "__import__(os).system(id)"
        errs = errors(mutate(self.a, evil), self.pack)
        self.assert_error(errs, "expression")


class MalformedInputIsAStructuredFailure(TempDirCase):
    """Review 2026-11-05 (wor11): overflow/recursion in arithmetic, a missing snapshot directory and other
    malformed input crashed with a traceback instead of a structured error."""

    def test_safe_eval_turns_every_arithmetic_failure_into_a_workflow_error(self):
        for expr in ("1" + "0" * 400, "-" * 50000 + "1", "(" * 300 + "1" + ")" * 300, "1e308*10", "1e308*10-1e308*10",
                     "1\x00", "a / b"):
            with self.subTest(expr=expr[:20]):
                with self.assertRaises(wf.WorkflowError):
                    wf.safe_eval(expr, {"a": 1.0, "b": 0.0})

    def _cli_case(self, value):
        d = EVALS / "trial-activation-causes"
        pk = json.loads((d / "evidence-pack.json").read_text())
        item = next(x for x in pk["items"] if x["source_id"] == "D1")
        item["values"]["activated"] = value
        raw = (json.dumps(pk) + "\n").encode()
        (self.tmp / "pack.json").write_bytes(raw)
        a = json.loads((d / "analysis-causal.json").read_text())
        import hashlib
        a["evidence"]["digest"] = hashlib.sha256(raw).hexdigest()
        (self.tmp / "a.json").write_text(json.dumps(a))
        return ["validate", "--analysis", self.tmp / "a.json", "--evidence", self.tmp / "pack.json"]

    def test_huge_source_value_is_reported_not_raised(self):
        args = self._cli_case(10 ** 400)
        p = self.run_cli(REPO / "scripts/capability_workflow.py", *args)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(p.returncode, (1, 2), p.stdout + p.stderr)
        self.assertRegex(p.stdout + p.stderr, r"k-overall|not a finite number|too large|longer than 18 digits")

    def test_missing_snapshot_dir_is_a_clean_cli_error(self):
        args = self._cli_case(310)
        p = self.run_cli(REPO / "scripts/capability_workflow.py", *args, "--snapshot-dir", self.tmp / "no-such-dir")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("no-such-dir", p.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 directories")
    def test_unreadable_snapshot_dir_is_a_clean_cli_error(self):
        args = self._cli_case(310)
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            p = self.run_cli(REPO / "scripts/capability_workflow.py", *args, "--snapshot-dir", locked)
        finally:
            locked.chmod(0o700)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("locked", p.stderr)


class PD03UnknownStaysUnknown(TempDirCase):
    def setUp(self):
        super().setUp()
        self.a, self.pack = scenario("landscaper-invoicing-unknowns", "analysis-quick")

    def test_missing_interviews_market_size_and_conversion_stay_unknown_with_a_plan(self):
        out = self.render(self.a, self.pack)
        md = (out / "decision-brief.md").read_text()
        unknowns = section(md, "Unknowns")
        for topic_text in ("no interviews exist", "number of potential customers is unknown", "Sign-up conversion"):
            self.assertIn(topic_text, unknowns)
        self.assertEqual(unknowns.count("resolve:"), 3)
        self.assertIn("needs approval: outreach", section(md, "Next action"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(result["status"], "needs_input")
        self.assertTrue(any("outreach" in u for u in result["unknowns"]))
        self.assertNotRegex(md, r"\b\d[\d,.]*\s*(million|thousand|operators exist|% conversion)")

    def test_generated_market_size_is_rejected(self):
        bad = mutate(self.a, lambda b: b["statements"].append(
            {"id": "l-x", "text": "There are 1.2 million solo landscapers.", "class": "estimate", "source_ids": [],
             "topic": "market-size", "basis": "industry intuition"}))
        self.assert_error(errors(bad, self.pack), "market-size is not available .* state it as fact/estimate")

    def test_dropping_an_unknown_is_rejected(self):
        bad = mutate(self.a, lambda b: b.update(statements=[s for s in b["statements"] if s["id"] != "l-u-conv"]))
        self.assert_error(errors(bad, self.pack), "baseline-conversion is not available")

    def test_every_number_in_every_scenario_traces_to_a_source_or_computation(self):
        for name, analysis in (("support-billing-backlog", "analysis-full"), ("trial-activation-causes", "analysis-causal")):
            a, pack = scenario(name, analysis)
            comps = {c["id"]: c for c in a["computations"]}
            for s in a["statements"]:
                if s["class"] not in ("observed", "synthetic"):
                    continue
                allowed = [n for sid in s["source_ids"] for n in wf.numbers_in(pack.items[sid]["excerpt"])]
                allowed += [v for sid in s["source_ids"] for v in pack.items[sid].get("values", {}).values()]
                allowed += [comps[c]["result"] for c in s.get("computation_ids", [])]
                for n in wf.numbers_in(s["text"]):
                    with self.subTest(scenario=name, statement=s["id"], number=n):
                        self.assertTrue(any(abs(n - x) < 1e-9 for x in allowed))


class PD04ModeScaling(TempDirCase):
    def setUp(self):
        super().setUp()
        self.quick, self.pack = scenario("support-billing-backlog", "analysis-quick")
        self.full, _ = scenario("support-billing-backlog", "analysis-full")

    def test_same_problem_quick_is_compact_and_full_covers_all_six_stages(self):
        q = (self.render(self.quick, self.pack, name="q") / "decision-brief.md").read_text()
        f_dir = self.render(self.full, self.pack, name="f")
        f = (f_dir / "problem-brief.md").read_text()
        q_words = len(q.split("\n## Decision\n", 1)[1].split())
        self.assertLessEqual(q_words, wf.QUICK_MAX_WORDS)
        self.assertGreater(len(f.split()), 2 * q_words)
        for title in ("SCQA", "Why tree", "Issue map", "Customer/process causal map", "Measurable hypothesis", "Stage log"):
            self.assertIn(f"## {title}", f)
            self.assertNotIn(f"## {title}", q)
        self.assertTrue((f_dir / "one-page-prd.md").exists())
        self.assertIn("draft, never approved implementation", (f_dir / "one-page-prd.md").read_text())
        for md in (q, f):
            self.assertIn("## Next action", md)
            self.assertIn("## Unknowns", md)

    def test_quick_mode_cannot_carry_full_sections_or_grow_unbounded(self):
        bad = mutate(self.quick, lambda b: b.update(why_tree=self.full["why_tree"]))
        self.assert_error(errors(bad, self.pack), "quick mode stays a compact decision brief")
        padded = mutate(self.quick, lambda b: b["statements"].extend(
            {"id": f"pad{i}", "text": "An assumption that adds words but no decision value to this brief.", "class": "assumption",
             "source_ids": []} for i in range(20)))
        files, errs = wf.render_analysis(padded, __import__("workflow_helpers").ctx(self.pack))
        self.assertEqual(files, {})
        self.assert_error(errs, "quick mode rendered")

    def test_full_mode_skips_need_reasons_and_are_refused_when_the_exercise_was_requested(self):
        def skip(b, reason):
            b.pop("issue_map")
            st = next(s for s in b["stage_log"] if s["stage"] == "issue-map")
            st["status"] = "skipped"
            if reason:
                st["reason"] = reason
        self.assert_error(errors(mutate(self.full, lambda b: skip(b, None)), self.pack), "issue-map is skipped: say why")
        self.assert_error(errors(mutate(self.full, lambda b: skip(b, "two branches only")), self.pack),
                          "explicitly requested")
        ok = mutate(self.full, lambda b: (skip(b, "Two branches only; the why tree already partitions them."),
                                          b.update(requested_full_exercise=False)))
        self.assertEqual(errors(ok, self.pack), [])

    def test_full_mode_needs_every_stage_logged(self):
        bad = mutate(self.full, lambda b: b.update(stage_log=b["stage_log"][:5]))
        self.assert_error(errors(bad, self.pack), "six stages")


class PD05SemanticHypothesisFields(unittest.TestCase):
    REST = dict(window_days=28, intervention="Friday-afternoon billing rota", measurement="ticket export median",
                counter_metric="non-billing median first response", failure_rule="stop if median stays above 18 hours")

    def test_both_label_conventions_map_to_identical_semantic_fields(self):
        ro = wf.hypothesis_from_xyz("ro-pna", {"share_percent": 30, "population": "trial accounts"},
                                    "create a first project within 7 days", "sample data shows what to do first", **self.REST)
        solo = wf.hypothesis_from_xyz("solosquad", 30, "trial accounts", "create a first project within 7 days",
                                      rationale="sample data shows what to do first", **self.REST)
        self.assertEqual(ro, solo)
        self.assertEqual((ro["population"], ro["threshold"]["value"], ro["behavior_or_outcome"]),
                         ("trial accounts", 30, "create a first project within 7 days"))
        self.assertEqual(wf.contract_errors(ro, "hypothesis", "h"), [])

    def test_positional_confusion_is_refused_not_guessed(self):
        with self.assertRaises(wf.WorkflowError):
            wf.hypothesis_from_xyz("ro-pna", 30, "trial accounts", "create a project", **self.REST)
        with self.assertRaises(wf.WorkflowError):
            wf.hypothesis_from_xyz("solosquad", "trial accounts", 30, "create a project", rationale="r", **self.REST)

    def test_window_measurement_and_failure_rule_are_never_inferred(self):
        rest = dict(self.REST)
        rest.pop("failure_rule")
        with self.assertRaisesRegex(wf.WorkflowError, "never inferred"):
            wf.hypothesis_from_xyz("solosquad", 30, "trial accounts", "create a project", rationale="r", **rest)


class ScenarioVarietyAndSkillConsumption(TempDirCase):
    def test_renderer_has_no_scenario_specific_content(self):
        source = (REPO / "scripts" / "capability_workflow.py").read_text()
        for d in EVALS.iterdir():
            pack = d / "evidence-pack.json"
            if not pack.exists():
                continue
            doc = cr.load_json(pack)
            for needle in [d.name, doc["pack_id"], doc["binding"]["task_id"]] + [i["source_id"] for i in doc["items"] if len(i["source_id"]) > 3]:
                self.assertNotIn(needle, source)

    def test_different_scenarios_produce_different_content_from_their_own_inputs(self):
        outs = []
        for name, analysis in (("support-billing-backlog", "analysis-quick"), ("landscaper-invoicing-unknowns", "analysis-quick")):
            a, pack = scenario(name, analysis)
            md = (self.render(a, pack, name=name) / "decision-brief.md").read_text()
            self.assertIn(a["decision"]["question"], md)
            outs.append(md)
        self.assertNotIn("billing", outs[1].lower())
        self.assertNotIn("landscap", outs[0].lower())

    def test_documented_skill_commands_run_against_a_scenario(self):
        skill = (REPO / "hermes/skills/problem-definition/SKILL.md").read_text()
        cmds = re.findall(r"^python3 \$KIT/scripts/capability_workflow\.py .*$", skill, re.M)
        self.assertEqual([c.split()[2] for c in cmds], ["validate", "render"])
        d = EVALS / "trial-activation-causes"
        subst = {"$KIT": str(REPO), "$ANALYSIS": str(d / "analysis-causal.json"), "$EVIDENCE": str(d / "evidence-pack.json"),
                 "$OUT": str(self.tmp / "skill-out")}
        for cmd in cmds:
            argv = cmd.split()[1:]
            for var, value in subst.items():
                argv = [x.replace(var, value) for x in argv]
            p = self.run_cli(*argv)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "completed")
        self.assertTrue((self.tmp / "skill-out" / "problem-brief.md").exists())

    def test_rendering_opens_no_network_connection(self):
        a, pack = scenario("support-billing-backlog", "analysis-full")
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            self.render(a, pack)


if __name__ == "__main__":
    unittest.main()
