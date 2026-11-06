"""Workflow artifact tenant/class boundary: reused and reviewed artifacts, and their whole transitive
source closure, must share the task's company/project and a data class the task may use (the registry's
common predicate). Out-of-scope entries are refused generically, before any content read or copy."""

from __future__ import annotations

import copy
import json
import shutil
import unittest

from workflow_helpers import EVALS, REG, TempDirCase, cr, ctx, wf

FOREIGN_CO = "other-co-zz"


class BoundaryCase(TempDirCase):
    scenario = analysis = ""

    def setUp(self):
        super().setUp()
        self.inputs = self.tmp / "inputs"
        shutil.copytree(EVALS / self.scenario / "inputs", self.inputs)
        self.a = cr.load_json(EVALS / self.scenario / f"{self.analysis}.json")
        self.pack, errs = wf.load_pack(EVALS / self.scenario / "evidence-pack.json")
        self.assertEqual(errs, [])
        self.index = json.loads((self.inputs / "artifacts.json").read_text())
        self.reads = []

    def entry(self, aid):
        return next(x for x in self.index["artifacts"] if x["artifact_id"] == aid)

    def store(self):
        (self.inputs / "artifacts.json").write_text(json.dumps(self.index))
        store, errs = wf.load_artifacts(self.inputs / "artifacts.json", self.inputs, self.a["binding"])
        self.assertEqual(errs, [])
        real = store.read_bytes

        def spy(aid):  # records only reads that returned content
            data = real(aid)
            self.reads.append(aid)
            return data
        store.read_bytes = spy
        return store

    def as_class(self, cls):
        self.a["binding"]["data_classification"] = cls

    def assert_private_hidden(self, errs, *secrets):
        blob = "\n".join(errs)
        for s in secrets:
            self.assertNotIn(s, blob)


