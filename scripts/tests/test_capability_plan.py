"""Capability planner: closed-ID selection -> smallest dependency closure, artifact reuse,
staleness, readiness and company-boundary refusal. Plus the read-only CLIs.

Selections here are explicit-offline evaluation inputs (labeled as such in every
plan), standing in for the model-router seam; nothing classifies task text.
Offline: synthetic tenants example-a/example-b, temp dirs only, no live home.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load():
    if "capability_registry" in sys.modules:
        return sys.modules["capability_registry"]
    spec = importlib.util.spec_from_file_location("capability_registry", SCRIPTS / "capability_registry.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["capability_registry"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cr = _load()
REG, LOCK = cr.load_registry()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def envelope(**kw):
    e = {"schema_version": 1, "task_id": "t-100", "request_id": "r-100", "requester_ref": "requester:example-a-operator",
         "destination_ref": "discord-thread:example-a-ops", "company_id": "example-a", "project_id": "example-web",
         "companies_involved": ["example-a"], "intent": "synthetic evaluation", "requested_outcome": "A decision-ready artifact.",
         "data_classification": "synthetic", "egress_class": "deny", "input_artifacts": [],
         "acceptance_criteria": ["Artifact passes its rubric."], "workflow_version": "0.1.0", "recovery_generation": 0}
    e.update(kw)
    return e


def artifact(aid, kind, text="body", **kw):
    a = {"artifact_id": aid, "kind": kind, "company_id": "example-a", "project_id": "example-web", "task_id": "t-099",
         "content_digest": sha(text), "produced_by": None, "generation": 1, "data_classification": "synthetic",
         "location": f"artifacts/{aid}.md", "source_refs": [], "review_status": "accepted", "accepted_by_ref": "requester:example-a-operator"}
    a.update(kw)
    return a


def index(*arts):
    return {"schema_version": 1, "artifacts": list(arts)}


def selection(*ids, kind="explicit-offline", outcome="ok", **kw):
    s = {"schema_version": 1, "selection_id": "sel-1", "task_id": "t-100", "selector": {"kind": kind, "outcome": outcome},
         "capability_ids": list(ids)}
    s.update(kw)
    return s


def plan(sel, env=None, arts=None, **kw):
    p = cr.build_plan(REG, sel, env or envelope(), arts, repo=REPO, **kw)
    errs = cr.schema_errors(p, "plan.schema.json", "plan")
    assert not errs, errs
    return p


def stage_ids(p):
    return [s["capability_id"] for s in p["stages"]]


def stage(p, cid):
    return next(s for s in p["stages"] if s["capability_id"] == cid)


class RoutingMinimalityTest(unittest.TestCase):
    """ROU-01 at the planner contract: only selected capabilities plus required producers."""

    def test_bug_or_typo_is_the_existing_engineering_path_alone(self):
        p = plan(selection("existing.engineering-path"))
        self.assertEqual(stage_ids(p), ["existing.engineering-path"])
        self.assertEqual(stage(p, "existing.engineering-path")["readiness"]["dimensions"]["defined"]["state"], "yes")
        self.assertEqual(stage(p, "existing.engineering-path")["execution"]["writer"], "existing-claude-route")

    def test_new_saas_idea_is_problem_discovery_research_in_dependency_order_with_no_prd(self):
        p = plan(selection("pm.competitor-research", "pm.product-discovery", "kit.problem-definition"),
                 env=envelope(egress_class="public-read"))
        self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.product-discovery", "pm.competitor-research"])
        self.assertNotIn("pm.lean-prd", stage_ids(p))
        disc = stage(p, "pm.product-discovery")
        self.assertEqual(disc["after"], ["kit.problem-definition"])
        self.assertEqual(next(i for i in disc["inputs"] if i["name"] == "problem")["satisfied_by"],
                         {"stage": "kit.problem-definition", "kind": "problem-brief"})

    def test_defined_feature_reuses_the_accepted_brief_and_adds_no_discovery(self):
        brief = artifact("a-feature", "feature-brief", "Export to CSV for finance users")
        p = plan(selection("pm.lean-prd", "gstack.plan-eng-review"), env=envelope(input_artifacts=[
            {"artifact_id": "a-feature", "digest": brief["content_digest"]}]), arts=index(brief))
        self.assertEqual(stage_ids(p), ["pm.lean-prd", "gstack.plan-eng-review"])
        prd_in = next(i for i in stage(p, "pm.lean-prd")["inputs"] if i["name"] == "opportunity")
        self.assertEqual(prd_in["satisfied_by"]["artifact_id"], "a-feature")
        self.assertEqual(stage(p, "gstack.plan-eng-review")["after"], ["pm.lean-prd"])
        self.assertEqual([r["artifact_id"] for r in p["reused_artifacts"]], ["a-feature"])

    def test_feature_without_an_accepted_frame_adds_only_a_quick_problem_definition(self):
        p = plan(selection("pm.lean-prd"))
        self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.lean-prd"])
        pd = stage(p, "kit.problem-definition")
        self.assertEqual((pd["added_because"], pd["mode"], pd["required_by"]), ("dependency", "quick", ["pm.lean-prd"]))

    def test_ui_improvement_orders_audit_then_fix_then_visual_qa(self):
        shot = artifact("a-ui", "ui-evidence", "screenshots")
        p = plan(selection("gstack.qa", "existing.engineering-path", "gstack.design-review"), arts=index(shot))
        self.assertEqual(stage_ids(p), ["gstack.design-review", "existing.engineering-path", "gstack.qa"])
        self.assertEqual(stage(p, "gstack.design-review")["execution"]["writer"], "none")
        self.assertNotIn("source-modification", stage(p, "gstack.design-review")["side_effects"])

    def test_competitor_question_is_research_only(self):
        p = plan(selection("pm.competitor-research"), env=envelope(egress_class="public-read"))
        self.assertEqual(stage_ids(p), ["pm.competitor-research"])

    def test_marketing_plan_uses_the_existing_marketing_recipe(self):
        p = plan(selection("existing.marketing-team", "pm.positioning"))
        self.assertEqual(stage_ids(p), ["pm.positioning", "existing.marketing-team"])

    def test_zero_capabilities_is_a_valid_minimal_plan(self):
        p = plan(selection(), specialist_routes=None)
        self.assertEqual((p["status"], p["stages"]), ("planned", []))

    def test_handoff_targets_are_never_added_automatically(self):
        p = plan(selection("pm.product-discovery"))
        self.assertNotIn("pm.lean-prd", stage_ids(p))


class SelectionContractTest(unittest.TestCase):
    def test_unknown_or_duplicate_ids_and_bad_modes_are_invalid(self):
        self.assertEqual(plan(selection("pm.pricing"))["status"], "invalid")
        self.assertEqual(plan(selection("pm.lean-prd", modes={"pm.lean-prd": "epic"}))["status"], "invalid")
        self.assertEqual(plan(selection("pm.lean-prd", modes={"kit.problem-definition": "full"}))["status"], "invalid")
        self.assertEqual(plan(selection("pm.lean-prd", "pm.lean-prd"))["status"], "invalid")

    def test_selection_must_be_for_this_task_and_carries_no_permissions(self):
        self.assertEqual(plan(selection("pm.lean-prd", task_id="t-other"))["status"], "invalid")
        self.assertEqual(plan(selection("pm.lean-prd", grants=["web.public-read"]))["status"], "invalid")
        p = plan(selection("pm.competitor-research"), env=envelope(egress_class="public-read"))
        self.assertTrue(p["permission_effect"].startswith("none"))
        self.assertEqual(p["writes"], [])

    def test_provenance_is_labeled(self):
        self.assertIn("explicit offline", plan(selection("pm.lean-prd"))["selection_provenance"]["label"])
        p = plan(selection("pm.lean-prd", kind="model-router"))
        self.assertIn("model-router", p["selection_provenance"]["label"])

    def test_failed_selector_selects_nothing_and_fabricates_nothing(self):
        p = plan(selection(outcome="failed"))
        self.assertEqual((p["status"], p["stages"]), ("blocked", []))
        self.assertTrue(any("selector" in r for r in p["reasons"]))

    def test_mode_selection_is_applied(self):
        p = plan(selection("kit.problem-definition", modes={"kit.problem-definition": "full"}))
        self.assertEqual(stage(p, "kit.problem-definition")["mode"], "full")

    def test_specialist_routes_pass_through_as_advisory_with_honest_readiness(self):
        routes = [{"profile": "strategy", "execution": "short", "advisory_only": True, "knowledge_status": "ready"}]
        p = plan(selection("kit.problem-definition", specialist_routes=routes))
        self.assertEqual(p["advisory_profiles"][0]["profile"], "strategy")
        self.assertIn("not learned_current", p["advisory_profiles"][0]["readiness_note"])


class ArtifactReuseTest(unittest.TestCase):
    """PD-06: accepted current artifacts are reused by kind/company/project/digest; stale ones are not."""

    def test_selected_stage_with_a_current_accepted_output_is_reused_not_rerun(self):
        brief = artifact("a-prob", "problem-brief", "accepted frame")
        # Provenance: the task references the accepted output it wants reused (review 2026-11-05).
        p = plan(selection("kit.problem-definition", "pm.lean-prd"), arts=index(brief),
                 env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": brief["content_digest"]}]))
        self.assertEqual(stage(p, "kit.problem-definition")["action"], "reuse-existing")
        self.assertEqual(stage(p, "kit.problem-definition")["reused_output"]["artifact_id"], "a-prob")
        prd_in = next(i for i in stage(p, "pm.lean-prd")["inputs"] if i["name"] == "opportunity")
        self.assertEqual(prd_in["satisfied_by"]["artifact_id"], "a-prob")

    def test_changed_upstream_makes_the_downstream_artifact_stale(self):
        prob = artifact("a-prob", "problem-brief", "v2 frame")
        disc = artifact("a-disc", "discovery-brief", "discovery", source_refs=[{"artifact_id": "a-prob", "digest": sha("v1 frame")}])
        p = plan(selection("pm.lean-prd"), arts=index(prob, disc))
        prd_in = next(i for i in stage(p, "pm.lean-prd")["inputs"] if i["name"] == "opportunity")
        self.assertEqual(prd_in["satisfied_by"]["artifact_id"], "a-prob")
        self.assertIn("a-disc", [s["artifact_id"] for s in p["stale_artifacts"]])

    def test_staleness_is_transitive_and_missing_refs_are_unverifiable(self):
        a = artifact("a1", "problem-brief", "new", source_refs=[{"artifact_id": "a0", "digest": sha("x")}])
        b = artifact("a2", "discovery-brief", "d", source_refs=[{"artifact_id": "a1", "digest": sha("new")}])
        p = plan(selection("pm.lean-prd"), arts=index(a, b))
        self.assertEqual(sorted(s["artifact_id"] for s in p["stale_artifacts"]), ["a1", "a2"])
        self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.lean-prd"])

    def test_only_this_company_project_and_accepted_artifacts_are_reused(self):
        for bad in (artifact("x", "feature-brief", company_id="example-b"), artifact("x", "feature-brief", project_id="example-ops"),
                    artifact("x", "feature-brief", review_status="draft"), artifact("x", "feature-brief", review_status="superseded")):
            with self.subTest(bad={k: bad[k] for k in ("company_id", "project_id", "review_status")}):
                p = plan(selection("pm.lean-prd"), arts=index(bad))
                self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.lean-prd"])

    def test_synthetic_artifacts_never_satisfy_a_real_task(self):
        p = plan(selection("pm.lean-prd"), env=envelope(data_classification="public"),
                 arts=index(artifact("a-syn", "feature-brief", data_classification="synthetic")))
        self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.lean-prd"])

    def test_producer_version_change_makes_an_artifact_stale(self):
        a = artifact("a-old", "problem-brief", produced_by={"capability_id": "kit.problem-definition", "capability_version": "0.0.1"})
        p = plan(selection("pm.lean-prd"), arts=index(a))
        self.assertIn("a-old", [s["artifact_id"] for s in p["stale_artifacts"]])

    def test_envelope_input_digest_must_match_the_index(self):
        a = artifact("a-feature", "feature-brief", "text")
        p = plan(selection("pm.lean-prd"), env=envelope(input_artifacts=[{"artifact_id": "a-feature", "digest": sha("other")}]), arts=index(a))
        self.assertEqual(p["status"], "invalid")


class InputsAndBlockersTest(unittest.TestCase):
    def test_required_user_input_without_producer_is_needs_input(self):
        p = plan(selection("kit.operational-analysis"), env=envelope(company_id="example-a"))
        self.assertTrue(any(n["capability_id"] == "kit.operational-analysis" and n["input"] == "baseline" for n in p["needs_input"]))

    def test_planned_capabilities_are_blocked_as_not_implemented(self):
        p = plan(selection("pm.positioning"))
        self.assertEqual(p["status"], "blocked")
        self.assertTrue(any("not implemented" in b for b in stage(p, "pm.positioning")["blockers"]))
        self.assertFalse(p["executable_now"])

    def test_denied_egress_blocks_research_and_keeps_the_gap_visible(self):
        p = plan(selection("pm.competitor-research"), env=envelope(egress_class="deny"))
        self.assertTrue(any("egress" in b for b in stage(p, "pm.competitor-research")["blockers"]))
        p = plan(selection("pm.product-discovery"), env=envelope(egress_class="deny"))
        self.assertIn("web.public-read", stage(p, "pm.product-discovery")["tool_needs"]["unavailable_optional"])

    def test_company_binding_required(self):
        p = plan(selection("kit.problem-definition"), env=envelope(company_id=None, project_id=None, companies_involved=[]))
        self.assertTrue(any(n["input"] == "company_id" for n in p["needs_input"]))

    def test_approvals_and_tool_needs_are_listed_not_granted(self):
        p = plan(selection("existing.engineering-path"))
        s = stage(p, "existing.engineering-path")
        self.assertEqual([a["action"] for a in s["approvals_required"]], ["commit-push"])
        self.assertIn("code.claude-route", s["tool_needs"]["required"])
        self.assertIn("not grants", s["tool_needs"]["note"])

    def test_existing_path_is_still_not_executable_without_runtime_observation(self):
        p = plan(selection("existing.engineering-path"))
        self.assertEqual(p["status"], "planned")
        self.assertFalse(p["executable_now"])
        dims = stage(p, "existing.engineering-path")["readiness"]["dimensions"]
        self.assertEqual(dims["installed"]["state"], "unknown")


class CompanyBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.companies = [cr.load_json(p) for p in sorted((REPO / "hermes/companies/examples").glob("*.json"))]
        self.projects = {p.stem: cr.load_json(p) for p in (REPO / "hermes/projects/examples").glob("*.json")}

    def test_private_company_data_is_refused_until_a_protected_boundary_is_accepted(self):
        p = plan(selection("kit.problem-definition"), env=envelope(data_classification="company-private"))
        self.assertEqual(p["status"], "refused")
        self.assertTrue(any("protected company boundary" in r for r in p["reasons"]))

    def test_private_multi_company_context_is_refused(self):
        p = plan(selection("kit.problem-definition"), env=envelope(data_classification="company-private",
                                                                   companies_involved=["example-a", "example-b"]))
        self.assertEqual(p["status"], "refused")
        self.assertTrue(any("cross-company" in r for r in p["reasons"]))

    def test_an_input_flag_cannot_certify_boundary_acceptance(self):
        p = plan(selection("kit.problem-definition"), env=envelope(data_classification="company-private", boundary_accepted=True))
        self.assertEqual(p["status"], "invalid")
        p = plan(selection("kit.problem-definition"), env=envelope(data_classification="company-private"),
                 reported=[{"evidence_id": "ev", "check_id": "c", "subject": {"capability_id": "kit.problem-definition"},
                            "dimension": "runtime_accepted", "method": "observation", "environment_ref": "env:x", "status": "passed",
                            "exit_code": None, "counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
                            "classification": "observed", "no_claim": []}])
        self.assertEqual(p["status"], "refused")

    def test_public_and_synthetic_work_proceeds(self):
        for cls in ("public", "synthetic"):
            self.assertNotEqual(plan(selection("existing.engineering-path"), env=envelope(data_classification=cls))["status"], "refused")

    def test_project_policy_only_narrows(self):
        web = self.projects["example-web"]
        p = plan(selection("gstack.office-hours"), project=web, companies=self.companies)
        self.assertEqual(p["status"], "refused")
        self.assertTrue(any("project policy" in r for r in p["reasons"]))
        p = plan(selection("kit.problem-definition"), project=web, companies=self.companies)
        self.assertNotEqual(p["status"], "refused")

    def test_wrong_company_project_requester_or_destination_is_refused(self):
        ops = self.projects["example-ops"]
        self.assertEqual(plan(selection("kit.problem-definition"), project=ops, companies=self.companies)["status"], "refused")
        p = plan(selection("kit.problem-definition"), env=envelope(requester_ref="requester:example-b-operator"), companies=self.companies)
        self.assertEqual(p["status"], "refused")
        p = plan(selection("kit.problem-definition"), env=envelope(destination_ref="discord-thread:example-b-ops"), companies=self.companies)
        self.assertEqual(p["status"], "refused")

    def test_plan_reports_boundaries_as_not_enforced(self):
        p = plan(selection("kit.problem-definition"), companies=self.companies)
        self.assertTrue(p["company_boundaries"])
        self.assertTrue(all(v["status"] == "not-enforced" for v in p["company_boundaries"].values()))


class SelectionSeamTest(unittest.TestCase):
    """Thin closed-ID seam: the picks a classifier (the model-router seam, at coordinator
    evaluation time) returns become a selection; nothing here reads task text."""

    def test_unknown_duplicate_and_over_cap_picks_are_dropped(self):
        sel, dropped = cr.selection_from_picks("t-100", "sel-9", ["PM.Lean-PRD", "pm.lean-prd", "pm.pricing", "kit.problem-definition",
                                                                  "gstack.spec", "gstack.qa"],
                                               registry=REG, selector_kind="model-router", outcome="ok", max_picks=3)
        self.assertEqual(sel["capability_ids"], ["pm.lean-prd", "kit.problem-definition", "gstack.spec"])
        self.assertEqual([d["reason"] for d in dropped], ["duplicate", "unknown", "cap"])
        self.assertEqual(cr.schema_errors(sel, "selection.schema.json", "s"), [])

    def test_failed_classifier_yields_an_empty_failed_selection(self):
        sel, _ = cr.selection_from_picks("t-100", "sel-9", None, registry=REG, selector_kind="model-router", outcome="failed")
        self.assertEqual((sel["capability_ids"], sel["selector"]["outcome"]), ([], "failed"))
        self.assertEqual(plan(sel)["status"], "blocked")

    def test_eligible_pool_narrows_picks(self):
        sel, dropped = cr.selection_from_picks("t-100", "s", ["gstack.spec", "kit.problem-definition"], registry=REG,
                                               selector_kind="model-router", outcome="ok", eligible=["kit.problem-definition"])
        self.assertEqual(sel["capability_ids"], ["kit.problem-definition"])
        self.assertEqual(dropped, [{"id": "gstack.spec", "reason": "not-eligible"}])


class ArtifactContentTest(unittest.TestCase):
    def test_contents_are_verified_under_the_artifact_root_without_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "a1.md").write_text("frame")
            good = artifact("a1", "problem-brief", "frame")
            self.assertEqual(cr.verify_artifact_contents(index(good), root), [])
            self.assertTrue(cr.verify_artifact_contents(index(artifact("a1", "problem-brief", "other")), root))
            os.symlink(root / "artifacts" / "a1.md", root / "artifacts" / "a2.md")
            self.assertTrue(any("symlink" in e for e in cr.verify_artifact_contents(index(artifact("a2", "problem-brief", "frame")), root)))
            os.link(root / "artifacts" / "a1.md", root / "artifacts" / "a3.md")
            self.assertTrue(any("hard link" in e for e in cr.verify_artifact_contents(index(artifact("a3", "problem-brief", "frame")), root)))
            self.assertTrue(any("missing" in e for e in cr.verify_artifact_contents(index(artifact("a9", "problem-brief")), root)))


class PurityTest(unittest.TestCase):
    """The registry library and both CLIs never execute, spawn or open network connections."""

    FORBIDDEN_MODULES = {"subprocess", "socket", "urllib", "http", "requests", "ftplib", "smtplib", "asyncio", "multiprocessing", "ctypes", "pty"}
    FORBIDDEN_OS_CALLS = {"system", "popen", "execv", "execve", "execvp", "execl", "spawnv", "spawnl", "fork", "forkpty", "startfile"}
    FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}

    def test_no_exec_or_network_surface(self):
        import ast
        for name in ("capability_registry.py", "capability-plan.py"):
            tree = ast.parse((SCRIPTS / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for m in mods:
                        self.assertNotIn(m.split(".")[0], self.FORBIDDEN_MODULES, f"{name} imports {m}")
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "os":
                        self.assertNotIn(fn.attr, self.FORBIDDEN_OS_CALLS, f"{name} calls os.{fn.attr}")
                    if isinstance(fn, ast.Name):
                        self.assertNotIn(fn.id, self.FORBIDDEN_BUILTINS, f"{name} calls {fn.id}")


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, script, *args):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "TMPDIR": str(self.tmp),
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        work = self.tmp / "cwd"
        work.mkdir(exist_ok=True)
        before = sorted(p.name for p in work.iterdir())
        r = subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=work, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(sorted(p.name for p in work.iterdir()), before, "CLI wrote into its working directory")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [], "CLI wrote into HOME")
        return r

    def put(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj))
        return str(p)

    def test_registry_validate_and_list(self):
        r = self.run_cli("capability_registry.py", "validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["capabilities"], len(REG["capabilities"]))
        self.assertIn("company_boundaries", out)
        r = self.run_cli("capability_registry.py", "list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("existing.engineering-path", r.stdout)
        self.assertIn("planned", r.stdout)

    def test_registry_readiness_reports_unknown_not_ready(self):
        r = self.run_cli("capability_registry.py", "readiness", "--id", "existing.engineering-path")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out[0]["dimensions"]["installed"]["state"], "unknown")
        self.assertFalse(out[0]["executable_now"])

    def test_verify_lock_against_a_research_lock(self):
        bogus = self.put("research.json", {"files": []})
        r = self.run_cli("capability_registry.py", "verify-lock", "--research-lock", bogus)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sha256", r.stdout)

    def test_plan_with_explicit_offline_capabilities(self):
        task = self.put("task.json", envelope())
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--mode", "pm.lean-prd=lean")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([s["capability_id"] for s in out["stages"]], ["kit.problem-definition", "pm.lean-prd"])
        self.assertEqual(out["selection_provenance"]["kind"], "explicit-offline")

    def test_plan_from_a_selection_file_and_summary_format(self):
        task = self.put("task.json", envelope())
        sel = self.put("sel.json", selection("existing.engineering-path"))
        r = self.run_cli("capability-plan.py", "--task", task, "--selection", sel, "--format", "summary")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("existing.engineering-path", r.stdout)
        self.assertIn("not grants", r.stdout)

    def test_plan_exit_codes_for_refused_and_invalid(self):
        priv = self.put("priv.json", envelope(data_classification="company-private"))
        r = self.run_cli("capability-plan.py", "--task", priv, "--capability", "kit.problem-definition")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(json.loads(r.stdout)["status"], "refused")
        task = self.put("task.json", envelope())
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.nope")
        self.assertEqual(r.returncode, 2)
        dup = self.tmp / "dup.json"
        dup.write_text('{"schema_version": 1, "schema_version": 1}')
        r = self.run_cli("capability-plan.py", "--task", str(dup), "--capability", "pm.lean-prd")
        self.assertEqual(r.returncode, 2)
        self.assertIn("duplicate key", r.stderr)

    def test_plan_rejects_selection_and_capability_flags_together(self):
        task = self.put("task.json", envelope())
        sel = self.put("sel.json", selection("pm.lean-prd"))
        r = self.run_cli("capability-plan.py", "--task", task, "--selection", sel, "--capability", "pm.lean-prd")
        self.assertEqual(r.returncode, 2)

    def test_plan_verifies_artifact_contents_when_a_root_is_given(self):
        root = self.tmp / "out"
        (root / "artifacts").mkdir(parents=True)
        (root / "artifacts" / "a-feature.md").write_text("changed text")
        task = self.put("task.json", envelope())
        arts = self.put("arts.json", index(artifact("a-feature", "feature-brief", "original text")))
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts, "--artifact-root", str(root))
        self.assertEqual(r.returncode, 2)
        self.assertIn("content digest", r.stdout)

    def test_stamp_provenance_prints_registry_digests_and_writes_nothing(self):
        src = index(artifact("a1", "problem-brief", produced_by={"capability_id": "kit.problem-definition", "capability_version": "0.1.0"}),
                    artifact("a2", "problem-brief", produced_by={"capability_id": "kit.problem-definition", "capability_version": "0.0.1"}),
                    artifact("a3", "feature-brief"))
        path = self.put("idx.json", src)
        before = Path(path).read_bytes()
        r = self.run_cli("capability_registry.py", "stamp-provenance", "--index", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(path).read_bytes(), before)
        out = {a["artifact_id"]: a for a in json.loads(r.stdout)["artifacts"]}
        self.assertEqual(out["a1"]["produced_by"], live_producer("kit.problem-definition"))
        self.assertEqual(out["a2"]["produced_by"], src["artifacts"][1]["produced_by"])  # other version: left stale
        self.assertIsNone(out["a3"]["produced_by"])



class ReviewReuseAndScopeTest(unittest.TestCase):
    """Independent-review findings (2026-11-05): selected-stage reuse needs provenance,
    reuse alone is never executable, in-plan producers beat older artifacts, and task
    scope/input references cannot cross company or project."""

    def setUp(self):
        self.companies = [cr.load_json(p) for p in sorted((REPO / "hermes/companies/examples").glob("*.json"))]

    def test_selected_audit_is_not_replaced_by_an_unreferenced_prior_report(self):
        old = artifact("old-rev", "review-report", "prior review", task_id="t-001")
        p = plan(selection("gstack.review"), arts=index(old))
        self.assertEqual(stage(p, "gstack.review")["action"], "run")
        self.assertNotIn("old-rev", [r["artifact_id"] for r in p["reused_artifacts"]])

    def test_selected_mode_is_not_satisfied_by_an_output_of_unknown_mode(self):
        brief = artifact("a-prob", "problem-brief", "quick frame")
        p = plan(selection("kit.problem-definition", modes={"kit.problem-definition": "full"}),
                 env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": brief["content_digest"]}]), arts=index(brief))
        self.assertEqual(stage(p, "kit.problem-definition")["action"], "run")

    def test_another_capabilitys_output_is_not_reused_for_a_selected_stage(self):
        rep = artifact("a-rev", "review-report", "r", produced_by={"capability_id": "existing.engineering-path", "capability_version": "0.1.0"})
        p = plan(selection("gstack.review"), env=envelope(input_artifacts=[{"artifact_id": "a-rev", "digest": rep["content_digest"]}]),
                 arts=index(rep))
        self.assertEqual(stage(p, "gstack.review")["action"], "run")

    def test_referenced_accepted_output_is_still_reused_with_provenance(self):
        brief = artifact("a-prob", "problem-brief", "accepted frame")
        p = plan(selection("kit.problem-definition"),
                 env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": brief["content_digest"]}]), arts=index(brief))
        st = stage(p, "kit.problem-definition")
        self.assertEqual(st["action"], "reuse-existing")
        self.assertEqual(st["reused_output"]["artifact_id"], "a-prob")

    def test_a_plan_of_only_reused_outputs_is_never_executable_now(self):
        brief = artifact("a-prob", "problem-brief", "accepted frame")
        p = plan(selection("kit.problem-definition"),
                 env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": brief["content_digest"]}]), arts=index(brief))
        self.assertEqual(stage(p, "kit.problem-definition")["action"], "reuse-existing")
        self.assertFalse(p["executable_now"])

    def test_a_selected_in_plan_producer_beats_an_older_indexed_artifact(self):
        old = artifact("old-disc", "discovery-brief", "old")
        p = plan(selection("kit.problem-definition", "pm.lean-prd"), arts=index(old))
        prd_in = next(i for i in stage(p, "pm.lean-prd")["inputs"] if i["name"] == "opportunity")
        self.assertEqual(prd_in["satisfied_by"], {"stage": "kit.problem-definition", "kind": "problem-brief"})
        self.assertEqual(stage(p, "pm.lean-prd")["after"], ["kit.problem-definition"])

    def test_project_owned_by_another_company_is_refused(self):
        p = plan(selection("kit.problem-definition"), env=envelope(project_id="example-ops"), companies=self.companies)
        self.assertEqual(p["status"], "refused")

    def test_input_artifact_of_another_company_is_refused(self):
        b = artifact("b-art", "feature-brief", "body", company_id="example-b", project_id="example-ops")
        p = plan(selection("pm.lean-prd"), env=envelope(input_artifacts=[{"artifact_id": "b-art", "digest": b["content_digest"]}]),
                 arts=index(b))
        self.assertEqual(p["status"], "refused")
        self.assertTrue(any("b-art" in r for r in p["reasons"]))

    def test_private_input_artifact_on_a_non_private_task_is_refused(self):
        a = artifact("a-priv", "feature-brief", "body", data_classification="company-private")
        p = plan(selection("pm.lean-prd"), env=envelope(input_artifacts=[{"artifact_id": "a-priv", "digest": a["content_digest"]}]),
                 arts=index(a))
        self.assertEqual(p["status"], "refused")

    def test_stale_artifacts_of_other_tenants_are_not_disclosed(self):
        b = artifact("b-stale", "problem-brief", "x", company_id="example-b", project_id="example-ops",
                     source_refs=[{"artifact_id": "gone", "digest": sha("y")}])
        p = plan(selection("kit.problem-definition"), arts=index(b))
        self.assertNotIn("b-stale", [s["artifact_id"] for s in p["stale_artifacts"]])

    def test_stale_artifacts_of_a_stricter_class_are_not_disclosed(self):
        for cls in ("company-private", "synthetic"):
            a = artifact("a-stale", "problem-brief", "x", data_classification=cls,
                         source_refs=[{"artifact_id": "a-hidden-src", "digest": sha("y")}])
            p = plan(selection("kit.problem-definition"), env=envelope(data_classification="public"), arts=index(a))
            self.assertEqual(p["stale_artifacts"], [], cls)
            self.assertNotIn("a-hidden-src", json.dumps(p), cls)

    def test_stale_artifacts_of_the_task_class_are_still_reported(self):
        a = artifact("a-stale", "problem-brief", "x", data_classification="public",
                     source_refs=[{"artifact_id": "gone", "digest": sha("y")}])
        p = plan(selection("kit.problem-definition"), env=envelope(data_classification="public"), arts=index(a))
        self.assertIn("a-stale", [s["artifact_id"] for s in p["stale_artifacts"]])


def live_producer(cid, **kw):
    """produced_by built from the live registry record (never a hard-coded digest)."""
    c = next(x for x in REG["capabilities"] if x["id"] == cid)
    p = {"capability_id": cid, "capability_version": c["version"], "implementation_digest": c["implementation"]["digest"]}
    if c["source"]["content_digest"]:
        p["source_digest"] = c["source"]["content_digest"]
    p.update(kw)
    return p


class ProducerProvenanceTest(unittest.TestCase):
    """Review 2026-11-05 (reg3): currency binds the producer's recorded implementation and upstream
    source digests, not only its version string."""

    def currency(self, *arts):
        return cr._artifact_currency(list(arts), {c["id"]: c for c in REG["capabilities"]})

    def test_artifact_stamped_with_the_live_digests_is_current_and_reusable(self):
        a = artifact("a-prob", "problem-brief", "frame", produced_by=live_producer("kit.problem-definition"))
        self.assertEqual(self.currency(a)["a-prob"], (True, "current"))
        p = plan(selection("kit.problem-definition"), env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": a["content_digest"]}]),
                 arts=index(a))
        self.assertEqual(stage(p, "kit.problem-definition")["action"], "reuse-existing")

    def test_removed_or_renamed_producer_is_stale(self):
        a = artifact("a-gone", "problem-brief", produced_by={"capability_id": "kit.removed-cap", "capability_version": "0.1.0",
                                                             "implementation_digest": "a" * 64})
        ok, why = self.currency(a)["a-gone"]
        self.assertFalse(ok)
        self.assertIn("stale", why)
        self.assertIn("not in the registry", why)

    def test_same_version_with_a_changed_implementation_is_invalid_not_reused(self):
        a = artifact("a-prob", "problem-brief", "frame", produced_by=live_producer("kit.problem-definition", implementation_digest="b" * 64))
        ok, why = self.currency(a)["a-prob"]
        self.assertFalse(ok)
        self.assertIn("without a version bump", why)
        p = plan(selection("kit.problem-definition", "pm.lean-prd"),
                 env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": a["content_digest"]}]), arts=index(a))
        self.assertEqual(stage(p, "kit.problem-definition")["action"], "run")
        self.assertIn("a-prob", [s["artifact_id"] for s in p["stale_artifacts"]])

    def test_same_version_with_a_changed_upstream_source_is_invalid(self):
        a = artifact("a-disc", "discovery-brief", produced_by=live_producer("pm.product-discovery", source_digest="c" * 64))
        ok, why = self.currency(a)["a-disc"]
        self.assertFalse(ok)
        self.assertIn("upstream source", why)

    def test_producer_record_without_digests_is_legacy_unverified_and_not_reused(self):
        for prod in ({"capability_id": "kit.problem-definition", "capability_version": "0.1.0"},
                     live_producer("pm.product-discovery", source_digest=None)):
            prod = {k: v for k, v in prod.items() if v is not None}
            with self.subTest(prod=prod):
                a = artifact("a-old", "problem-brief" if prod["capability_id"].startswith("kit") else "discovery-brief", produced_by=prod)
                ok, why = self.currency(a)["a-old"]
                self.assertFalse(ok)
                self.assertTrue(why.startswith("legacy-unverified"), why)
                p = plan(selection("pm.lean-prd"), arts=index(a))
                self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.lean-prd"])

    def test_requester_supplied_artifact_has_no_producer_to_bind(self):
        a = artifact("a-feature", "feature-brief", produced_by=None)
        self.assertEqual(self.currency(a)["a-feature"], (True, "current"))

    def test_schema_accepts_digest_fields_and_rejects_malformed_ones(self):
        good = index(artifact("a1", "problem-brief", produced_by=live_producer("pm.product-discovery")))
        self.assertEqual(cr.schema_errors(good, "artifact.schema.json", "artifacts"), [])
        bad = index(artifact("a1", "problem-brief", produced_by=live_producer("kit.problem-definition", implementation_digest="nothex")))
        self.assertTrue(cr.schema_errors(bad, "artifact.schema.json", "artifacts"))


class ContentCheckLabelTest(unittest.TestCase):
    """Review 2026-11-05 (reg7): reuse rests on index digests unless bytes were checked; the plan says so."""

    def test_library_plan_labels_every_artifact_decision_unverified(self):
        brief = artifact("a-feature", "feature-brief", "Export to CSV")
        p = plan(selection("pm.lean-prd"), env=envelope(input_artifacts=[{"artifact_id": "a-feature", "digest": brief["content_digest"]}]),
                 arts=index(brief))
        sat = next(i for i in stage(p, "pm.lean-prd")["inputs"] if i["name"] == "opportunity")["satisfied_by"]
        self.assertEqual(sat["content_check"], "unverified")
        self.assertEqual([r["content_check"] for r in p["reused_artifacts"]], ["unverified"])
        prob = artifact("a-prob", "problem-brief", "f")
        p = plan(selection("kit.problem-definition"), env=envelope(input_artifacts=[{"artifact_id": "a-prob", "digest": prob["content_digest"]}]),
                 arts=index(prob))
        self.assertEqual(stage(p, "kit.problem-definition")["reused_output"]["content_check"], "unverified")


class ContentCheckCliTest(unittest.TestCase):
    """capability-plan.py main() in-process with registry validation stubbed out: registry validity has
    its own CLI test; here only the content-check labelling and the mismatch path are under test, so
    concurrent implementation-digest drift does not mask them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        spec = importlib.util.spec_from_file_location("capability_plan_cli", SCRIPTS / "capability-plan.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)  # type: ignore[union-attr]

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj))
        return str(p)

    def run_cli(self, _script, *args):
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest import mock
        buf = io.StringIO()
        with mock.patch.object(self.cli._registry(), "validate_registry", lambda *a, **k: []), contextlib.redirect_stdout(buf):
            rc = self.cli.main(list(args))
        return SimpleNamespace(returncode=rc, stdout=buf.getvalue(), stderr="")

    def setup_reuse(self, on_disk):
        root = self.tmp / "out"
        (root / "artifacts").mkdir(parents=True)
        (root / "artifacts" / "a-feature.md").write_text(on_disk)
        brief = artifact("a-feature", "feature-brief", "original text")
        task = self.put("task.json", envelope(input_artifacts=[{"artifact_id": "a-feature", "digest": brief["content_digest"]}]))
        return task, self.put("arts.json", index(brief)), str(root)

    def test_without_an_artifact_root_reuse_is_labelled_unverified(self):
        task, arts, _ = self.setup_reuse("original text")
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([a["content_check"] for a in out["reused_artifacts"]], ["unverified"])
        self.assertNotIn('"verified"', r.stdout)
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts, "--format", "summary")
        self.assertIn("content unverified", r.stdout)

    def test_with_a_matching_artifact_root_reuse_is_labelled_verified(self):
        task, arts, root = self.setup_reuse("original text")
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts, "--artifact-root", root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([a["content_check"] for a in out["reused_artifacts"]], ["verified"])
        sat = next(i for i in out["stages"][0]["inputs"] if i["name"] == "opportunity")["satisfied_by"]
        self.assertEqual(sat["content_check"], "verified")

    def test_a_tampered_artifact_leaves_no_reuse_decision_in_the_invalid_plan(self):
        task, arts, root = self.setup_reuse("tampered text")
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts, "--artifact-root", root)
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "invalid")
        self.assertEqual((out["stages"], out["reused_artifacts"], out["executable_now"]), ([], [], False))
        self.assertNotIn("a-feature", json.dumps(out["stages"]))
        self.assertEqual(cr.schema_errors(out, "plan.schema.json", "plan"), [])

    def test_artifact_root_checks_only_this_tenants_artifacts_and_never_names_others(self):
        # Final review 2026-11-05: a shared index's other-tenant artifact (missing under the root)
        # invalidated this task's plan and its id appeared in the errors.
        task, arts, root = self.setup_reuse("original text")
        idx = json.loads(Path(arts).read_text())
        idx["artifacts"].append(artifact("b-secret-roadmap", "feature-brief", "other tenant", company_id="example-b",
                                         project_id="example-b-app", accepted_by_ref="requester:example-b-operator"))
        arts = self.put("shared.json", idx)
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts, "--artifact-root", root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([a["content_check"] for a in out["reused_artifacts"]], ["verified"])
        self.assertNotIn("b-secret-roadmap", r.stdout)
        # an explicit reference to the foreign artifact is refused, still without a content check of it
        foreign = artifact("b-secret-roadmap", "feature-brief", "other tenant", company_id="example-b", project_id="example-b-app")
        task = self.put("task-foreign.json", envelope(input_artifacts=[{"artifact_id": "b-secret-roadmap",
                                                                         "digest": foreign["content_digest"]}]))
        r = self.run_cli("capability-plan.py", "--task", task, "--capability", "pm.lean-prd", "--artifacts", arts, "--artifact-root", root)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "refused")
        self.assertNotIn("content digest", r.stdout)
        self.assertFalse(any("artifact b-secret-roadmap:" in e for e in out.get("errors", [])))


if __name__ == "__main__":
    unittest.main()
