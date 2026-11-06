"""PM-02 and PD-06: adapted lean PRD built on accepted upstream artifacts.

trial-activation-prd reuses the accepted discovery brief (two hops from the
original problem definition); support-billing-prd reuses a full-mode problem
brief directly, with no discovery replay. Analyses are coordinator-authored
synthetic demos; tests exercise the validator, renderer, staleness and the
planner seam.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_discovery import DiscoveryCase, ctx, errors, item, load, section  # noqa: E402
from test_capability_plan import envelope, plan, selection, stage_ids  # noqa: E402
from workflow_helpers import EVALS, REG, REPO, TempDirCase, cr, mutate, wf  # noqa: E402


def scope_item(a, sid):
    return next(x for x in a["release"]["first_version"] + a["release"]["later"] if x["id"] == sid)


class PM02LeanPrdFromDiscovery(DiscoveryCase):
    scenario, analysis = "trial-activation-prd", "analysis-lean"

    def test_lean_prd_has_segment_value_scope_release_and_traced_testable_criteria(self):
        out = self.render(self.a)
        prd = (out / "lean-prd.md").read_text()
        self.assertIn("SYNTHETIC fixture data", prd)
        self.assertIn("Status: **draft**", prd)
        for title in ("1. Summary", "3. Background", "4. Objective", "5. Market segments", "6. Value propositions",
                      "7. Solution", "8. Release"):
            self.assertIn(f"## {title}", prd)
        self.assertNotIn("## 2. Contacts", prd)  # unknown contacts are left out, not padded
        self.assertIn("What before:", section(prd, "6. Value propositions"))
        self.assertIn("target >= 36 percent within 28 days", section(prd, "4. Objective"))
        release = section(prd, "8. Release")
        self.assertIn("Not doing:", release)
        self.assertIn("Changing the welcome email schedule.", release)
        self.assertIn("7.4 Assumptions and open questions", section(prd, "7. Solution"))
        self.assertIn("The guide's effect on activation is unknown", section(prd, "7. Solution"))
        amap = (out / "acceptance-map.md").read_text()
        for ac in self.a["acceptance_criteria"]:
            self.assertIn(f"`{ac['id']}`", amap)
        self.assertIn("evidence: st-activation (synthetic)", amap)
        self.assertIn("chosen requirement:", amap)
        handoff = (out / "implementation-handoff.md").read_text()
        self.assertIn("carries no approval", handoff)
        self.assertIn("gstack.plan-design-review", handoff)
        for text in (prd, amap, handoff):
            self.assertNotIn("leaderboard", text.lower())
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["approval_requests"], [])

    def test_declined_discovery_idea_cannot_reappear_as_scope(self):
        self.assert_error(self.errs(lambda b: scope_item(b, "r-templates").update(idea_id="i-leaderboard")),
                          "i-leaderboard was declined in discovery")
        self.assert_error(self.errs(lambda b: b["release"]["first_version"].append(
            {"id": "r-x", "text": "Activation leaderboard on the dashboard."})), "Activation leaderboard.*declined")
        ok = mutate(self.a, lambda b: b["release"]["non_goals"].append({"id": "ng-x", "text": "Activation leaderboard."}))
        self.assertEqual(errors(ok, self.pack, self.store), [])
        self.assert_error(self.errs(lambda b: scope_item(b, "r-guide").update(idea_id="i-nope")), "i-nope is not an idea")

    def test_every_first_version_item_has_a_traced_testable_criterion(self):
        self.assert_error(self.errs(lambda b: b.update(acceptance_criteria=[x for x in b["acceptance_criteria"] if x["scope_id"] != "r-reopen"])),
                          "r-reopen has no acceptance criterion")
        self.assert_error(self.errs(lambda b: item(b, "acceptance_criteria", "ac-steps").update(scope_id="r-templates")),
                          "ac-steps.*not first-version scope")
        self.assert_error(self.errs(lambda b: item(b, "acceptance_criteria", "ac-activation")["trace"].update(statement_ids=["st-h-guide"])),
                          "ac-activation.*hypothesis, not evidence")
        self.assert_error(self.errs(lambda b: item(b, "acceptance_criteria", "ac-reopen").update(then="the guide is intuitive")),
                          "ac-reopen.*not testable")
        self.assert_error(self.errs(lambda b: item(b, "acceptance_criteria", "ac-activation").update(then="activation improves")),
                          "ac-activation: a metric check needs a number")

    def test_segments_value_and_solution_line_up(self):
        self.assert_error(self.errs(lambda b: b.update(value_propositions=[])), "value_propositions")
        def extra_segment(b):
            b["segments"].append({"id": "seg-x", "name": "Admins", "job": "Set up billing.", "constraints": []})
        errs = self.errs(extra_segment)
        self.assert_error(errs, "seg-x has no value proposition")
        self.assert_error(errs, "seg-x has no feature")

    def test_unknowns_stay_visible_dates_stay_relative_and_lean_stays_short(self):
        self.assert_error(self.errs(lambda b: b.update(open_assumption_ids=["st-h-guide"])),
                          "unknown st-u-effect is missing from 7.4")
        self.assert_error(self.errs(lambda b: b["release"].update(timeframe="Ship by 2026-12-01.")), "relative")
        def pad(b):
            filler = "A later item that adds length to the document but changes no decision about the first version " * 3
            b["release"]["later"] += [{"id": f"r-pad{i}", "text": filler[:290]} for i in range(11)]
            b["features"] += [{"id": f"f-pad{i}", "text": filler[:290], "segment_ids": ["seg-new-trial"]} for i in range(9)]
        files, errs = wf.render_analysis(mutate(self.a, pad), ctx(self.pack, self.store))
        self.assertEqual(files, {})
        self.assert_error(errs, "lean mode rendered")

    def test_a_prd_needs_an_accepted_opportunity_and_carries_no_approval(self):
        self.assert_error(self.errs(lambda b: b.update(reused_artifacts=[])), "kit.problem-definition")
        for field, value in (("approved", True), ("review_status", "accepted"), ("publish_issues", True)):
            with self.subTest(field=field):
                self.assert_error(self.errs(lambda b: b.update({field: value})), "Additional properties")
        self.assert_error(self.errs(lambda b: b["handoff"].update(authorizes_implementation=True)), "Additional properties")


class PD06ReuseAndStaleness(DiscoveryCase):
    scenario, analysis = "trial-activation-prd", "analysis-lean"

    def test_prd_output_is_current_and_goes_stale_when_an_upstream_artifact_changes(self):
        out = self.render(self.a)
        index = cr.load_json(out / "artifacts.json")
        caps = {c["id"]: c for c in REG["capabilities"]}
        currency = cr._artifact_currency(index["artifacts"], caps)
        self.assertTrue(all(ok for ok, _ in currency.values()), currency)
        self.assertIn("t-trial-activation.problem-brief", currency)  # the whole chain travels with the PRD
        changed = copy.deepcopy(index)
        brief = next(x for x in changed["artifacts"] if x["artifact_id"] == "t-trial-activation.problem-brief")
        brief["content_digest"] = "f" * 64  # the original problem brief was edited after acceptance
        currency = cr._artifact_currency(changed["artifacts"], caps)
        self.assertFalse(currency["t-trial-prd.lean-prd"][0])
        self.assertIn("stale", currency["t-trial-prd.lean-prd"][1])
        p = plan(selection("gstack.plan-eng-review"), env=envelope(task_id="t-100"), arts=changed)
        self.assertIn("t-trial-prd.lean-prd", [s["artifact_id"] for s in p["stale_artifacts"]])

    def test_planner_reuses_the_accepted_discovery_brief_instead_of_replaying_problem_definition(self):
        idx = cr.load_json(EVALS / self.scenario / "inputs" / "artifacts.json")
        p = plan(selection("pm.lean-prd"), arts=idx)
        self.assertEqual(stage_ids(p), ["pm.lean-prd"])
        sat = next(i for i in p["stages"][0]["inputs"] if i["name"] == "opportunity")["satisfied_by"]
        self.assertEqual(sat["artifact_id"], "t-trial-discovery.discovery-brief")
        self.assertEqual(p["status"], "planned")
        self.assertFalse(p["executable_now"])  # defined in source, but nothing observed it installed or runnable


class PM02FullPrdFromProblemBrief(DiscoveryCase):
    scenario, analysis = "support-billing-prd", "analysis-full"

    def test_full_prd_from_a_problem_brief_has_two_segments_risks_and_waits_for_the_question(self):
        out = self.render(self.a)
        prd = (out / "lean-prd.md").read_text()
        self.assertIn("(full mode)", prd)
        self.assertEqual(section(prd, "6. Value propositions").count("What after:"), 2)
        self.assertIn("## Risks", prd)
        self.assertIn("Rotate daily", section(prd, "Risks"))
        self.assertIn("Automatic replies sent to customers without an agent.", section(prd, "8. Release"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(result["status"], "needs_input")
        self.assertTrue(any("rota" in u for u in result["unknowns"]))

    def test_full_mode_needs_risks_with_mitigations_and_constraints(self):
        self.assert_error(self.errs(lambda b: b.update(risks=[])), "full mode needs at least one risk")
        self.assert_error(self.errs(lambda b: b.update(constraints=[])), "full mode needs constraints")


class VarietyAndSkill(TempDirCase):
    def test_two_prds_differ_and_come_from_their_own_inputs(self):
        outs = {}
        for name, analysis in (("trial-activation-prd", "analysis-lean"), ("support-billing-prd", "analysis-full")):
            a, pack, store = load(name, analysis)
            files, errs = wf.render_analysis(a, ctx(pack, store))
            self.assertEqual(errs, [])
            self.assertIn(a["feature_name"], files["lean-prd.md"])
            outs[name] = files["lean-prd.md"]
        self.assertNotIn("billing", outs["trial-activation-prd"].lower())
        self.assertNotIn("trial", outs["support-billing-prd"].lower())

    def test_skill_commands_run_and_upstream_commands_are_not_presented_as_hermes_commands(self):
        skill = (REPO / "hermes/skills/pm-lean-prd/SKILL.md").read_text()
        self.assertIn("name: pm-lean-prd", skill)
        self.assertNotRegex(skill, r"(?m)^\s*/write-prd\b")
        self.assertNotIn("pm-ai-shipping", skill)
        cmds = re.findall(r"^python3 \$KIT/scripts/capability_workflow\.py .*$", skill, re.M)
        self.assertEqual([c.split()[2] for c in cmds], ["validate", "render"])
        d = EVALS / "support-billing-prd"
        subst = {"$KIT": str(REPO), "$ANALYSIS": str(d / "analysis-full.json"), "$EVIDENCE": str(d / "evidence-pack.json"),
                 "$ARTIFACTS": str(d / "inputs" / "artifacts.json"), "$OUT": str(self.tmp / "skill-out")}
        for cmd in cmds:
            argv = cmd.split()[1:]
            for var, value in subst.items():
                argv = [x.replace(var, value) for x in argv]
            p = self.run_cli(*argv)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "needs_input")
        self.assertTrue((self.tmp / "skill-out" / "acceptance-map.md").exists())

    def test_stored_discovery_input_is_byte_identical_to_a_fresh_render(self):
        d = EVALS / "trial-activation-discovery"
        p = self.run_cli("scripts/capability_workflow.py", "render", "--analysis", d / "analysis-existing.json",
                         "--evidence", d / "evidence-pack.json", "--artifacts", d / "inputs" / "artifacts.json",
                         "--out", self.tmp / "disc")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.tmp / "disc" / "discovery-brief.md").read_bytes(),
                         (EVALS / "trial-activation-prd" / "inputs" / "discovery-brief.md").read_bytes())