class ReuseBoundary(BoundaryCase):
    scenario, analysis = "trial-office-hours", "analysis-startup"
    brief = "t-trial-discovery.discovery-brief"

    def test_same_company_private_artifact_is_refused_for_a_public_task(self):
        self.as_class("public")
        self.entry(self.brief)["data_classification"] = "company-private"
        errs = wf.check_reuse(self.a["reused_artifacts"], self.a, self.store(), REG)
        self.assert_error(errs, rf"reused artifact {self.brief} is company-private and cannot be used by a public task")

    def test_same_company_private_artifact_is_refused_for_a_synthetic_task(self):
        self.entry(self.brief)["data_classification"] = "company-private"
        store = self.store()
        errs = wf.validate_analysis(self.a, ctx(self.pack, store))[1]
        self.assert_error(errs, rf"reused artifact {self.brief} is company-private and cannot be used by a synthetic task")
        self.assertNotIn(self.brief, self.reads)  # its text never reached the decision parser
        with self.assertRaisesRegex(cr.RegistryError, "outside this task's company/project/data-class scope"):
            store.read_text(self.brief)

    def test_synthetic_artifact_is_still_refused_for_a_public_task(self):
        self.as_class("public")
        errs = wf.check_reuse(self.a["reused_artifacts"], self.a, self.store(), REG)
        self.assert_error(errs, rf"reused artifact {self.brief} is synthetic and cannot stand for real work")

    def test_a_private_task_is_refused_like_the_planner_before_any_read(self):
        self.as_class("company-private")
        for x in self.index["artifacts"]:
            x["data_classification"] = "company-private"
        store = self.store()
        kind, errs = wf.validate_analysis(self.a, ctx(self.pack, store))
        self.assertEqual(errs, [cr.PRIVATE_RUNTIME_REFUSAL])
        self.assertEqual(self.reads, [])

    def test_a_binding_class_outside_the_capability_scope_is_refused_before_any_read(self):
        reg = copy.deepcopy(REG)
        cid = wf.KINDS[self.a["kind"]].capability_id
        cap = next(c for c in reg["capabilities"] if c["id"] == cid)
        cap["scope"]["data_classes"] = [d for d in cap["scope"]["data_classes"] if d != "synthetic"]
        c = ctx(self.pack, self.store())
        c.registry = reg
        errs = wf.validate_analysis(self.a, c)[1]
        self.assertEqual(errs, [f"{cid} does not allow synthetic data (capability scope.data_classes)"])
        self.assertEqual(self.reads, [])

    def cli_refusal(self, a, reg=None):
        """In-process CLI run whose evidence/artifact paths do not exist: any pack or index read would
        surface a different error, so an exact refusal proves the gate ran before them."""
        import contextlib
        import io
        from unittest import mock
        path = self.tmp / "analysis.json"
        path.write_text(json.dumps(a))
        out, buf = self.tmp / "out", io.StringIO()
        reg = reg if reg is not None else REG
        with mock.patch.object(wf.cr, "load_registry", return_value=(reg, [])), \
                mock.patch.object(wf, "load_pack", side_effect=AssertionError("pack read")), \
                mock.patch.object(wf, "load_artifacts", side_effect=AssertionError("index read")), \
                contextlib.redirect_stdout(buf):
            rc = wf.main(["render", "--analysis", str(path), "--evidence", str(self.tmp / "missing-pack.json"),
                          "--artifacts", str(self.tmp / "missing-index.json"), "--out", str(out)])
        self.assertEqual(rc, 1)
        self.assertFalse(out.exists())
        return json.loads(buf.getvalue())["errors"]

    def test_cli_refuses_a_private_task_before_reading_pack_or_index(self):
        self.as_class("company-private")
        self.assertEqual(self.cli_refusal(self.a), [cr.PRIVATE_RUNTIME_REFUSAL])

    def test_cli_refuses_a_class_outside_the_capability_scope_before_reading(self):
        reg = copy.deepcopy(REG)
        cid = wf.KINDS[self.a["kind"]].capability_id
        cap = next(c for c in reg["capabilities"] if c["id"] == cid)
        cap["scope"]["data_classes"] = ["public"]
        self.assertEqual(self.cli_refusal(self.a, reg),
                         [f"{cid} does not allow synthetic data (capability scope.data_classes)"])

    def test_planner_and_workflow_share_the_private_refusal(self):
        self.assertIn("private execution is refused", cr.PRIVATE_RUNTIME_REFUSAL)

    def test_own_artifact_with_a_foreign_transitive_source_is_refused_without_naming_it(self):
        src = self.entry("t-trial-activation.problem-brief")  # source of the reused brief
        src["company_id"] = FOREIGN_CO
        (self.inputs / src["location"]).write_text("FOREIGN SECRET TEXT")  # would also fail its digest if read
        a = copy.deepcopy(self.a)
        a["reused_artifacts"] = [r for r in a["reused_artifacts"] if r["artifact_id"] == self.brief]
        store = self.store()
        errs = wf.check_reuse(a["reused_artifacts"], a, store, REG)
        self.assert_error(errs, rf"reused artifact {self.brief} is stale: .*leaves this artifact's company/project/data-class scope")
        self.assert_private_hidden(errs, FOREIGN_CO, src["content_digest"], src["location"], "FOREIGN SECRET")
        self.assertEqual(self.reads, [])

    def test_own_artifact_with_a_private_source_is_refused_for_a_synthetic_task(self):
        self.entry("t-trial-activation.problem-brief")["data_classification"] = "company-private"
        a = copy.deepcopy(self.a)
        a["reused_artifacts"] = [r for r in a["reused_artifacts"] if r["artifact_id"] == self.brief]
        errs = wf.check_reuse(a["reused_artifacts"], a, self.store(), REG)
        self.assert_error(errs, rf"reused artifact {self.brief} is stale: .*data-class scope")

    def test_build_outputs_refuses_a_foreign_closure_entry_before_copying_it(self):
        src = self.entry("t-trial-activation.problem-brief")
        src["company_id"] = FOREIGN_CO
        (self.inputs / src["location"]).unlink()  # any read attempt would surface a different error
        a = copy.deepcopy(self.a)
        a["reused_artifacts"] = [r for r in a["reused_artifacts"] if r["artifact_id"] == self.brief]
        c = ctx(self.pack, self.store())
        with self.assertRaises(wf.WorkflowError) as cm:
            wf.build_outputs(a, c, {}, b"{}\n", run_ref="run:test")
        self.assertEqual(str(cm.exception), wf.OUT_OF_SCOPE)
        self.assertNotIn(src["artifact_id"], self.reads)

    def test_shared_index_foreign_entries_are_not_read_or_named_when_loading(self):
        self.index["artifacts"].append(dict(self.entry(self.brief), artifact_id="foreign.secret-brief", company_id=FOREIGN_CO,
                                            location="foreign-missing.md"))
        (self.inputs / "artifacts.json").write_text(json.dumps(self.index))
        store, errs = wf.load_artifacts(self.inputs / "artifacts.json", self.inputs, self.a["binding"])
        self.assertEqual(errs, [])
        errs = wf.validate_analysis(self.a, ctx(self.pack, store))[1]
        self.assert_private_hidden(errs, "foreign.secret-brief", FOREIGN_CO, "foreign-missing")
        self.assertEqual(errs, [])

    def test_cli_rejects_a_private_reused_artifact_and_writes_nothing(self):
        self.entry(self.brief)["data_classification"] = "company-private"
        (self.inputs / "artifacts.json").write_text(json.dumps(self.index))
        out = self.tmp / "out"
        p = self.run_cli("scripts/capability_workflow.py", "render", "--analysis", EVALS / self.scenario / f"{self.analysis}.json",
                         "--evidence", EVALS / self.scenario / "evidence-pack.json", "--artifacts", self.inputs / "artifacts.json",
                         "--out", out)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("is company-private and cannot be used by a synthetic task", p.stdout)
        self.assertFalse(out.exists())

    def test_the_unchanged_synthetic_chain_still_renders(self):
        store = self.store()
        self.assertEqual(wf.validate_analysis(self.a, ctx(self.pack, store))[1], [])


