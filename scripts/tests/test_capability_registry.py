"""Capability registry: strict JSON, schema + semantic validation, source lock, readiness.

Offline and read-only: the real registry files under hermes/capabilities are
loaded as data; every negative case mutates a copy in a temp directory. Nothing
here installs, executes, or reads the live Hermes home.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("capability_registry", SCRIPTS / "capability_registry.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["capability_registry"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cr = _load()


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class StrictJsonTest(TempDir):
    def test_plain_object_loads(self):
        self.assertEqual(cr.load_json(self.write("a.json", '{"a": [1, 2.5, "x"]}')), {"a": [1, 2.5, "x"]})

    def test_duplicate_keys_are_rejected_at_any_depth(self):
        for text in ('{"a": 1, "a": 2}', '{"o": {"k": 1, "k": 1}}', '[{"x": 0, "x": 0}]'):
            with self.subTest(text=text), self.assertRaisesRegex(cr.RegistryError, "duplicate key"):
                cr.load_json(self.write("d.json", text))

    def test_non_finite_numbers_are_rejected(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e999}'):
            with self.subTest(text=text), self.assertRaisesRegex(cr.RegistryError, "non-finite"):
                cr.load_json(self.write("n.json", text))

    def test_oversized_file_is_rejected_before_parsing(self):
        p = self.write("big.json", '{"a": "' + "x" * 200 + '"}')
        with self.assertRaisesRegex(cr.RegistryError, "exceeds"):
            cr.load_json(p, max_bytes=100)

    def test_overlong_integer_literal_is_rejected(self):
        with self.assertRaisesRegex(cr.RegistryError, "integer"):
            cr.load_json(self.write("i.json", '{"a": ' + "9" * 40 + "}"))

    def test_symlink_is_rejected(self):
        target = self.write("t.json", "{}")
        link = self.tmp / "l.json"
        os.symlink(target, link)
        with self.assertRaisesRegex(cr.RegistryError, "symlink"):
            cr.load_json(link)

    def test_symlinked_parent_directory_is_rejected(self):
        self.write("real/t.json", "{}")
        os.symlink(self.tmp / "real", self.tmp / "alias")
        with self.assertRaisesRegex(cr.RegistryError, "symlink"):
            cr.load_json(self.tmp / "alias" / "t.json")

    def test_hardlinked_file_is_rejected(self):
        target = self.write("h.json", "{}")
        os.link(target, self.tmp / "h2.json")
        with self.assertRaisesRegex(cr.RegistryError, "hard link"):
            cr.load_json(target)

    def test_non_regular_file_is_rejected(self):
        with self.assertRaisesRegex(cr.RegistryError, "regular file"):
            cr.load_json(self.tmp)

    def test_repo_file_that_grew_past_the_cap_after_the_size_check_is_refused(self):
        # reg9: simulate growth between lstat and read by under-reporting the size at lstat time.
        from unittest import mock
        self.write("grow.txt", "x" * 64)
        real = os.lstat

        def small(path, *a, **kw):
            st = real(path, *a, **kw)
            if str(path).endswith("grow.txt"):
                fields = list(st)
                fields[6] = 1  # st_size
                return os.stat_result(fields)
            return st
        with mock.patch.object(cr.os, "lstat", small):
            with self.assertRaisesRegex(cr.RegistryError, "exceeds"):
                cr.safe_repo_file(self.tmp, "grow.txt", max_bytes=16)

    def test_invalid_utf8_and_syntax_errors_are_registry_errors(self):
        p = self.tmp / "bad.json"
        p.write_bytes(b'{"a": "\xff"}')
        with self.assertRaises(cr.RegistryError):
            cr.load_json(p)
        with self.assertRaises(cr.RegistryError):
            cr.load_json(self.write("s.json", '{"a": }'))


CAPS = REPO / "hermes" / "capabilities"


def shipped():
    return cr.load_json(CAPS / "registry.json"), cr.load_json(CAPS / "upstreams.lock.json")


def cap(reg, cid):
    return next(c for c in reg["capabilities"] if c["id"] == cid)


class RegistryValidationTest(unittest.TestCase):
    def setUp(self):
        self.reg, self.lock = shipped()

    def errors(self, reg=None, lock=None, repo=REPO):
        return cr.validate_registry(reg if reg is not None else self.reg,
                                    lock if lock is not None else self.lock, repo=repo)

    def assertError(self, pattern, reg=None, lock=None, repo=REPO):
        errs = self.errors(reg, lock, repo)
        self.assertTrue(any(__import__("re").search(pattern, e) for e in errs), f"{pattern!r} not in {errs}")

    def mutated(self, fn):
        reg = copy.deepcopy(self.reg)
        fn(reg)
        return reg

    def test_shipped_registry_and_lock_are_valid(self):
        self.assertEqual(self.errors(), [])
        ids = [c["id"] for c in self.reg["capabilities"]]
        for wanted in ("kit.problem-definition", "pm.product-discovery", "pm.lean-prd", "gstack.office-hours", "gstack.spec",
                       "gstack.plan-eng-review", "gstack.plan-design-review", "gstack.design-consultation", "gstack.review",
                       "gstack.qa", "gstack.design-review", "pm.competitor-research", "existing.engineering-path"):
            self.assertIn(wanted, ids)

    def test_every_schema_is_a_valid_draft_2020_12_schema(self):
        names = sorted(p.name for p in (CAPS / "schemas").glob("*.json"))
        self.assertIn("registry.schema.json", names)
        for name in names:
            with self.subTest(schema=name):
                cr.check_schema(cr.load_json(CAPS / "schemas" / name))

    def test_no_capability_is_shipped_as_a_full_gstack_ship_or_upstream_installer(self):
        paths = {p for c in self.reg["capabilities"] for p in c["source"]["paths"]}
        self.assertFalse({p for p in paths if p.startswith(("ship/", "setup", "bin/", "setup-browser-cookies/"))})

    def test_duplicate_capability_id(self):
        self.assertError("duplicate capability id", self.mutated(lambda r: r["capabilities"].append(copy.deepcopy(r["capabilities"][0]))))

    def test_permission_grant_or_stored_readiness_fields_are_schema_errors(self):
        for field, value in (("permissions_granted", ["web.public-read"]), ("readiness", "ready"),
                             ("installed", True), ("runtime_accepted", True), ("command", "rm -rf /")):
            with self.subTest(field=field):
                self.assertError("schema", self.mutated(lambda r: cap(r, "pm.lean-prd").__setitem__(field, value)))
        self.assertError("schema", self.mutated(lambda r: r.__setitem__("grants", {})))
        self.assertError("schema", self.mutated(lambda r: cap(r, "pm.lean-prd")["tools"]["required"].append("terminal.unrestricted")))

    def test_auto_update_cannot_be_enabled(self):
        self.assertError("schema", self.mutated(lambda r: cap(r, "gstack.qa")["governance"].__setitem__("auto_update", True)))

    def test_unknown_producer_handoff_and_mode(self):
        self.assertError("unknown producer", self.mutated(lambda r: cap(r, "pm.lean-prd")["inputs"][1].__setitem__("producer", "kit.nope")))
        self.assertError("unknown handoff", self.mutated(lambda r: cap(r, "pm.lean-prd").__setitem__("handoff_to", ["pm.nope"])))
        self.assertError("producer_mode", self.mutated(lambda r: cap(r, "pm.lean-prd")["inputs"][1].__setitem__("producer_mode", "full-ish")))
        self.assertError("default_mode", self.mutated(lambda r: cap(r, "kit.problem-definition")["execution"].__setitem__("default_mode", "deep")))

    def test_producer_must_output_a_requested_kind(self):
        self.assertError("does not output", self.mutated(
            lambda r: cap(r, "pm.lean-prd")["inputs"][1].__setitem__("producer", "gstack.qa")))

    def test_producer_cycle_is_rejected(self):
        def cyc(r):
            pd = cap(r, "kit.problem-definition")
            pd["inputs"].append({"name": "prd", "kinds": ["lean-prd"], "required": True, "producer": "pm.lean-prd"})
        self.assertError("cycle", self.mutated(cyc))

    def test_skill_name_collisions_and_prefixes(self):
        self.assertError("reserved", self.mutated(lambda r: cap(r, "gstack.review").__setitem__("exposed_skill_name", "review")))
        self.assertError("existing kit skill", self.mutated(
            lambda r: cap(r, "kit.problem-definition").__setitem__("exposed_skill_name", "kanban-workflow")))
        self.assertError("prefix", self.mutated(lambda r: cap(r, "gstack.qa").__setitem__("exposed_skill_name", "qa-audit")))
        self.assertError("prefix", self.mutated(lambda r: cap(r, "kit.problem-definition").__setitem__("exposed_skill_name", "pm-problem")))
        self.assertError("duplicate exposed", self.mutated(lambda r: cap(r, "pm.lean-prd").__setitem__("exposed_skill_name", "pm-product-discovery")))

    def test_source_paths_must_be_locked_selected_files(self):
        self.assertError("not in the lock", self.mutated(lambda r: cap(r, "pm.lean-prd")["source"]["paths"].append("pm-execution/skills/made-up/SKILL.md")))
        self.assertError("moved stub", self.mutated(
            lambda r: cap(r, "pm.lean-prd")["source"]["paths"].append("pm-ai-shipping/skills/code-review/SKILL.md")))
        self.assertError("inspiration", self.mutated(
            lambda r: cap(r, "kit.problem-definition")["source"]["inspiration_refs"].append({"upstream": "pna-builders", "path": "src/app/page.tsx"})))

    def test_reference_only_upstream_cannot_be_adapted(self):
        def adapt_pna(r):
            s = cap(r, "kit.problem-definition")["source"]
            s.update(kind="adapted", upstream="pna-builders", revision=self.lock["sources"]["pna-builders"]["revision"],
                     paths=["src/lib/chatbot/systemPrompt.ts"], license="MIT")
        self.assertError("reference-only", self.mutated(adapt_pna))

    def test_revision_and_content_digest_must_match_the_lock(self):
        self.assertError("revision", self.mutated(lambda r: cap(r, "gstack.spec")["source"].__setitem__("revision", "0" * 40)))
        self.assertError("content_digest", self.mutated(lambda r: cap(r, "gstack.spec")["source"].__setitem__("content_digest", "0" * 64)))
        self.assertError("content_digest", self.mutated(lambda r: cap(r, "gstack.spec")["source"]["paths"].pop()))

    def test_lifecycle_and_implementation_consistency(self):
        self.assertError("planned", self.mutated(lambda r: cap(r, "pm.positioning")["implementation"].update(
            paths=["hermes/skills/kanban-workflow/SKILL.md"], digest="0" * 64)))
        self.assertError("existing", self.mutated(lambda r: cap(r, "existing.engineering-path")["implementation"].update(paths=[], digest=None)))
        self.assertError("implementation digest", self.mutated(
            lambda r: cap(r, "existing.engineering-path")["implementation"].__setitem__("digest", "1" * 64)))

    def test_implementation_paths_must_stay_inside_the_repo(self):
        def esc(r):
            cap(r, "existing.marketing-team")["implementation"]["paths"] = ["hermes/../../etc/passwd"]
        self.assertError("schema", self.mutated(esc))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "hermes" / "teams").mkdir(parents=True)
            (root / "hermes" / "profiles").mkdir(parents=True)
            os.symlink(REPO / "hermes" / "profiles" / "marketing", root / "hermes" / "profiles" / "marketing")
            (root / "hermes" / "teams" / "marketing-team.md").write_bytes((REPO / "hermes/teams/marketing-team.md").read_bytes())
            self.assertError("symlink", repo=root)

    def test_read_only_capabilities_cannot_declare_source_changes(self):
        self.assertError("writer", self.mutated(lambda r: cap(r, "gstack.design-review")["side_effects"].append("source-modification")))
        self.assertError("approval", self.mutated(lambda r: cap(r, "existing.engineering-path").__setitem__("approvals", [])))
        self.assertError("'none'", self.mutated(lambda r: cap(r, "pm.lean-prd")["side_effects"].append("none")))

    def test_private_data_with_public_egress_needs_the_private_research_approval(self):
        def leak(r):
            c = cap(r, "pm.competitor-research")
            c["scope"]["data_classes"].append("company-private")
            c["approvals"] = []
        self.assertError("external-research-if-private-context", self.mutated(leak))

    def test_tool_need_cannot_be_both_required_and_optional(self):
        self.assertError("both required and optional", self.mutated(lambda r: cap(r, "pm.lean-prd")["tools"]["optional"].append("read.scoped")))

    def test_spend_or_external_write_tool_needs_its_side_effect(self):
        # Review 2026-11-05 (reg8): tool needs must not understate side effects.
        self.assertError(r"image\.generate-paid.*spend",
                         self.mutated(lambda r: cap(r, "gstack.design-review")["tools"]["optional"].append("image.generate-paid")))
        self.assertError(r"github\.approved-write.*external-write",
                         self.mutated(lambda r: cap(r, "gstack.spec")["tools"]["optional"].append("github.approved-write")))

        def declared(r):
            c = cap(r, "gstack.design-review")
            c["tools"]["optional"].append("image.generate-paid")
            c["side_effects"].append("spend")
        self.assertFalse([e for e in self.errors(self.mutated(declared)) if "image.generate-paid" in e])

    def test_no_shipped_capability_lists_an_unimplemented_paid_or_external_write_tool(self):
        # No implementation path calls image generation or GitHub writes; listing them would overstate support.
        for c in self.reg["capabilities"]:
            needs = set(c["tools"]["required"]) | set(c["tools"]["optional"])
            self.assertFalse(needs & {"image.generate-paid", "github.approved-write"}, c["id"])

    def test_implementation_files_never_embed_a_registry_digest(self):
        # Artifact provenance records implementation digests; an implementation path holding one would make the
        # digest self-referential (cyclic). Checked mechanically over every shipped implementation file.
        digests = {c["implementation"]["digest"] for c in self.reg["capabilities"] if c["implementation"]["digest"]}
        for c in self.reg["capabilities"]:
            for p in c["implementation"]["paths"]:
                text = (REPO / p).read_text(encoding="utf-8", errors="replace")
                self.assertFalse([d for d in digests if d in text], f"{c['id']}: {p} embeds a registry implementation digest")


class LockValidationTest(unittest.TestCase):
    def setUp(self):
        self.reg, self.lock = shipped()

    def assertError(self, pattern, lock):
        errs = cr.validate_registry(self.reg, lock, repo=REPO)
        self.assertTrue(any(__import__("re").search(pattern, e) for e in errs), f"{pattern!r} not in {errs}")

    def test_pins_are_exactly_the_reviewed_revisions(self):
        self.assertEqual({k: v["revision"] for k, v in self.lock["sources"].items() if k in cr.PINNED_REVISIONS}, cr.PINNED_REVISIONS)
        lock = copy.deepcopy(self.lock)
        lock["sources"]["gstack"]["revision"] = "f" * 40
        self.assertError("pinned", lock)

    def test_duplicate_lock_entry(self):
        lock = copy.deepcopy(self.lock)
        lock["files"].append(dict(lock["files"][0]))
        self.assertError("duplicate lock entry", lock)

    def test_every_pm_shipping_stub_stays_excluded(self):
        stubs = [f for f in self.lock["files"] if f["path"].startswith("pm-ai-shipping/")]
        self.assertEqual(sum(1 for f in stubs if "/skills/" in f["path"]), 3)
        self.assertEqual(sum(1 for f in stubs if "/commands/" in f["path"]), 5)
        lock = copy.deepcopy(self.lock)
        next(f for f in lock["files"] if f["path"].startswith("pm-ai-shipping/"))["status"] = "selected"
        self.assertError("moved stub", lock)

    def test_reference_only_sources_have_no_selected_files(self):
        lock = copy.deepcopy(self.lock)
        next(f for f in lock["files"] if f["repo"] == "pna-builders")["status"] = "selected"
        self.assertError("reference-only", lock)

    def test_license_notice_files_are_locked_for_every_adapted_source(self):
        lock = copy.deepcopy(self.lock)
        lock["files"] = [f for f in lock["files"] if not (f["repo"] == "gstack" and f["path"] == "LICENSE")]
        self.assertError("LICENSE", lock)


class ResearchLockTest(unittest.TestCase):
    def setUp(self):
        _, self.lock = shipped()
        files = [{k: f[k] for k in ("repo", "path", "sha256", "bytes")} for f in self.lock["files"]]
        files.append({"repo": "gstack", "path": "ship/SKILL.md", "sha256": "a" * 64, "bytes": 1})
        self.research = {"schema_version": 1, "research_date": self.lock["research_lock"]["research_date"],
                         "sources": {k: {"repository": v["repository"], "revision": v["revision"]} for k, v in self.lock["sources"].items()},
                         "files": files}

    def compare(self, research, sha=None):
        raw = json.dumps(research).encode()
        lock = copy.deepcopy(self.lock)
        lock["research_lock"]["sha256"] = sha or __import__("hashlib").sha256(raw).hexdigest()
        lock["research_lock"]["files_recorded"] = len(research["files"])
        return cr.compare_research_lock(lock, raw)

    def test_matching_research_lock(self):
        self.assertEqual(self.compare(self.research), [])

    def test_changed_hash_missing_file_wrong_digest_or_revision(self):
        r = copy.deepcopy(self.research)
        r["files"][0]["sha256"] = "b" * 64
        self.assertTrue(any("differs" in e for e in self.compare(r)))
        r = copy.deepcopy(self.research)
        del r["files"][1]
        self.assertTrue(any("not in the research lock" in e for e in self.compare(r)))
        self.assertTrue(any("sha256" in e for e in self.compare(self.research, sha="c" * 64)))
        r = copy.deepcopy(self.research)
        r["sources"]["pm-skills"]["revision"] = "d" * 40
        self.assertTrue(any("revision" in e for e in self.compare(r)))


class FilesDigestTest(unittest.TestCase):
    def test_order_independent_and_content_sensitive(self):
        a = cr.files_digest([("x", "1" * 64), ("y", "2" * 64)])
        self.assertEqual(a, cr.files_digest([("y", "2" * 64), ("x", "1" * 64)]))
        self.assertNotEqual(a, cr.files_digest([("x", "1" * 64), ("y", "3" * 64)]))
        self.assertNotEqual(a, cr.files_digest([("x", "1" * 64)]))


def evidence(**kw):
    e = {"evidence_id": "ev-1", "check_id": "unit", "subject": {"capability_id": "pm.lean-prd"}, "method": "command",
         "environment_ref": "env:scratch-home", "status": "passed", "exit_code": 0,
         "counts": {"passed": 3, "failed": 0, "errors": 0, "skipped": 0}, "classification": "observed", "no_claim": []}
    e.update(kw)
    return e


class EvidenceVerdictTest(unittest.TestCase):
    def verdict(self, **kw):
        return cr.evaluate_check(evidence(**kw))[0]

    def test_only_an_observed_clean_nonzero_run_passes(self):
        self.assertEqual(self.verdict(), "pass")

    def test_skips_zero_counts_and_nonzero_exit_never_pass(self):
        self.assertEqual(self.verdict(counts={"passed": 3, "failed": 0, "errors": 0, "skipped": 1}), "inconclusive")
        self.assertEqual(self.verdict(counts={"passed": 0, "failed": 0, "errors": 0, "skipped": 0}), "inconclusive")
        self.assertEqual(self.verdict(exit_code=1), "fail")
        self.assertEqual(self.verdict(counts={"passed": 3, "failed": 1, "errors": 0, "skipped": 0}), "fail")
        self.assertEqual(self.verdict(counts={"passed": 3, "failed": 0, "errors": 2, "skipped": 0}), "fail")
        self.assertEqual(self.verdict(status="skipped"), "inconclusive")
        self.assertEqual(self.verdict(status="not-run"), "inconclusive")
        self.assertEqual(self.verdict(exit_code=None), "inconclusive")

    def test_fixture_reported_and_simulated_evidence_never_pass(self):
        for cls in ("fixture", "reported", "simulated"):
            with self.subTest(cls=cls):
                self.assertEqual(self.verdict(classification=cls), "inconclusive")


class _Observer:
    name = "test-observer"

    def __init__(self, table, cls="observed"):
        self.table, self.cls = table, cls

    def observe(self, capability_id, dimension):
        state = self.table.get(dimension)
        return None if state is None else cr.Observation(state=state, basis=f"{self.name}:{dimension}", evidence_class=self.cls)


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.reg, _ = shipped()

    def ready(self, cid, **kw):
        return cr.evaluate_readiness(cap(self.reg, cid), repo=REPO, **kw)

    def test_dimensions_are_the_eight_requested_ones(self):
        self.assertEqual(cr.DIMENSIONS, ("defined", "installed", "learned_current", "tools_available", "authorized",
                                         "runtime_accepted", "executed", "outcome_verified"))

    def test_planned_capability_is_not_defined_and_everything_else_is_unknown(self):
        r = self.ready("pm.positioning")
        self.assertEqual(r["dimensions"]["defined"]["state"], "no")
        self.assertIn("planned", r["dimensions"]["defined"]["basis"])
        self.assertEqual(r["dimensions"]["learned_current"]["state"], "not_applicable")
        for dim in ("installed", "tools_available", "authorized", "runtime_accepted", "executed", "outcome_verified"):
            self.assertEqual(r["dimensions"][dim]["state"], "unknown", dim)
        self.assertFalse(r["executable_now"])

    def test_existing_path_is_defined_by_digest_but_not_installed_from_files(self):
        r = self.ready("existing.engineering-path")
        self.assertEqual(r["dimensions"]["defined"]["state"], "yes")
        self.assertEqual(r["dimensions"]["installed"]["state"], "unknown")
        self.assertFalse(r["executable_now"])

    def test_changed_implementation_file_makes_defined_no(self):
        c = copy.deepcopy(cap(self.reg, "existing.engineering-path"))
        c["implementation"]["digest"] = "0" * 64
        self.assertEqual(cr.evaluate_readiness(c, repo=REPO)["dimensions"]["defined"]["state"], "no")

    def test_reported_evidence_is_labeled_reported_never_yes(self):
        rep = [evidence(evidence_id="ev-inst", subject={"capability_id": "existing.engineering-path"}, dimension="installed"),
               evidence(evidence_id="ev-acc", subject={"capability_id": "existing.engineering-path"}, dimension="runtime_accepted")]
        r = self.ready("existing.engineering-path", reported=rep)
        self.assertEqual(r["dimensions"]["installed"]["state"], "reported")
        self.assertEqual(r["dimensions"]["runtime_accepted"]["state"], "reported")
        self.assertEqual(r["dimensions"]["installed"]["evidence"], ["ev-inst"])
        self.assertFalse(r["executable_now"])

    def test_fixture_observations_never_become_yes(self):
        obs = _Observer({"installed": "yes", "tools_available": "yes"}, cls="fixture")
        r = self.ready("existing.engineering-path", observer=obs)
        self.assertEqual(r["dimensions"]["installed"]["state"], "fixture")
        self.assertEqual(r["dimensions"]["tools_available"]["state"], "fixture")

    def test_protected_dimensions_need_an_accepted_protected_observer(self):
        obs = _Observer({d: "yes" for d in cr.DIMENSIONS if d != "defined"})
        r = self.ready("existing.engineering-path", observer=obs)
        self.assertEqual(r["dimensions"]["installed"]["state"], "yes")
        for dim in ("authorized", "runtime_accepted"):
            self.assertEqual(r["dimensions"][dim]["state"], "unknown", dim)
            self.assertIn("protected", r["dimensions"][dim]["basis"])
        # executed/outcome_verified cannot stand on unknown protected prerequisites
        self.assertEqual(r["dimensions"]["executed"]["state"], "inconsistent")
        self.assertEqual(r["dimensions"]["outcome_verified"]["state"], "inconsistent")
        self.assertFalse(r["executable_now"])

    def test_no_dimension_promotes_past_its_prerequisite(self):
        obs = _Observer({"outcome_verified": "yes"})
        r = self.ready("existing.engineering-path", observer=obs)
        self.assertEqual(r["dimensions"]["outcome_verified"]["state"], "inconsistent")
        self.assertIn("executed", r["dimensions"]["outcome_verified"]["basis"])

    def test_trusted_protected_observer_is_a_code_extension_point_empty_by_default(self):
        self.assertEqual(cr.TRUSTED_PROTECTED_OBSERVERS, frozenset())
        obs = _Observer({d: "yes" for d in cr.DIMENSIONS if d != "defined"})
        saved = cr.TRUSTED_PROTECTED_OBSERVERS
        try:
            cr.TRUSTED_PROTECTED_OBSERVERS = frozenset({_Observer})
            r = self.ready("existing.engineering-path", observer=obs)
        finally:
            cr.TRUSTED_PROTECTED_OBSERVERS = saved
        self.assertEqual({d: v["state"] for d, v in r["dimensions"].items()},
                         {"defined": "yes", "installed": "yes", "learned_current": "not_applicable", "tools_available": "yes",
                          "authorized": "yes", "runtime_accepted": "yes", "executed": "yes", "outcome_verified": "yes"})
        self.assertTrue(r["executable_now"])

    def test_learned_knowledge_is_its_own_dimension(self):
        r = self.ready("existing.marketing-team", observer=_Observer({"installed": "yes"}))
        self.assertEqual(r["dimensions"]["learned_current"]["state"], "unknown")


def result(**kw):
    reg, _ = shipped()
    c = cap(reg, "pm.competitor-research")
    r = {"schema_version": 1, "capability_id": c["id"], "capability_version": c["version"], "source_digest": c["source"]["content_digest"],
         "implementation_digest": c["implementation"]["digest"], "mode": "scan", "task_id": "t-1", "status": "partial", "summary": "Synthetic comparison.",
         "artifacts": [], "sources": [
             {"source_id": "s1", "kind": "primary-web", "ref": "https://example.invalid/pricing", "retrieved_at": "2026-11-04T12:00:00Z"},
             {"source_id": "s2", "kind": "synthetic-fixture", "ref": "fixture:competitor-b"}],
         "claims": [{"claim_id": "c1", "text": "Plan A costs 10 per seat.", "classification": "observed", "source_ids": ["s1"]},
                    {"claim_id": "c2", "text": "Competitor B targets SMBs.", "classification": "synthetic", "source_ids": ["s2"]}],
         "checks": [], "assumptions": [], "unknowns": ["Competitor C pricing page unavailable."], "decisions": [],
         "approval_requests": [], "execution_receipt_ref": None, "limitations": []}
    r.update(kw)
    return r


class ResultValidationTest(unittest.TestCase):
    def setUp(self):
        self.reg, _ = shipped()

    def errs(self, r):
        return cr.validate_result(r, self.reg)

    def assertError(self, pattern, r):
        e = self.errs(r)
        self.assertTrue(any(__import__("re").search(pattern, x) for x in e), f"{pattern!r} not in {e}")

    def test_valid_partial_result(self):
        self.assertEqual(self.errs(result()), [])

    def test_observed_claims_need_primary_cited_sources(self):
        r = result()
        r["claims"][0]["source_ids"] = []
        self.assertError("observed claim c1 cites no", r)
        r = result()
        r["claims"][0]["source_ids"] = ["s2"]
        self.assertError("synthetic", r)
        r = result()
        r["claims"][0]["source_ids"] = ["nope"]
        self.assertError("unknown source", r)
        r = result()
        del r["sources"][0]["retrieved_at"]
        self.assertError("retrieved_at", r)

    def test_completed_needs_passing_observed_checks_and_no_pending_approval(self):
        self.assertError("no checks", result(status="completed"))
        self.assertError("not a pass", result(status="completed", checks=[evidence(subject={"capability_id": "pm.competitor-research"},
                                                                                   counts={"passed": 0, "failed": 0, "errors": 0, "skipped": 0})]))
        ok = evidence(subject={"capability_id": "pm.competitor-research"})
        self.assertEqual(self.errs(result(status="completed", checks=[ok])), [])
        self.assertError("pending approval", result(status="completed", checks=[ok], approval_requests=[
            {"action": "publication", "target_ref": "channel:x", "reason": "post"}]))

    def test_result_must_match_the_registry_record(self):
        self.assertError("unknown capability", result(capability_id="pm.nope"))
        self.assertError("source_digest", result(source_digest="0" * 64))
        self.assertError("mode", result(mode="deep-dive"))
        self.assertError("schema", result(granted_tools=["web.public-read"]))


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.reg, _ = shipped()
        self.companies = [cr.load_json(p) for p in sorted((REPO / "hermes/companies/examples").glob("*.json"))]
        self.projects = [cr.load_json(p) for p in sorted((REPO / "hermes/projects/examples").glob("*.json"))]

    def errs(self, companies=None, projects=None):
        return cr.validate_manifests(companies if companies is not None else self.companies,
                                     projects if projects is not None else self.projects, self.reg, examples=True)

    def assertError(self, pattern, **kw):
        e = self.errs(**kw)
        self.assertTrue(any(__import__("re").search(pattern, x) for x in e), f"{pattern!r} not in {e}")

    def test_two_synthetic_tenants_validate(self):
        self.assertEqual(self.errs(), [])
        self.assertEqual(sorted(c["company_id"] for c in self.companies), ["example-a", "example-b"])

    def test_every_boundary_is_reported_not_enforced(self):
        report = cr.company_boundary_report(self.companies[0])
        self.assertEqual(sorted(report), sorted(["repositories", "documents", "memory", "connected_apps", "browser_sessions", "credentials",
                                                 "outputs", "network", "model_inference", "cross_company_sharing"]))
        self.assertTrue(all(v["status"] == "not-enforced" for v in report.values()))

    def test_manifest_cannot_claim_enforcement_or_carry_secrets(self):
        c = copy.deepcopy(self.companies)
        c[0]["boundaries"]["memory"]["status"] = "enforced"
        self.assertError("schema", companies=c)
        c = copy.deepcopy(self.companies)
        c[0]["boundary_accepted"] = True
        self.assertError("schema", companies=c)
        c = copy.deepcopy(self.companies)
        c[0]["service_account_refs"] = ["ghp_abcdefghijklmnopqrstuvwxyz0123456789"]
        self.assertError("schema", companies=c)
        c = copy.deepcopy(self.companies)
        c[0]["default_egress"] = "allow"
        self.assertError("schema", companies=c)

    def test_cross_company_references_and_scopes_are_rejected(self):
        c = copy.deepcopy(self.companies)
        c[0]["memory_scope"] = "company/example-b"
        self.assertError("memory_scope", companies=c)
        c = copy.deepcopy(self.companies)
        c[0]["boundaries"]["documents"]["policy_ref"] = "policy:example-b-documents"
        self.assertError("cross-company", companies=c)

    def test_project_must_belong_to_a_listing_company_and_name_known_capabilities(self):
        p = copy.deepcopy(self.projects)
        p[0]["company_id"] = "example-z"
        self.assertError("unknown company", projects=p)
        p = copy.deepcopy(self.projects)
        p[0]["approved_capability_ids"].append("pm.nope")
        self.assertError("unknown capability", projects=p)
        c = copy.deepcopy(self.companies)
        c[0]["project_ids"] = []
        self.assertError("does not list", companies=c)

    def test_examples_must_be_marked_synthetic(self):
        c = copy.deepcopy(self.companies)
        c[0]["synthetic"] = False
        self.assertError("synthetic", companies=c)


if __name__ == "__main__":
    unittest.main()
