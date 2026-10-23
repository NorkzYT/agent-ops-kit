"""AI-only specialist selection (``specialist_selection_mode: ai``) and the dynamic
profile catalog. Offline: the classifier is a fake, readiness runs against temp
fixtures, nothing is dispatched and no gateway is touched.

Run: python3 -m unittest discover -s scripts/tests -p 'test_specialist*.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _loader import REPO, submodule  # noqa: E402

sp = submodule("specialists")
hook_mod = submodule("specialist_hook")
config_mod = submodule("router_config")

MARKETING = "Write a marketing campaign and positioning for the launch of our new course."
NEUTRAL = ("I keep going back and forth on what to do next with my life and the people "
           "around me, and I would like a structured way to think it through.")


def ready(_name):
    return sp.Readiness("ready", True, 3, "knowledge/x-knowledge")


def settings(**kw):
    base = dict(enabled=True, classifier="cliproxy", selection_mode="ai", max_profiles=6)
    base.update(kw)
    return sp.SpecialistSettings(**base)


def names(trace):
    return [r["profile"] for r in trace["routes"]]


class Recorder:
    def __init__(self, answer=(), meta=None, raise_exc=None):
        self.answer, self.meta, self.raise_exc, self.calls = list(answer), meta, raise_exc, []

    def __call__(self, text, pool):
        self.calls.append({"text": str(text), "pool": list(pool), "catalog": dict(getattr(text, "catalog", {}) or {}),
                           "max_picks": getattr(text, "max_picks", None)})
        if self.raise_exc:
            raise self.raise_exc
        return hook_mod.Picks(self.answer, self.meta or {"model": "fake", "effort": "medium", "attempt": 1,
                                                        "outcome": "ok"})


class AiModeTest(unittest.TestCase):
    def test_ai_mode_never_scores_keywords_and_only_the_model_selects(self):
        fake = Recorder(["philosophy"])
        t = sp.route_text(MARKETING, settings(), readiness=ready, classifier=fake)
        self.assertEqual(t["selection_mode"], "ai")
        self.assertEqual(names(t), ["philosophy"])          # not marketing: no keyword candidate at all
        self.assertEqual(t["routes"][0]["reasons"], ["ai"])
        self.assertEqual(t["classifier"], "used")
        self.assertEqual(t["classifier_detail"]["outcome"], "ok")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("marketing", fake.calls[0]["pool"])    # the whole eligible catalog is offered
        self.assertEqual(fake.calls[0]["max_picks"], 6)
        self.assertNotIn("kw:", json.dumps(t))

    def test_model_may_select_high_risk_profiles_which_stay_advisory_short(self):
        fake = Recorder(["red-team", "security-reviewer", "health-fitness", "herbalism"])
        t = sp.route_text(NEUTRAL, settings(), readiness=ready, classifier=fake)
        self.assertEqual(names(t), ["red-team", "security-reviewer", "health-fitness", "herbalism"])
        for r in t["routes"]:
            self.assertTrue(r["high_risk"] and r["advisory_only"])
            self.assertEqual((r["execution"], r["toolsets"]), ("short", sp.ADVISORY_TOOLSETS))
            self.assertIn("no external actions", r["constraint"])
            self.assertEqual(r["knowledge"]["status"], "ready")
        self.assertIn("HIGH-RISK", sp.render_plan(t))
        self.assertIn("AI-selected", sp.render_plan(t))
        self.assertIn("-t skills", sp.render_plan(t))

    def test_cap_is_max_profiles_not_two(self):
        picks = ["strategy", "business", "marketing", "philosophy", "survival", "martial-arts", "offers-sales"]
        t = sp.route_text(NEUTRAL, settings(max_profiles=6), readiness=ready, classifier=Recorder(picks))
        self.assertEqual(names(t), picks[:6])
        self.assertIn({"profile": "offers-sales", "reason": "cap"}, t["dropped"])
        t3 = sp.route_text(NEUTRAL, settings(max_profiles=3), readiness=ready, classifier=Recorder(picks))
        self.assertEqual(len(names(t3)), 3)

    def test_unknown_disabled_not_installed_private_and_duplicate_outputs_are_rejected(self):
        def rdy(name):
            return sp.Readiness("not-installed") if name == "survival" else ready(name)

        fake = Recorder(["ghost", "strategy", "strategy", "survival", "business", "Red-Team "])
        t = sp.route_text(NEUTRAL, settings(allowed=("strategy", "survival", "red-team")), readiness=rdy,
                          classifier=fake)
        self.assertEqual(names(t), ["strategy", "red-team"])
        self.assertNotIn("survival", fake.calls[0]["pool"])          # readiness filtered before the call
        self.assertNotIn("business", fake.calls[0]["pool"])          # allowlist filtered before the call
        drops = {(d["profile"], d["reason"]) for d in t["dropped"]}
        self.assertIn(("?", "classifier:unknown"), drops)
        self.assertIn(("strategy", "classifier:duplicate"), drops)
        self.assertIn(("survival", "readiness:not-installed"), drops)
        self.assertIn(("business", "classifier:not-eligible"), drops)

    def test_require_ready_filters_the_catalog(self):
        def rdy(name):
            return sp.Readiness("needs-learn", True, 2) if name != "philosophy" else ready(name)

        fake = Recorder(["philosophy", "strategy"])
        t = sp.route_text(NEUTRAL, settings(require_ready=True), readiness=rdy, classifier=fake)
        self.assertEqual(fake.calls[0]["pool"], ["philosophy"])
        self.assertEqual(names(t), ["philosophy"])
        self.assertIn({"profile": "strategy", "reason": "readiness:needs-learn"}, t["dropped"])

    def test_failure_timeout_or_invalid_output_selects_none_content_free(self):
        boom = Recorder(raise_exc=RuntimeError("down"))
        t = sp.route_text(MARKETING + " Quuxbarian", settings(), readiness=ready, classifier=boom)
        self.assertEqual(t["routes"], [])
        self.assertEqual(t["classifier"], "error:RuntimeError")
        self.assertEqual(t["reason"], "classifier-failed")
        slow = Recorder([], {"model": "gpt-5.6-terra", "effort": "medium", "attempt": 1, "outcome": "unavailable:timeout"})
        t = sp.route_text(MARKETING + " Quuxbarian", settings(), readiness=ready, classifier=slow)
        self.assertEqual((t["routes"], t["reason"]), ([], "classifier-failed"))
        self.assertEqual(t["classifier_detail"]["outcome"], "unavailable:timeout")
        junk = Recorder(["<<tags>>", "none"])
        t = sp.route_text(MARKETING + " Quuxbarian", settings(), readiness=ready, classifier=junk)
        self.assertEqual((t["routes"], t["reason"]), ([], "all-dropped"))
        self.assertEqual(sp.render_plan(t), "")
        for trace in (t,):
            self.assertNotIn("Quuxbarian", json.dumps(trace))

    def test_no_classifier_configured_selects_none_and_never_falls_back_to_keywords(self):
        t = sp.route_text(MARKETING, settings(classifier="none"), readiness=ready, classifier=None)
        self.assertEqual((t["routes"], t["classifier"], t["reason"]), ([], "unconfigured", "classifier-unconfigured"))
        t = sp.route_text(MARKETING, settings(classifier="cliproxy"), readiness=ready, classifier=None)
        self.assertEqual((t["routes"], t["classifier"]), ([], "unconfigured"))

    def test_private_task_never_calls_cliproxy_and_fails_closed(self):
        fake = Recorder(["philosophy"])
        t = sp.route_text("#privacy " + NEUTRAL, settings(classifier="cliproxy"), readiness=ready, classifier=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(t["privacy"], "local-only")
        self.assertEqual(t["classifier"], "skipped:private")
        self.assertEqual(t["routes"], [])
        # a local classifier may run, on the local-eligible pool only (none today -> nothing to ask)
        local = Recorder(["philosophy"])
        t = sp.route_text("#privacy " + NEUTRAL, settings(classifier="local"), readiness=ready, classifier=local)
        self.assertEqual(local.calls, [])
        self.assertEqual(t["classifier"], "skipped:empty-pool")
        self.assertEqual(t["routes"], [])

    def test_status_continuation_and_non_substantive_turns_do_not_call_the_model(self):
        fake = Recorder(["philosophy"])
        for text in ("thanks", "any update on that?", "hi"):
            t = sp.route_text(text, settings(), readiness=ready, classifier=fake)
            self.assertEqual(t["routes"], [], text)
        self.assertEqual(fake.calls, [])

    def test_hybrid_and_keywords_modes_are_unchanged(self):
        called = Recorder(["philosophy"])
        t = sp.route_text(MARKETING, settings(selection_mode="hybrid"), readiness=ready, classifier=called)
        self.assertEqual((names(t), called.calls, t["classifier"]), (["marketing"], [], "not-needed"))
        t = sp.route_text(NEUTRAL, settings(selection_mode="hybrid"), readiness=ready, classifier=called)
        self.assertEqual(names(t), ["philosophy"])
        kw = sp.route_text(NEUTRAL, settings(selection_mode="keywords"), readiness=ready, classifier=Recorder(["philosophy"]))
        self.assertEqual((kw["routes"], kw["classifier"], kw["selection_mode"]), ([], "off", "keywords"))
        kw = sp.route_text(MARKETING, settings(selection_mode="keywords"), readiness=ready, classifier=None)
        self.assertEqual(names(kw), ["marketing"])


class DynamicCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ai-catalog-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = self.tmp / "hermes" / "profiles"
        self.seeds = self.tmp / "seeds"
        # installed, book-backed, with a spec in the seed assignments
        self.install("cloud-security", knowledge=True, soul="# SOUL.md\n\nYou are **Cloud Security**.\n")
        self.seed("cloud-security", {"purpose": "Cloud security posture, IAM and logging review.",
                                     "in_scope": ["IAM"], "out_of_scope": [], "parent": "none",
                                     "overlap": [], "boundaries": ["advice only"]})
        # installed, SOUL only (knowledge-apply renders a Scope section)
        self.install("psychology", knowledge=True, soul="# SOUL.md\n\nYou are **Psychology**.\n\n## Scope\n\n"
                     "Purpose: Psychological theories, personality and self-understanding.\n\n## Knowledge\n")
        # installed, not book-backed (no knowledge folder, no seed): never in the catalog
        self.install("windows-operator", knowledge=False, soul="# SOUL.md\n\nDesktop operator.\n")
        # a registry profile installed too
        self.install("strategy", knowledge=True, soul="# SOUL.md\n")
        (self.root / "_template").mkdir()
        (self.root / ".hidden").mkdir()

    def install(self, name, knowledge, soul):
        (self.root / name).mkdir(parents=True)
        (self.root / name / "SOUL.md").write_text(soul)
        if knowledge:
            (self.root / name / "knowledge").mkdir()
            (self.root / name / "knowledge" / "a.md").write_text("x\n")

    def seed(self, name, spec):
        (self.seeds / name).mkdir(parents=True)
        (self.seeds / name / "knowledge-assignments.json").write_text(json.dumps(
            {"version": 1, "profile": name, "assignments": [], "profile_spec": spec}))

    def test_catalog_adds_installed_book_backed_profiles_with_purpose_and_conservative_risk(self):
        cat = sp.catalog(settings(profiles_root=self.root, seeds_root=self.seeds))
        self.assertIn("cloud-security", cat)
        self.assertIn("psychology", cat)
        self.assertNotIn("windows-operator", cat)
        self.assertNotIn("_template", cat)
        self.assertEqual(cat["cloud-security"].purpose, "Cloud security posture, IAM and logging review.")
        self.assertTrue(cat["cloud-security"].high_risk, "security purpose -> high-risk, advisory-only")
        self.assertEqual(cat["psychology"].purpose, "Psychological theories, personality and self-understanding.")
        self.assertTrue(cat["psychology"].high_risk, "mental-health domain is inferred high-risk conservatively")
        self.assertEqual(cat["cloud-security"].signals, ())
        self.assertFalse(cat["cloud-security"].implementer)
        self.assertEqual(cat["strategy"], sp.BY_NAME["strategy"], "registry entries are never replaced")
        explicit = dict(purpose="Gardening tips.", high_risk=True)
        self.seed("gardening", explicit)
        self.install("gardening", True, "# SOUL.md\n")
        self.assertTrue(sp.catalog(settings(profiles_root=self.root, seeds_root=self.seeds))["gardening"].high_risk)
        self.install("cooking", True, "# SOUL.md\n\n## Scope\n\nPurpose: Home cooking and recipes.\n")
        self.assertFalse(sp.catalog(settings(profiles_root=self.root, seeds_root=self.seeds))["cooking"].high_risk)

    def test_ai_mode_offers_dynamic_profiles_and_routes_them_advisory(self):
        fake = Recorder(["cloud-security", "psychology", "strategy"])
        s = settings(profiles_root=self.root, seeds_root=self.seeds)
        t = sp.route_text(NEUTRAL, s, classifier=fake)
        self.assertEqual(fake.calls[0]["catalog"]["cloud-security"], "Cloud security posture, IAM and logging review.")
        self.assertIn("cloud-security", fake.calls[0]["pool"])
        self.assertNotIn("marketing", fake.calls[0]["pool"], "registry profiles that are not installed are not offered")
        self.assertEqual(names(t), ["cloud-security", "psychology", "strategy"])
        cs = t["routes"][0]
        self.assertTrue(cs["high_risk"] and cs["advisory_only"])
        self.assertEqual((cs["execution"], cs["toolsets"]), ("short", "skills"))
        self.assertEqual(cs["knowledge"]["status"], "needs-learn")       # knowledge, no /learn skill yet
        self.assertEqual(t["catalog"], {"profiles": 16, "dynamic": 2, "eligible": 3})

    def test_model_cannot_invent_or_activate_a_profile(self):
        fake = Recorder(["gardening-guru", "cloud-security"])
        t = sp.route_text(NEUTRAL, settings(profiles_root=self.root, seeds_root=self.seeds), classifier=fake)
        self.assertEqual(names(t), ["cloud-security"])
        self.assertIn({"profile": "?", "reason": "classifier:unknown"}, t["dropped"])
        self.assertFalse((self.root / "gardening-guru").exists())

    def test_allowlist_accepts_installed_dynamic_names(self):
        known, unknown = sp.split_allowed(["cloud-security", "strategy", "bogus"], extra_known=("cloud-security",))
        self.assertEqual((known, unknown), (("cloud-security", "strategy"), ("bogus",)))
        self.assertEqual(sp.knowledge_readiness("cloud-security", self.root).status, "needs-learn")
        self.assertEqual(sp.knowledge_readiness("bogus", self.root).status, "unknown-profile")


class ConfigAndHookTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for k in list(os.environ):
            if k.startswith("MODEL_ROUTER_SPECIALIST"):
                del os.environ[k]

    def test_default_mode_is_ai_and_modes_are_validated(self):
        self.assertEqual(config_mod.RouterSettings().specialist_selection_mode, "ai")
        self.assertEqual(config_mod.load_settings(None).specialist_selection_mode, "ai")
        s = config_mod.load_settings(lambda k, d: {"specialist_selection_mode": "Hybrid"}.get(k))
        self.assertEqual(s.specialist_selection_mode, "hybrid")
        s = config_mod.load_settings(lambda k, d: {"specialist_selection_mode": "regex"}.get(k))
        self.assertEqual(s.specialist_selection_mode, "ai", "an unknown mode falls back to the default")
        with mock.patch.dict(os.environ, {"MODEL_ROUTER_SPECIALIST_MODE": "keywords"}):
            self.assertEqual(config_mod.load_settings(None).specialist_selection_mode, "keywords")
        self.assertEqual(hook_mod.specialist_settings(config_mod.RouterSettings()).selection_mode, "ai")

    def test_hook_in_ai_mode_uses_the_classifier_and_logs_mode_without_content(self):
        s = config_mod.RouterSettings(specialist_routing=True, specialist_classifier="cliproxy",
                                      specialist_classifier_base_url="http://127.0.0.1:1/v1")
        with mock.patch.dict(os.environ, {"CLIPROXY_API_KEY": "k"}):
            h = hook_mod.SpecialistHook(s)
        h._classifier = Recorder(["philosophy"])
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)), \
                self.assertLogs("hermes.plugin.model_router", level="INFO") as logs:
            out = h.on_pre_llm_call(session_id="s", user_message=MARKETING + " Quuxbarian")
        self.assertIn("- philosophy:", out["context"])
        self.assertNotIn("- marketing:", out["context"])
        text = "\n".join(logs.output)
        self.assertIn("mode=ai", text)
        self.assertIn("classifier=used", text)
        self.assertNotIn("Quuxbarian", text + out["context"])

    def test_hook_ai_mode_without_classifier_routes_nothing(self):
        h = hook_mod.SpecialistHook(config_mod.RouterSettings(specialist_routing=True))
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)), \
                self.assertLogs("hermes.plugin.model_router", level="INFO") as logs:
            self.assertIsNone(h.on_pre_llm_call(session_id="s", user_message=MARKETING))
        self.assertIn("classifier=unconfigured", "\n".join(logs.output))

    def test_dry_run_command_is_offline_keywords_and_says_so(self):
        h = hook_mod.SpecialistHook(config_mod.RouterSettings(specialist_profiles_root="/nonexistent/profiles"))
        out = json.loads(h.dry_run(MARKETING))
        self.assertEqual(out["selection_mode"], "keywords")
        self.assertEqual(out["live_selection_mode"], "ai")
        self.assertIn({"profile": "marketing", "reason": "readiness:not-installed"}, out["dropped"])

    def test_cliproxy_classifier_offers_catalog_purposes_and_honours_max_picks(self):
        seen = {}

        class Opener:
            def open(self, req, timeout):
                seen["body"] = json.loads(req.data.decode())
                import io
                return io.BytesIO(json.dumps({"choices": [{"message": {"content":
                                   "cloud-security, philosophy, strategy, business"}}]}).encode())

        s = config_mod.RouterSettings(specialist_classifier="cliproxy", specialist_classifier_base_url="http://127.0.0.1:1/v1")
        with mock.patch.dict(os.environ, {"CLIPROXY_API_KEY": "k"}):
            fn = hook_mod.cliproxy_classifier(s, opener=Opener())
        text = sp.ClassifierText(NEUTRAL)
        text.catalog = {"cloud-security": "Cloud security posture review.", "philosophy": "x", "strategy": "y", "business": "z"}
        text.max_picks = 3
        picks = fn(text, ["cloud-security", "philosophy", "strategy", "business"])
        self.assertEqual(list(picks), ["cloud-security", "philosophy", "strategy"])
        user = seen["body"]["messages"][1]["content"]
        self.assertIn("cloud-security: Cloud security posture review.", user)
        self.assertIn("at most 3", seen["body"]["messages"][0]["content"])
        legacy = fn(sp.ClassifierText(NEUTRAL), ["philosophy", "strategy", "business"])
        self.assertEqual(list(legacy), ["philosophy", "strategy"], "without max_picks the hybrid cap of two holds")

    def test_cli_dry_run_defaults_to_keywords_and_takes_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = [sys.executable, str(REPO / "scripts" / "specialist-route.py"), "--profiles-root", tmp]
            proc = subprocess.run(base + [MARKETING], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            trace, _ = json.JSONDecoder().raw_decode(proc.stdout)
            self.assertEqual(trace["selection_mode"], "keywords")
            self.assertIn("offline", proc.stdout)
            proc = subprocess.run(base + ["--mode", "ai", MARKETING], capture_output=True, text=True, timeout=60)
            trace, _ = json.JSONDecoder().raw_decode(proc.stdout)
            self.assertEqual((trace["selection_mode"], trace["classifier"], trace["routes"]), ("ai", "unconfigured", []))
            self.assertIn("specialist-classify-probe", proc.stdout)


if __name__ == "__main__":
    unittest.main()