class ReviewedBoundary(BoundaryCase):
    scenario, analysis = "trial-guide-plan-review", "analysis-review"

    def test_same_company_private_reviewed_artifact_is_refused_for_a_public_task(self):
        self.as_class("public")
        self.entry("t-trial-prd.lean-prd")["data_classification"] = "company-private"
        _, errs = wf.check_reviewed(self.a, ctx(self.pack, self.store()), wf.PLAN_KINDS, "plan")
        self.assert_error(errs, r"reviewed artifact t-trial-prd.lean-prd is company-private and cannot be used by a public task")
        self.assertEqual(self.reads, [])

    def test_reviewed_artifact_with_a_foreign_source_is_refused_generically(self):
        src = self.entry("t-trial-prd.lean-prd")  # source of the reviewed design brief/package
        src["company_id"] = FOREIGN_CO
        errs = wf.validate_analysis(self.a, ctx(self.pack, self.store()))[1]
        self.assert_error(errs, r"reviewed artifact t-trial-design.design-brief is stale: .*leaves this artifact's")
        self.assert_private_hidden(errs, FOREIGN_CO)

    def test_the_unchanged_synthetic_review_still_validates(self):
        self.assertEqual(wf.validate_analysis(self.a, ctx(self.pack, self.store()))[1], [])


class DesignReviewBoundary(BoundaryCase):
    scenario, analysis = "trial-guide-review", "analysis-audit"

    def test_same_company_private_design_artifact_is_refused_for_a_public_task(self):
        self.as_class("public")
        for aid in ("t-trial-design.design-preview", "t-trial-design.design-tokens"):
            self.entry(aid)["data_classification"] = "company-private"
        errs = wf.validate_design_review(self.a, ctx(self.pack, self.store()))
        self.assert_error(errs, r"reviewed artifact t-trial-design.design-preview is company-private and cannot be used by a public task")
        self.assertEqual(self.reads, [])

    def test_same_company_private_design_artifact_is_refused_for_a_synthetic_task(self):
        self.entry("t-trial-design.design-preview")["data_classification"] = "company-private"
        errs = wf.validate_analysis(self.a, ctx(self.pack, self.store()))[1]
        self.assert_error(errs, r"reviewed artifact t-trial-design.design-preview is company-private and cannot be used by a synthetic task")
        self.assertEqual(self.reads, [])

    def test_the_unchanged_synthetic_audit_still_validates(self):
        self.assertEqual(wf.validate_analysis(self.a, ctx(self.pack, self.store()))[1], [])


