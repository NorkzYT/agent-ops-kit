"""PM-01: adapted product discovery (new and existing branches) on chained synthetic scenarios.

Each scenario reuses an accepted problem/decision brief that was rendered by the
problem-definition workflow from an earlier eval (the stored bytes are checked
against a fresh render). Analyses are coordinator-authored synthetic demos.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_helpers import EVALS, REG, REPO, TempDirCase, cr, mutate, repack, wf  # noqa: E402


def section(md: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}[^\n]*\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    return m.group(1) if m else ""


def load(name: str, analysis: str):
    """(analysis, pack, store) for one chained scenario."""
    d = EVALS / name
    pack, errs = wf.load_pack(d / "evidence-pack.json")
    assert not errs, errs
    store, errs = wf.load_artifacts(d / "inputs" / "artifacts.json", d / "inputs")
    assert not errs, errs
    return cr.load_json(d / f"{analysis}.json"), pack, store


def ctx(pack, store):
    c = wf.Context(registry=REG, pack=pack, store=store, extra={})
    c.extra["pack_raw"] = (pack.root / "evidence-pack.json").read_bytes()
    return c


def errors(a, pack, store):
    return wf.validate_analysis(a, ctx(pack, store))[1]


def item(a, key, iid):
    return next(x for x in a[key] if x["id"] == iid)


class DiscoveryCase(TempDirCase):
    scenario = analysis = ""

    def setUp(self):
        super().setUp()
        self.a, self.pack, self.store = load(self.scenario, self.analysis)

    def render(self, a, name="out"):  # noqa: D401 - same shape as TempDirCase.render, with the artifact store
        c = ctx(self.pack, self.store)
        files, errs = wf.render_analysis(a, c)
        self.assertEqual(errs, [])
        out = wf.build_outputs(a, c, files, (json.dumps(a, indent=2) + "\n").encode(), run_ref="run:test")
        wf.write_fresh_dir(self.tmp / name, out)
        return self.tmp / name

    def errs(self, fn):
        return errors(mutate(self.a, fn), self.pack, self.store)


class PM01ExistingProduct(DiscoveryCase):
    scenario, analysis = "trial-activation-discovery", "analysis-existing"

    def test_existing_branch_renders_brief_ranked_register_and_unrun_experiments(self):
        out = self.render(self.a)
        brief = (out / "discovery-brief.md").read_text()
        self.assertIn("SYNTHETIC fixture data", brief)
        self.assertIn(self.a["discovery_question"], section(brief, "Discovery question"))
        self.assertIn("existing product", brief)
        self.assertIn("I open an empty workspace", section(brief, "Opportunities"))
        self.assertRegex(section(brief, "Checkpoints"), r"idea-selection: answered \(source E3\)")
        decisions = section(brief, "Idea decisions")
        self.assertIn("`i-leaderboard` **declined**: Activation leaderboard", decisions)
        self.assertIn("`i-templates` **carried**", decisions)
        register = (out / "assumption-register.md").read_text()
        rows = [r for r in register.splitlines() if r.startswith("| ") and "---" not in r][1:]
        self.assertEqual([r.split("|")[1].strip() for r in rows[:2]], ["1", "2"])  # test-first ranking
        self.assertNotIn("leaderboard", register.lower())
        plan = (out / "experiment-plan.md").read_text()
        self.assertEqual(plan.count("Result: not run"), 2)
        self.assertIn("fake-door", plan)
        self.assertIn("Risk mitigation:", plan)
        self.assertNotIn("leaderboard", plan.lower())
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "completed")
        index = cr.load_json(out / "artifacts.json")
        self.assertEqual(cr.verify_artifact_contents(index, out), [])
        brief_art = next(x for x in index["artifacts"] if x["kind"] == "discovery-brief")
        reused = self.a["reused_artifacts"][0]
        self.assertIn({"artifact_id": reused["artifact_id"], "digest": reused["digest"]}, brief_art["source_refs"])

    def test_output_index_carries_the_source_closure_so_a_later_stage_can_verify_the_chain(self):
        out = self.render(self.a)
        index = cr.load_json(out / "artifacts.json")
        currency = cr._artifact_currency(index["artifacts"], {c["id"]: c for c in REG["capabilities"]})
        self.assertEqual({aid for aid, (ok, _) in currency.items() if not ok}, set())
        self.assertIn("pack-trial-activation", currency)  # the problem brief's own evidence, two hops back
        self.assertEqual(cr.verify_artifact_contents(index, out), [])

    def test_existing_branch_refuses_new_product_methods_and_categories(self):
        self.assert_error(self.errs(lambda b: item(b, "experiments", "ex-fake-door").update(method="pre-order")),
                          "pre-order is a new-product method")
        self.assert_error(self.errs(lambda b: item(b, "assumptions", "as-skip").update(category="go-to-market")),
                          "go-to-market is not an existing-product risk category")

    def test_declined_idea_cannot_come_back_through_assumptions_or_experiments(self):
        def revive(b):
            item(b, "assumptions", "as-skip")["idea_id"] = "i-leaderboard"
        self.assert_error(self.errs(revive), "i-leaderboard is declined")

    def test_results_cannot_be_simulated(self):
        self.assert_error(self.errs(lambda b: item(b, "experiments", "ex-guide-ab").update(result="passed")),
                          "experiments/1/result")

    def test_every_test_assumption_needs_an_experiment_and_actions_follow_the_matrix(self):
        self.assert_error(self.errs(lambda b: b.update(experiments=b["experiments"][:1])), "as-guide .* has no experiment")
        self.assert_error(self.errs(lambda b: item(b, "assumptions", "as-seed").update(risk="high")),
                          "as-seed: high impact and high risk means test")
        self.assert_error(self.errs(lambda b: item(b, "assumptions", "as-guide").update(rank=1)), "ranks")

    def test_production_tests_need_risk_mitigation(self):
        self.assert_error(self.errs(lambda b: item(b, "experiments", "ex-guide-ab").pop("risk_mitigation")),
                          "ex-guide-ab: ab-test on a live product needs risk_mitigation")

    def test_checkpoints_gate_idea_choices_and_answers_cite_supplied_evidence(self):
        def pending(b):
            b["checkpoints"][0].update(status="pending")
            b["checkpoints"][0].pop("answer_source_id")
        self.assert_error(self.errs(pending), "idea-selection checkpoint is pending")
        self.assert_error(self.errs(lambda b: b["checkpoints"][1].update(answer_source_id="E9")),
                          "answer cites unknown source E9")

    def test_answered_checkpoint_needs_a_recorded_answer_of_the_tasks_class(self):
        # review 2026-11-05 (wor0): any pack item id satisfied an answered checkpoint
        def public_pack(d):
            d["binding"]["data_classification"] = "public"
            d["items"].append({"source_id": "E4", "kind": "dataset", "classification": "real", "title": "Activation export",
                               "ref": "dataset:trial/export", "topics": ["activation"], "excerpt": "40 rows."})
            d["items"].append({"source_id": "E5", "kind": "document", "classification": "real", "title": "Requester reply",
                               "ref": "doc:trial/reply", "topics": ["checkpoint-reply"],
                               "excerpt": "Requester reply: carry both.\nidea-selection: i-templates, i-guide\n"
                                          "assumption-focus: as-uptake, as-guide"})

        def fn(b):
            b["binding"]["data_classification"] = "public"
            b["reused_artifacts"] = []  # keep the synthetic upstream brief out of this public variant
            b["checkpoints"][0]["answer_source_id"] = "E4"
            b["checkpoints"][1]["answer_source_id"] = "E1"
        a, pack = repack(self.tmp, EVALS / self.scenario, mutate(self.a, fn), public_pack)
        errs = errors(a, pack, self.store)
        self.assert_error(errs, r"idea-selection checkpoint: E4 is a dataset, not a recorded requester answer")
        self.assert_error(errs, r"assumption-focus checkpoint: E1 is synthetic and cannot answer for a public task")
        ok = mutate(a, lambda b: [c.update(answer_source_id="E5") for c in b["checkpoints"]])
        self.assertEqual([e for e in errors(ok, pack, self.store) if "checkpoint" in e], [])

    def test_an_answered_checkpoint_must_record_the_specific_ids_it_acts_on(self):
        # Final review 2026-11-05: any answer-kind item (e.g. the dataset fixture E1) satisfied a checkpoint.
        errs = self.errs(lambda b: [c.update(answer_source_id="E1") for c in b["checkpoints"]])
        self.assert_error(errs, r"idea-selection checkpoint: E1 does not record a line 'idea-selection: <ids>'")
        self.assert_error(errs, r"assumption-focus checkpoint: E1 does not record a line 'assumption-focus: <ids>'")
        errs = self.errs(lambda b: item(b, "ideas", "i-import").update(status="carried"))
        self.assert_error(errs, r"idea-selection checkpoint: E3 records \['i-guide', 'i-templates'\], "
                                r"but the analysis carries \['i-guide', 'i-import', 'i-templates'\]")
        def defer(b):
            x = item(b, "assumptions", "as-uptake")
            x.update(action="defer")
            x.pop("rank")
        errs = self.errs(defer)
        self.assert_error(errs, r"assumption-focus checkpoint: E3 records \['as-guide', 'as-uptake'\], "
                                r"but the analysis tests \['as-guide'\]")
        self.assertEqual([e for e in errors(self.a, self.pack, self.store) if "checkpoint" in e], [])

    def test_checkpoint_answers_are_reported_as_recorded_not_as_approval(self):
        kind = wf.KINDS["product-discovery"]
        self.assertTrue(any("Checkpoint answers are requester answers recorded in evidence-pack items" in x
                            for x in kind.limitations(self.a, None)))
        # a recorded checkpoint answer never stands in for an approval: the experiment's approval stays open
        a = mutate(self.a, lambda b: item(b, "experiments", "ex-guide-ab").update(approvals_needed=["publication"]))
        self.assertTrue(all(c["status"] == "answered" for c in a["checkpoints"]))
        self.assertEqual([e for e in errors(a, self.pack, self.store) if "approval" in e or "checkpoint" in e], [])
        self.assertIn("approval needed before experiment ex-guide-ab: publication", kind.needs_input(a, None))

    def test_the_product_trio_perspectives_are_all_present(self):
        def no_engineering(b):
            for i in b["ideas"]:
                if i["perspective"] == "engineering":
                    i["perspective"] = "product"
        self.assert_error(self.errs(no_engineering), "engineering")

    def test_a_problem_frame_must_be_reused_by_id_and_digest(self):
        errs = errors(mutate(self.a, lambda b: b.update(reused_artifacts=[])), self.pack, self.store)
        self.assert_error(errs, "kit.problem-definition")

    def test_a_changed_or_unaccepted_problem_frame_is_refused(self):
        inputs = self.tmp / "inputs"
        shutil.copytree(EVALS / self.scenario / "inputs", inputs)
        brief = inputs / "problem-brief.md"
        brief.write_text(brief.read_text() + "\nedited after acceptance\n")
        store, errs = wf.load_artifacts(inputs / "artifacts.json", inputs)
        self.assertIsNone(store)
        self.assert_error(errs, "content digest differs")
        idx = cr.load_json(EVALS / self.scenario / "inputs" / "artifacts.json")
        for x in idx["artifacts"]:
            if x["kind"] == "problem-brief":
                x["review_status"] = "draft"
                x.pop("accepted_by_ref")
        store = wf.ArtifactStore(idx, EVALS / self.scenario / "inputs")
        self.assert_error(errors(self.a, self.pack, store), "is draft, not accepted")

    def test_invented_numbers_are_rejected(self):
        self.assert_error(self.errs(lambda b: item(b, "statements", "d-chats").update(
            text="22 of 40 reviewed trial support chats asked how to start a first project.")), "number 22")


class PM01NewProduct(DiscoveryCase):
    scenario, analysis = "landscaper-discovery", "analysis-new"

    def test_new_branch_uses_pretotypes_waits_for_approvals_and_keeps_gaps_visible(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            out = self.render(self.a)
        brief = (out / "discovery-brief.md").read_text()
        self.assertIn("new product", brief)
        gaps = section(brief, "Evidence gaps")
        self.assertIn("Public research: denied by the evidence pack policy", gaps)
        self.assertEqual(gaps.count("resolve:"), 4)
        self.assertNotRegex(brief, r"\b\d[\d,.]*\s*(million|thousand|operators exist)")
        self.assertIn("`i-scheduling` **declined**", section(brief, "Idea decisions"))
        register = (out / "assumption-register.md").read_text()
        self.assertIn("go-to-market", register)
        plan = (out / "experiment-plan.md").read_text()
        self.assertIn("concierge-mvp", plan)
        self.assertIn("Approvals needed before running: outreach, publication", plan)
        result = cr.load_json(out / "result.json")
        self.assertEqual(result["status"], "needs_input")
        self.assertTrue(any("outreach" in u for u in result["unknowns"]))

    def test_new_branch_needs_a_go_to_market_assumption(self):
        self.assert_error(self.errs(lambda b: item(b, "assumptions", "as-reach").update(category="value")),
                          "new product needs at least one go-to-market assumption")

    def test_new_branch_refuses_live_product_methods(self):
        self.assert_error(self.errs(lambda b: item(b, "experiments", "ex-landing").update(method="ab-test")),
                          "ab-test is an existing-product method")

    def test_contacting_people_needs_outreach_or_publication_approval(self):
        self.assert_error(self.errs(lambda b: item(b, "experiments", "ex-concierge").update(approvals_needed=[])),
                          "ex-concierge: concierge-mvp contacts people outside the task")

    def test_willingness_to_pay_needs_a_skin_in_the_game_test(self):
        def opinion_only(b):
            item(b, "experiments", "ex-concierge")["method"] = "explainer-video"
        self.assert_error(self.errs(opinion_only), "viability .* skin-in-the-game")

    def test_unavailable_market_size_cannot_be_estimated(self):
        self.assert_error(self.errs(lambda b: b["statements"].append(
            {"id": "n-x", "text": "There are many thousands of operators.", "class": "estimate", "source_ids": [],
             "topic": "market-size", "basis": "intuition"})), "market-size is not available")


class ChainProvenanceAndSkill(TempDirCase):
    CHAINS = (("trial-activation-causes", "analysis-causal", "trial-activation-discovery", "problem-brief.md"),
              ("landscaper-invoicing-unknowns", "analysis-quick", "landscaper-discovery", "decision-brief.md"))

    def test_stored_inputs_are_byte_identical_to_a_fresh_render_of_the_upstream_scenario(self):
        for src, analysis, dest, name in self.CHAINS:
            with self.subTest(dest=dest):
                d = EVALS / src
                p = self.run_cli("scripts/capability_workflow.py", "render", "--analysis", d / f"{analysis}.json",
                                 "--evidence", d / "evidence-pack.json", "--out", self.tmp / dest)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual((self.tmp / dest / name).read_bytes(), (EVALS / dest / "inputs" / name).read_bytes())

    def test_two_scenarios_give_different_content_from_their_own_inputs(self):
        outs = {}
        for name, analysis in (("trial-activation-discovery", "analysis-existing"), ("landscaper-discovery", "analysis-new")):
            a, pack, store = load(name, analysis)
            c = ctx(pack, store)
            files, errs = wf.render_analysis(a, c)
            self.assertEqual(errs, [])
            outs[name] = files["discovery-brief.md"]
        self.assertNotIn("landscap", outs["trial-activation-discovery"].lower())
        self.assertNotIn("trial", outs["landscaper-discovery"].lower())

    def test_skill_commands_run_and_no_upstream_command_or_moved_stub_is_presented_as_working(self):
        skill = (REPO / "hermes/skills/pm-product-discovery/SKILL.md").read_text()
        self.assertNotRegex(skill, r"(?m)^\s*/discover\b")
        self.assertNotIn("pm-ai-shipping", skill)
        self.assertIn("name: pm-product-discovery", skill)
        cmds = re.findall(r"^python3 \$KIT/scripts/capability_workflow\.py .*$", skill, re.M)
        self.assertEqual([c.split()[2] for c in cmds], ["validate", "render"])
        d = EVALS / "landscaper-discovery"
        subst = {"$KIT": str(REPO), "$ANALYSIS": str(d / "analysis-new.json"), "$EVIDENCE": str(d / "evidence-pack.json"),
                 "$ARTIFACTS": str(d / "inputs" / "artifacts.json"), "$OUT": str(self.tmp / "skill-out")}
        for cmd in cmds:
            argv = cmd.split()[1:]
            for var, value in subst.items():
                argv = [x.replace(var, value) for x in argv]
            p = self.run_cli(*argv)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "needs_input")
        self.assertTrue((self.tmp / "skill-out" / "experiment-plan.md").exists())


if __name__ == "__main__":
    unittest.main()
