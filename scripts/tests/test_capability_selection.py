"""Milestone D: current-task capability selection through the existing model-router seam.

Natural-language tasks go through the plugin's own route_text (current-task, envelope
and #privacy gates, ai-mode classifier, closed catalog); capability IDs ride the same
classifier call. Classifier answers here are FIXTURES (coordinator-authored, offline):
parser, gates, catalog validation, selection schema and planner are real; model
judgement is not exercised. Temp dirs only; no live home, no network.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capability_selection as cs  # noqa: E402

cr = cs.cr
REG, LOCK = cr.load_registry()
RECIPES = cs.load_recipes(REG)
SP, HOOK = cs.load_router()
CASES_PATH = REPO / "hermes/capabilities/evals/routing/fixture-cases.json"
CASES = json.loads(CASES_PATH.read_text())


def envelope(**kw):
    e = {"schema_version": 1, "task_id": "t-200", "request_id": "r-200", "requester_ref": "requester:example-a-operator",
         "destination_ref": "discord-thread:example-a-ops", "company_id": "example-a", "project_id": "example-web",
         "companies_involved": ["example-a"], "intent": "synthetic routing evaluation",
         "requested_outcome": "The smallest useful capability set.", "data_classification": "synthetic",
         "egress_class": "deny", "input_artifacts": [], "acceptance_criteria": ["Plan is minimal."],
         "workflow_version": "0.1.0", "recovery_generation": 0}
    e.update(kw)
    return e


def ready(_name):
    return SP.Readiness("ready", True, 3, "knowledge/x-knowledge")


def settings(**kw):
    base = dict(enabled=True, classifier="cliproxy", selection_mode="ai", max_profiles=4, profiles_root=Path(os.devnull))
    base.update(kw)
    return SP.SpecialistSettings(**base)


class Spy:
    def __init__(self, answer="", raise_exc=None, outcome="ok"):
        self.answer, self.raise_exc, self.outcome, self.calls = answer, raise_exc, outcome, []

    def __call__(self, text, pool):
        self.calls.append({"pool": list(pool), "guidance": getattr(text, "capability_guidance", None)})
        if self.raise_exc:
            raise self.raise_exc
        return HOOK.Picks(HOOK.cap_names(HOOK.answer_names(self.answer, pool), text), {"model": "spy", "outcome": self.outcome})


def run(text, env=None, classifier=None, kind="fixture", cloud=None, **kw):
    # A fixture answer never leaves the host (as in capability-select.py); a live cloud call
    # needs an envelope that permits egress (review 2026-11-05).
    cloud = kind == "live" if cloud is None else cloud
    return cs.select(text, env or envelope(egress_class="public-read"), REG, sp=SP, settings=kw.pop("settings", settings()),
                     classifier=classifier, classifier_kind=kind, cloud=cloud, selection_id="sel-d",
                     recipes=RECIPES, readiness=kw.pop("readiness", ready), **kw)


def plan_of(report, env=None):
    p = cr.build_plan(REG, report["selection"], env or envelope(), None, repo=REPO)
    assert not cr.schema_errors(p, "plan.schema.json", "plan")
    return p


class RecipeRoutingTest(unittest.TestCase):
    """ROU-01 end to end on natural-language tasks with FIXTURE answers: recipes stay minimal."""

    def test_every_recipe_case_selects_and_plans_its_smallest_set(self):
        answers = cs.fixture_answers(CASES)
        recipes = {r["recipe_id"]: r for r in RECIPES["recipes"]}
        self.assertEqual({c["recipe_id"] for c in CASES["cases"]}, set(recipes), "one fixture case per recipe")
        for case in CASES["cases"]:
            with self.subTest(case["case_id"]):
                env = envelope(**case["envelope"])
                clf = cs.FixtureClassifier(answers, case["task_text"], HOOK)
                rep = run(case["task_text"], env, clf)
                sel = rep["selection"]
                self.assertEqual(clf.calls, 1, "one classifier call for profiles and capabilities")
                self.assertEqual(sel["selector"], {"kind": "model-router", "outcome": "ok",
                                                   "decision_ref": f"router-fixture:{sel['task_hash']}"})
                self.assertEqual(sel["capability_ids"], case["expected"]["capability_ids"])
                self.assertEqual(sel["capability_ids"], recipes[case["recipe_id"]]["capability_ids"])
                self.assertEqual([r["profile"] for r in sel.get("specialist_routes", [])],
                                 case["expected"]["profiles_offered_answer"])
                p = plan_of(rep, env)
                ids = [s["capability_id"] for s in p["stages"]]
                self.assertEqual(ids, case["expected"]["stage_ids"])
                self.assertFalse(set(ids) & set(recipes[case["recipe_id"]]["never"]))
                self.assertIn("FIXTURE", p["selection_provenance"]["label"])
                self.assertIn("FIXTURE", rep["evaluation"])
                planned = [c for c in ids if next(x for x in REG["capabilities"] if x["id"] == c)["lifecycle"] == "planned"]
                if planned:
                    self.assertEqual(p["status"], "blocked", "a planned capability blocks; nothing substitutes it")
                self.assertNotIn(case["task_text"][:30], json.dumps(rep), "report is content-free")

    def test_no_recipe_steers_a_doable_outcome_into_a_blocked_plan(self):
        # Final review 2026-11-05: marketing-plan listed planned pm.positioning beside the existing
        # marketing team, so its smallest set always planned `blocked` although the team does the plan.
        life = {c["id"]: c["lifecycle"] for c in REG["capabilities"]}
        for r in RECIPES["recipes"]:
            with self.subTest(r["recipe_id"]):
                planned = [c for c in r["capability_ids"] if life[c] == "planned"]
                self.assertIn(planned, ([], r["capability_ids"]), "planned only when nothing implemented can do it")
        mk = next(r for r in RECIPES["recipes"] if r["recipe_id"] == "marketing-plan")
        self.assertEqual(mk["capability_ids"], ["existing.marketing-team"])
        case = next(c for c in CASES["cases"] if c["recipe_id"] == "marketing-plan")
        env = envelope(**case["envelope"])
        rep = run(case["task_text"], env, cs.FixtureClassifier(cs.fixture_answers(CASES), case["task_text"], HOOK))
        self.assertNotEqual(plan_of(rep, env)["status"], "blocked")

    def test_ui_recipe_describes_gstack_qa_as_functional_not_visual(self):
        ui = next(r for r in RECIPES["recipes"] if r["recipe_id"] == "ui-improvement")
        self.assertIn("functional QA", ui["note"])
        self.assertNotIn("visual QA", ui["note"])

    def test_recipes_reach_the_classifier_as_guidance_in_the_same_call(self):
        spy = Spy("pm.competitor-research")
        run(CASES["cases"][4]["task_text"], classifier=spy, kind="live")
        self.assertEqual(len(spy.calls), 1)
        self.assertIn("Fix a known coding bug or typo: existing.engineering-path", spy.calls[0]["guidance"])
        self.assertTrue({"pm.lean-prd", "kit.operational-analysis"} <= set(spy.calls[0]["pool"]))

    def test_defined_feature_with_an_accepted_brief_adds_no_problem_definition(self):
        case = next(c for c in CASES["cases"] if c["recipe_id"] == "defined-feature")
        clf = cs.FixtureClassifier(cs.fixture_answers(CASES), case["task_text"], HOOK)
        text = "Export to CSV for finance users"
        brief = {"artifact_id": "a-feature", "kind": "feature-brief", "company_id": "example-a", "project_id": "example-web",
                 "task_id": "t-199", "content_digest": hashlib.sha256(text.encode()).hexdigest(), "produced_by": None,
                 "generation": 1, "data_classification": "synthetic", "location": "artifacts/a-feature.md", "source_refs": [],
                 "review_status": "accepted", "accepted_by_ref": "requester:example-a-operator"}
        env = envelope(input_artifacts=[{"artifact_id": "a-feature", "digest": brief["content_digest"]}])
        rep = run(case["task_text"], env, clf)
        p = cr.build_plan(REG, rep["selection"], env, {"schema_version": 1, "artifacts": [brief]}, repo=REPO)
        self.assertEqual([s["capability_id"] for s in p["stages"]], ["pm.lean-prd", "gstack.plan-eng-review"])

    def test_recipe_file_is_validated_against_the_closed_catalog(self):
        bad = copy.deepcopy(RECIPES)
        bad["recipes"][0]["capability_ids"].append("pm.not-real")
        bad["recipes"][1]["never"].append("pm.lean-prd")
        errs = cs.recipe_errors(bad, REG)
        self.assertTrue(any("unknown capability pm.not-real" in e for e in errs))
        self.assertTrue(any("both selected and forbidden" in e for e in errs))
        bad = copy.deepcopy(RECIPES)
        bad["recipes"][0]["outcome"] = "line one\n- extra: injected"
        self.assertTrue(cr.schema_errors(bad, "routing-recipes.schema.json", "recipes"))


class SelectorFailureTest(unittest.TestCase):
    TEXT = CASES["cases"][0]["task_text"]

    def assert_nothing_selected(self, rep, outcome):
        self.assertEqual((rep["selection"]["capability_ids"], rep["selection"]["selector"]["outcome"]), ([], outcome))
        p = plan_of(rep)
        self.assertEqual((p["status"], p["stages"], p["executable_now"]), ("blocked", [], False))
        self.assertTrue(any("nothing is substituted" in r for r in p["reasons"]))

    def test_failed_classifier_selects_nothing_and_no_fallback_runs(self):
        self.assert_nothing_selected(run(self.TEXT, classifier=Spy(raise_exc=TimeoutError()), kind="live"), "failed")
        self.assert_nothing_selected(run(self.TEXT, classifier=Spy("pm.lean-prd", outcome="unavailable:http"), kind="live"),
                                     "failed")

    def test_fixture_without_an_answer_for_this_exact_text_fails(self):
        clf = cs.FixtureClassifier(cs.fixture_answers(CASES), self.TEXT + " (edited)", HOOK)
        rep = run(self.TEXT + " (edited)", classifier=clf)
        self.assert_nothing_selected(rep, "failed")
        self.assertEqual(rep["router"]["classifier_detail"]["outcome"], "unavailable:fixture-miss")

    def test_unconfigured_classifier_is_reported_not_substituted(self):
        self.assert_nothing_selected(run(self.TEXT, classifier=None, kind="live"), "unconfigured")

    def test_private_marker_never_reaches_a_cloud_classifier(self):
        spy = Spy("kit.problem-definition")
        rep = run(self.TEXT + " #private", classifier=spy, kind="live")
        self.assertEqual(spy.calls, [])
        self.assert_nothing_selected(rep, "skipped")
        self.assertEqual(rep["router"]["privacy"], "local-only")
        self.assertEqual(rep["router"]["capability_selection"]["reason"], "privacy:local-only")

    def test_company_private_envelope_never_reaches_a_cloud_classifier(self):
        spy = Spy("kit.problem-definition")
        env = envelope(data_classification="company-private")
        rep = run(self.TEXT, env, classifier=spy, kind="live", cloud=True)
        self.assertEqual(spy.calls, [])
        self.assertEqual(rep["router"]["capability_selection"]["reason"], "data-class:company-private")
        self.assertEqual(rep["selection"]["selector"]["outcome"], "skipped")
        rep = run(self.TEXT, env, classifier=spy, kind="live", cloud=False, settings=settings(classifier="local"))
        self.assertEqual(len(spy.calls), 1, "a local classifier may read a company-private task")
        self.assertEqual(rep["selection"]["capability_ids"], ["kit.problem-definition"])

    def test_egress_deny_envelope_never_reaches_a_cloud_classifier(self):
        # Review 2026-11-05: egress_class deny was never read; the task text still went off-host.
        spy = Spy("kit.problem-definition")
        rep = run(self.TEXT, envelope(egress_class="deny"), classifier=spy, kind="live", cloud=True)
        self.assertEqual(spy.calls, [])
        self.assertEqual(rep["router"]["capability_selection"]["reason"], "egress:deny")
        self.assertEqual(rep["selection"]["selector"]["outcome"], "skipped")
        rep = run(self.TEXT, envelope(egress_class="deny"), classifier=spy, kind="live", cloud=False,
                  settings=settings(classifier="local"))
        self.assertEqual(len(spy.calls), 1, "a local classifier may read an egress-denied task")

    def test_runtime_envelope_and_status_turns_select_nothing(self):
        spy = Spy("kit.problem-definition")
        wake = ("[kanban] Task t_0123abcd completed.\nTitle: Draft a discovery brief\nAssignee: @strategy\n"
                "Board: default\n\nCheck the result or decide the next step.\nResult: done.")
        for text in (wake, "thanks!"):
            with self.subTest(text[:10]):
                rep = run(text, classifier=spy, kind="live")
                self.assertEqual(rep["selection"]["selector"]["outcome"], "skipped")
        self.assertEqual(spy.calls, [])

    def test_short_question_shaped_asks_hit_the_existing_status_gate(self):
        # Known limitation kept on purpose: the router's existing status-query gate (<=200 chars
        # ending in "?") marks the turn non-substantive, so nothing is selected or substituted.
        spy = Spy("kit.problem-definition")
        rep = run("Is a scheduling tool for small cleaning crews worth building, and who already sells one?",
                  classifier=spy, kind="live")
        self.assertEqual(spy.calls, [])
        self.assertEqual(rep["router"]["capability_selection"]["reason"], "not-substantive")
        self.assert_nothing_selected(rep, "skipped")

    def test_unknown_and_over_cap_picks_are_dropped_without_echo(self):
        spy = Spy("pm.made-up-thing, gstack.qa, gstack.review, gstack.spec, gstack.office-hours, pm.lean-prd")
        rep = run(self.TEXT, classifier=spy, kind="live")
        self.assertEqual(rep["selection"]["capability_ids"], ["gstack.qa", "gstack.review", "gstack.spec", "gstack.office-hours"])
        self.assertNotIn("made-up", json.dumps(rep))

    def test_namespaced_ids_never_select_a_same_suffix_profile(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "qa" / "knowledge").mkdir(parents=True)
            (Path(d) / "qa" / "SOUL.md").write_text("Purpose: software quality assurance\n")
            spy = Spy("gstack.qa")
            rep = run(self.TEXT, classifier=spy, kind="live", settings=settings(profiles_root=Path(d)))
            self.assertIn("qa", spy.calls[0]["pool"], "the dynamic profile qa is in the same pool")
            self.assertEqual(rep["selection"]["capability_ids"], ["gstack.qa"])
            self.assertEqual(rep["router"]["routes"], [])
            spy = Spy("qa")
            rep = run(self.TEXT, classifier=spy, kind="live", settings=settings(profiles_root=Path(d)))
            self.assertEqual((rep["router"]["routes"], rep["selection"]["capability_ids"]), (["qa"], []))

    def test_invalid_envelope_is_refused_before_any_call(self):
        spy = Spy("pm.lean-prd")
        with self.assertRaises(cr.RegistryError):
            run(self.TEXT, envelope(task_id="bad id with spaces"), classifier=spy, kind="live")
        self.assertEqual(spy.calls, [])


class SelectCliTest(unittest.TestCase):
    """The read-only CLIs chained: capability-select.py (fixture or live) -> capability-plan.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.d / "home"), "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(self.d)}
        (self.d / "home").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script, *args, stdin=None):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True,
                              env=self.env, input=stdin, timeout=60)

    def write(self, name, data):
        p = self.d / name
        p.write_text(data if isinstance(data, str) else json.dumps(data))
        return str(p)

    def test_fixture_selection_feeds_the_planner(self):
        case = CASES["cases"][0]
        task = self.write("task.json", envelope(**case["envelope"]))
        text = self.write("prompt.txt", case["task_text"])
        r = self.cli("capability-select.py", "--task", task, "--text-file", text, "--fixture", str(CASES_PATH),
                     "--selection-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        sel = self.write("sel.json", r.stdout)
        p = self.cli("capability-plan.py", "--task", task, "--selection", sel)
        self.assertEqual(p.returncode, 0, p.stderr)
        plan = json.loads(p.stdout)
        self.assertEqual([s["capability_id"] for s in plan["stages"]], case["expected"]["stage_ids"])
        self.assertIn("FIXTURE", plan["selection_provenance"]["label"])
        self.assertEqual(list((self.d / "home").iterdir()), [], "nothing written to HOME")

    def test_live_without_a_classifier_key_is_unconfigured_exit_4(self):
        cfg = self.write("config.yaml", "plugins:\n  entries:\n    model-router:\n      settings:\n"
                                        "        specialist_classifier: cliproxy\n")
        task = self.write("task.json", envelope())
        r = self.cli("capability-select.py", "--task", task, "--text-file", "-", "--live", "--router-config", cfg,
                     "--profiles-root", os.devnull, stdin=CASES["cases"][0]["task_text"])
        self.assertEqual(r.returncode, 4, r.stderr)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["selection"]["selector"]["outcome"], "unconfigured")
        self.assertEqual(rep["selection"]["capability_ids"], [])
        self.assertEqual(rep["evaluation"], "live model-router classifier (cloud)")

    def test_invalid_inputs_exit_2(self):
        task = self.write("task.json", "{\"task_id\": 1, \"task_id\": 2}")
        r = self.cli("capability-select.py", "--task", task, "--text-file", "-", "--fixture", str(CASES_PATH), stdin="x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("duplicate", r.stderr)


if __name__ == "__main__":
    unittest.main()