class PackPreReadBoundary(TempDirCase):
    """The evidence pack's own binding is parsed and checked against the (already contract- and
    policy-checked) analysis binding before any captured file is opened; a malformed, unknown, private
    or mismatched analysis opens no pack, index or capture, and nothing is written."""
    scenario = "public-work-management-pricing"

    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "case"
        shutil.copytree(EVALS / self.scenario, self.dir)
        self.a = cr.load_json(self.dir / "analysis-scan.json")
        self.doc = json.loads((self.dir / "evidence-pack.json").read_text())
        self.captured = [it["captured_file"] for it in self.doc["items"] if "captured_file" in it]
        self.assertTrue(self.captured)
        self.reads = []

    def rebind_pack(self, **change):
        import hashlib
        self.doc["binding"].update(change)
        raw = (json.dumps(self.doc, indent=2) + "\n").encode()
        (self.dir / "evidence-pack.json").write_bytes(raw)
        self.a["evidence"]["digest"] = hashlib.sha256(raw).hexdigest()

    def spy(self):
        from unittest import mock
        real = wf.cr.safe_repo_file

        def spy(root, rel, *args, **kw):
            self.reads.append(str(rel))
            return real(root, rel, *args, **kw)
        return mock.patch.object(wf.cr, "safe_repo_file", side_effect=spy)

    def capture_reads(self):
        return [r for r in self.reads if r in self.captured]

    def cli(self, a, *, guard_loads=False):
        import contextlib
        import io
        from unittest import mock
        path = self.tmp / "analysis.json"
        path.write_text(json.dumps(a) if not isinstance(a, str) else a)
        out, buf = self.tmp / "out", io.StringIO()
        with contextlib.ExitStack() as st:
            st.enter_context(self.spy())
            st.enter_context(contextlib.redirect_stdout(buf))
            st.enter_context(contextlib.redirect_stderr(io.StringIO()))
            if guard_loads:
                st.enter_context(mock.patch.object(wf, "load_pack", side_effect=AssertionError("pack read")))
                st.enter_context(mock.patch.object(wf, "load_artifacts", side_effect=AssertionError("index read")))
            rc = wf.main(["render", "--analysis", str(path), "--evidence", str(self.dir / "evidence-pack.json"),
                          "--artifacts", str(self.tmp / "missing-index.json"), "--out", str(out)])
        self.assertFalse(out.exists())
        self.assertEqual(self.capture_reads(), [])
        return rc, buf.getvalue()

    # ---- library: load_pack checks the pack binding before captures

    def test_load_pack_refuses_a_private_pack_before_reading_captures(self):
        self.rebind_pack(data_classification="company-private")
        with self.spy():
            pack, errs = wf.load_pack(self.dir / "evidence-pack.json")
        self.assertIsNone(pack)
        self.assertEqual(errs, [cr.PRIVATE_RUNTIME_REFUSAL])
        self.assertEqual(self.capture_reads(), [])

    def test_load_pack_refuses_a_binding_mismatch_before_reading_captures(self):
        for change in ({"data_classification": "synthetic"}, {"task_id": "t-other"}, {"company_id": FOREIGN_CO}):
            self.doc = json.loads((EVALS / self.scenario / "evidence-pack.json").read_text())
            self.rebind_pack(**change)
            self.reads.clear()
            with self.spy():
                pack, errs = wf.load_pack(self.dir / "evidence-pack.json", binding=self.a["binding"])
            self.assertIsNone(pack, change)
            self.assertEqual(errs, [wf.PACK_BINDING_MISMATCH], change)
            self.assertEqual(self.capture_reads(), [], change)
            for v in change.values():
                self.assertNotIn(v, "\n".join(errs))

    def test_load_pack_refuses_a_non_dict_expected_binding_before_reading_captures(self):
        with self.spy():
            pack, errs = wf.load_pack(self.dir / "evidence-pack.json", binding="nope")
        self.assertIsNone(pack)
        self.assertEqual(errs, [wf.PACK_BINDING_MISMATCH])
        self.assertEqual(self.capture_reads(), [])

    def test_load_pack_with_the_matching_binding_still_reads_and_verifies_captures(self):
        with self.spy():
            pack, errs = wf.load_pack(self.dir / "evidence-pack.json", binding=self.a["binding"])
        self.assertEqual(errs, [])
        self.assertEqual(sorted(self.capture_reads()), sorted(self.captured))
        self.assertEqual(len(pack.captures), len(self.captured))

    # ---- CLI: analysis validated, then pack binding, before any capture/index read

    def test_cli_refuses_a_private_pack_for_a_public_analysis_without_capture_reads(self):
        self.rebind_pack(data_classification="company-private")
        rc, out = self.cli(self.a)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["errors"], [cr.PRIVATE_RUNTIME_REFUSAL])

    def test_cli_refuses_a_mismatched_pack_without_capture_reads(self):
        self.rebind_pack(task_id="t-someone-else")
        rc, out = self.cli(self.a)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["errors"], [wf.PACK_BINDING_MISMATCH])
        self.assertNotIn("t-someone-else", out)

    def test_cli_refuses_malformed_or_unknown_analyses_before_any_pack_or_index_read(self):
        bad_binding = copy.deepcopy(self.a)
        bad_binding["binding"] = "public"
        no_binding = copy.deepcopy(self.a)
        del no_binding["binding"]
        bad_class = copy.deepcopy(self.a)
        bad_class["binding"]["data_classification"] = "top-secret"
        unknown = dict(self.a, kind="not-a-kind")
        for a in (unknown, bad_binding, no_binding, bad_class, [1, 2], "null", {"kind": "competitor-research"}):
            self.reads.clear()
            rc, out = self.cli(a, guard_loads=True)
            self.assertEqual(rc, 1, a if isinstance(a, str) else str(a)[:80])
            self.assertTrue(json.loads(out)["errors"])
            self.assertNotIn(str(self.dir / "evidence-pack.json"), self.reads)
            self.assertEqual([r for r in self.reads if r == "evidence-pack.json"], [])

    def test_cli_public_fixture_still_renders_and_reads_its_captures(self):
        import contextlib
        import io
        path = self.dir / "analysis-scan.json"
        out, buf = self.tmp / "out-ok", io.StringIO()
        with self.spy(), contextlib.redirect_stdout(buf):
            rc = wf.main(["render", "--analysis", str(path), "--evidence", str(self.dir / "evidence-pack.json"),
                          "--out", str(out)])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(sorted(set(self.capture_reads())), sorted(self.captured))
        self.assertTrue((out / "result.json").exists())

    # ---- library: build_outputs gates on its own, before any read or output

    def gate(self, a, pack, store=None):
        from unittest import mock
        c = ctx(pack, store)
        with mock.patch.object(wf, "_result_source", side_effect=AssertionError("output built")), \
                mock.patch.object(wf.cr, "producer_record", side_effect=AssertionError("output built")), \
                self.spy():
            with self.assertRaises(wf.WorkflowError) as cm:
                wf.build_outputs(a, c, {"competitor-report.md": "x"}, b"{}\n", run_ref="run:test")
        self.assertEqual(self.reads, [])
        return str(cm.exception)

    def loaded(self):
        pack, errs = wf.load_pack(self.dir / "evidence-pack.json")
        self.assertEqual(errs, [])
        return pack

    def test_build_outputs_refuses_a_private_analysis_directly(self):
        pack = self.loaded()
        a = copy.deepcopy(self.a)
        a["binding"]["data_classification"] = "company-private"
        self.assertIn(cr.PRIVATE_RUNTIME_REFUSAL, self.gate(a, pack))

    def test_build_outputs_refuses_a_class_outside_the_capability_scope_directly(self):
        pack = self.loaded()
        a = copy.deepcopy(self.a)
        a["binding"]["data_classification"] = "synthetic"
        msg = self.gate(a, pack)
        self.assertTrue("does not allow synthetic data" in msg or wf.PACK_BINDING_MISMATCH in msg
                        or "differs from the evidence pack binding" in msg, msg)

    def test_build_outputs_refuses_a_pack_binding_mismatch_directly(self):
        pack = self.loaded()
        a = copy.deepcopy(self.a)
        a["binding"]["task_id"] = "t-other"
        self.assertIn("differs from the evidence pack binding", self.gate(a, pack))

    def test_build_outputs_refuses_a_tampered_pack_copy_directly(self):
        pack = self.loaded()
        (self.dir / "evidence-pack.json").write_bytes((self.dir / "evidence-pack.json").read_bytes() + b" ")
        self.assertIn("evidence pack bytes differ", self.gate(self.a, pack))

    def test_build_outputs_refuses_malformed_or_unknown_analyses_directly(self):
        pack = self.loaded()
        nb = copy.deepcopy(self.a)
        nb["binding"] = "public"
        for a in (dict(self.a, kind="nope"), nb, {"kind": "competitor-research"}, [], None):
            self.reads.clear()
            self.gate(a, pack)

    def test_build_outputs_still_builds_for_the_validated_public_fixture(self):
        pack = self.loaded()
        c = ctx(pack)
        files, errs = wf.render_analysis(self.a, c)
        self.assertEqual(errs, [])
        out = wf.build_outputs(self.a, c, files, b"{}\n", run_ref="run:test")
        self.assertIn("result.json", out)
        self.assertIn("evidence-pack.json", out)


class RegistryCurrencyBoundary(unittest.TestCase):
    def arts(self):
        base = {"kind": "problem-brief", "company_id": "c", "project_id": "p", "task_id": "t", "produced_by": None,
                "generation": 1, "data_classification": "public", "location": "x.md", "review_status": "accepted"}
        src = dict(base, artifact_id="s", content_digest="1" * 64, source_refs=[])
        top = dict(base, artifact_id="a", content_digest="2" * 64, source_refs=[{"artifact_id": "s", "digest": "1" * 64}])
        return top, src

    def currency(self, *arts):
        return cr._artifact_currency(list(arts), {c["id"]: c for c in REG["capabilities"]})

    def test_same_scope_chain_is_current(self):
        self.assertEqual(self.currency(*self.arts())["a"], (True, "current"))

    def test_cross_tenant_or_stricter_class_source_is_stale_without_its_details(self):
        for change in ({"company_id": FOREIGN_CO}, {"project_id": "q"}, {"data_classification": "company-private"},
                       {"data_classification": "synthetic"}):
            top, src = self.arts()
            src.update(change)
            ok, why = self.currency(top, src)["a"]
            self.assertFalse(ok, change)
            self.assertIn("leaves this artifact's company/project/data-class scope", why)
            for secret in ("'s'", " s ", FOREIGN_CO, "1" * 12):
                self.assertNotIn(secret, why)


if __name__ == "__main__":
    unittest.main()


class LibraryPackBindingMismatch(TempDirCase):
    """Library path: a pack loaded without an expected binding (so load_pack accepts it and verifies its
    captures during fixture setup) whose binding differs from the analysis in task, company or class.
    validate_analysis/render_analysis refuse on the binding alone: no later store read, no captured-file
    open, no output, and the generic error does not echo the foreign binding values."""
    cases = (("trial-office-hours", "analysis-startup"),        # reuse: store reads
             ("trial-guide-review", "analysis-audit"),          # design-review: store reads
             ("capability-cli-qa", "analysis-functional"),      # qa: store + captured-file reads
             ("public-work-management-pricing", "analysis-scan"))  # competitor scan: captured-file reads

    def load(self, scenario, analysis, change, analysis_class=None):
        import hashlib
        d = self.tmp / f"{scenario}-{len(list(self.tmp.iterdir()))}"
        shutil.copytree(EVALS / scenario, d)
        a = cr.load_json(d / f"{analysis}.json")
        doc = json.loads((d / "evidence-pack.json").read_text())
        doc["binding"].update(change)
        raw = (json.dumps(doc, indent=2) + "\n").encode()
        (d / "evidence-pack.json").write_bytes(raw)
        a["evidence"]["digest"] = hashlib.sha256(raw).hexdigest()  # isolate the binding difference
        if analysis_class:
            a["binding"]["data_classification"] = analysis_class
        pack, errs = wf.load_pack(d / "evidence-pack.json")  # no expected binding: captures read here
        self.assertEqual(errs, [], (scenario, change))
        store = None
        if (d / "inputs" / "artifacts.json").exists():
            store, errs = wf.load_artifacts(d / "inputs" / "artifacts.json", d / "inputs", a["binding"])
            self.assertEqual(errs, [], (scenario, change))
        captured = [it["captured_file"] for it in doc["items"] if "captured_file" in it]
        return a, pack, store, captured

    def mismatches(self, a_class):
        yield {"task_id": "t-someone-else"}, None
        yield {"company_id": FOREIGN_CO}, None
        if a_class == "synthetic":
            yield {"data_classification": "public"}, None
        else:  # a synthetic pack cannot hold public items: mismatch the class on the analysis side
            yield {}, "synthetic"

    def test_mismatched_pack_binding_stops_before_any_store_or_capture_read(self):
        from unittest import mock
        real_safe = wf.cr.safe_repo_file
        for scenario, analysis in self.cases:
            a_class = cr.load_json(EVALS / scenario / f"{analysis}.json")["binding"]["data_classification"]
            for change, analysis_class in self.mismatches(a_class):
                with self.subTest(scenario=scenario, change=change, analysis_class=analysis_class):
                    a, pack, store, captured = self.load(scenario, analysis, change, analysis_class)
                    store_reads, file_reads = [], []
                    if store is not None:
                        real_read = store.read_bytes

                        def read_spy(aid, _real=real_read):
                            store_reads.append(aid)
                            return _real(aid)
                        store.read_bytes = read_spy

                    def safe_spy(root, rel, *args, **kw):
                        file_reads.append(str(rel))
                        return real_safe(root, rel, *args, **kw)
                    c = ctx(pack, store)
                    out = self.tmp / "out"
                    with mock.patch.object(wf.cr, "safe_repo_file", side_effect=safe_spy):
                        errs = wf.validate_analysis(a, c)[1]
                        files, rerrs = wf.render_analysis(a, c)
                        with self.assertRaisesRegex(wf.WorkflowError, "refusing to build outputs"):
                            wf.build_outputs(a, c, {}, b"{}\n", run_ref="run:test")
                    expected = ["analysis binding (task/company/project/class) differs from the evidence pack binding"]
                    self.assertEqual(errs, expected)
                    self.assertEqual((files, rerrs), ({}, expected))
                    self.assertEqual(store_reads, [])
                    self.assertEqual([r for r in file_reads if r in captured], [])
                    self.assertFalse(out.exists())
                    for v in change.values():
                        self.assertNotIn(v, "\n".join(errs))

    def test_matching_pack_binding_still_validates_and_reads(self):
        from unittest import mock
        real_safe = wf.cr.safe_repo_file
        for scenario, analysis in self.cases:
            with self.subTest(scenario=scenario):
                a, pack, store, captured = self.load(scenario, analysis, {})
                store_reads, file_reads = [], []
                if store is not None:
                    real_read = store.read_bytes

                    def read_spy(aid, _real=real_read):
                        store_reads.append(aid)
                        return _real(aid)
                    store.read_bytes = read_spy

                def safe_spy(root, rel, *args, **kw):
                    file_reads.append(str(rel))
                    return real_safe(root, rel, *args, **kw)
                with mock.patch.object(wf.cr, "safe_repo_file", side_effect=safe_spy):
                    files, errs = wf.render_analysis(a, ctx(pack, store))
                self.assertEqual(errs, [])
                self.assertTrue(files)
                if scenario != "public-work-management-pricing":  # scan validation reads no capture or store bytes
                    self.assertTrue(store_reads or [r for r in file_reads if r in captured])
