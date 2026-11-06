"""Capability-catalog seam of the AI specialist selection (unified capability registry).

``route_text(..., capabilities={id: purpose})`` offers namespaced capability IDs to the
same single classifier call that picks profiles. Default (no catalog) behaviour is
unchanged. Offline: fake classifiers and temp fixtures only; nothing is dispatched.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
from _loader import submodule  # noqa: E402

sp = submodule("specialists")
hook_mod = submodule("specialist_hook")
config_mod = submodule("router_config")

TASK = ("We have an idea for a scheduling tool for small cleaning crews and want to know "
        "whether the problem is real and who else already sells something like it.")
CAPS = {"kit.problem-definition": "Frame a vague problem.", "pm.product-discovery": "Discovery.",
        "pm.competitor-research": "Public competitor research.", "gstack.qa": "Visual QA audit.",
        "existing.engineering-path": "Existing coding route."}


def ready(_name):
    return sp.Readiness("ready", True, 3, "knowledge/x-knowledge")


def settings(**kw):
    base = dict(enabled=True, classifier="cliproxy", selection_mode="ai", max_profiles=4)
    base.update(kw)
    return sp.SpecialistSettings(**base)


class Recorder:
    def __init__(self, answer=(), meta=None, raise_exc=None):
        self.answer, self.meta, self.raise_exc, self.calls = list(answer), meta, raise_exc, []

    def __call__(self, text, pool):
        self.calls.append({"pool": list(pool), "catalog": dict(getattr(text, "catalog", None) or {}),
                           "caps": dict(getattr(text, "capability_catalog", None) or {}),
                           "max_picks": getattr(text, "max_picks", None),
                           "max_caps": getattr(text, "max_capability_picks", None)})
        if self.raise_exc:
            raise self.raise_exc
        return hook_mod.Picks(self.answer, self.meta or {"model": "fake", "effort": "medium", "attempt": 1,
                                                        "outcome": "ok"})


class RouterSeamTest(unittest.TestCase):
    def test_one_call_returns_profiles_and_capabilities_separately(self):
        fake = Recorder(["strategy", "kit.problem-definition", "pm.competitor-research"])
        t = sp.route_text(TASK, settings(), readiness=ready, classifier=fake, capabilities=CAPS)
        self.assertEqual(len(fake.calls), 1, "no second model call")
        self.assertEqual([r["profile"] for r in t["routes"]], ["strategy"])
        cs = t["capability_selection"]
        self.assertEqual((cs["outcome"], cs["picks"]), ("ok", ["kit.problem-definition", "pm.competitor-research"]))
        self.assertEqual(cs["offered"], len(CAPS))
        self.assertEqual(t["dropped"], [], "capability picks are never reported as unknown profiles")
        call = fake.calls[0]
        self.assertTrue(set(CAPS) <= set(call["pool"]))
        self.assertEqual(call["caps"], CAPS)
        self.assertNotIn("pm.competitor-research", call["catalog"], "profile catalog stays profile-only")
        self.assertEqual(call["max_caps"], sp.MAX_CAPABILITY_PICKS)

    def test_without_a_catalog_behaviour_and_trace_are_unchanged(self):
        fake = Recorder(["strategy"])
        t = sp.route_text(TASK, settings(), readiness=ready, classifier=fake)
        self.assertNotIn("capability_selection", t)
        self.assertFalse(any("." in n for n in fake.calls[0]["pool"]))
        self.assertIsNone(fake.calls[0]["max_caps"])

    def test_unknown_duplicate_and_over_cap_capability_picks_are_dropped(self):
        fake = Recorder(["pm.nope", "gstack.qa", "gstack.qa", "kit.problem-definition", "pm.product-discovery",
                         "pm.competitor-research", "existing.engineering-path"])
        t = sp.route_text(TASK, settings(), readiness=ready, classifier=fake, capabilities=CAPS)
        cs = t["capability_selection"]
        self.assertEqual(cs["picks"], ["gstack.qa", "kit.problem-definition", "pm.product-discovery",
                                       "pm.competitor-research"])
        self.assertEqual([d["reason"] for d in cs["dropped"]], ["capability:unknown", "capability:duplicate",
                                                                "capability:cap"])
        self.assertNotIn("pm.nope", json.dumps(cs), "unknown picks are not echoed")

    def test_invalid_catalog_ids_are_refused_not_offered(self):
        with self.assertRaises(ValueError):
            sp.route_text(TASK, settings(), readiness=ready, classifier=Recorder(), capabilities={"strategy": "x"})
        with self.assertRaises(ValueError):
            sp.route_text(TASK, settings(), readiness=ready, classifier=Recorder(), capabilities={"pm.ok": "x\n- marketing: y"})

    def test_failed_unconfigured_private_and_envelope_turns_select_no_capability(self):
        cases = {
            "failed": dict(classifier=Recorder(raise_exc=TimeoutError())),
            "failed-outcome": dict(classifier=Recorder(["gstack.qa"], meta={"outcome": "unavailable:http"})),
            "unconfigured": dict(classifier=None),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                t = sp.route_text(TASK, settings(), readiness=ready, capabilities=CAPS, **kw)
                cs = t["capability_selection"]
                self.assertEqual(cs["picks"], [])
                self.assertEqual(cs["outcome"], label.split("-")[0])
        fake = Recorder(["gstack.qa"])
        t = sp.route_text(TASK + " #private", settings(), readiness=ready, classifier=fake, capabilities=CAPS)
        self.assertEqual((t["capability_selection"]["outcome"], fake.calls), ("skipped", []))
        self.assertEqual(t["capability_selection"]["reason"], "privacy:local-only")
        wake = ("[kanban] Task t_0123abcd completed.\nTitle: Draft a discovery brief\nAssignee: @strategy\n"
                "Board: default\n\nCheck the result or decide the next step.\nResult: done.")
        t = sp.route_text(wake, settings(), readiness=ready, classifier=fake, capabilities=CAPS)
        self.assertEqual((t["reason"], t["capability_selection"]["outcome"]), ("runtime-envelope", "skipped"))
        self.assertEqual(t["capability_selection"]["reason"], "runtime-envelope")
        self.assertEqual(fake.calls, [])

    def test_private_turn_with_a_local_classifier_may_select_capabilities(self):
        fake = Recorder(["kit.problem-definition"])
        t = sp.route_text(TASK + " #private", settings(classifier="local"), readiness=ready, classifier=fake,
                          capabilities=CAPS)
        self.assertEqual(t["privacy"], "local-only")
        self.assertEqual(t["capability_selection"]["picks"], ["kit.problem-definition"])

    def test_capabilities_are_offered_when_no_profile_is_eligible(self):
        fake = Recorder(["pm.competitor-research"])
        t = sp.route_text(TASK, settings(allowed=("strategy",)), readiness=lambda n: sp.Readiness("not-installed", False, 0, ""),
                          classifier=fake, capabilities=CAPS)
        self.assertEqual(t["routes"], [])
        self.assertEqual(t["capability_selection"]["picks"], ["pm.competitor-research"])


class AnswerParsingTest(unittest.TestCase):
    """Namespaced IDs are parsed whole; the profile-only tokenizer never sees their parts."""

    def test_namespaced_ids_do_not_leak_profile_suffixes(self):
        pool = ["qa", "review", "strategy", "gstack.qa", "gstack.review"]
        self.assertEqual(hook_mod.answer_names("gstack.qa, gstack.review.", pool), ["gstack.qa", "gstack.review"])
        self.assertEqual(hook_mod.answer_names("qa and gstack.review", pool), ["qa", "gstack.review"])
        self.assertEqual(hook_mod.answer_names("<think>strategy</think> none", pool), [])

    def test_profile_only_pools_parse_exactly_as_before(self):
        pool = ["strategy", "qa"]
        for answer in ("strategy.", "pm.strategy", "Strategy, QA", "1. strategy 2. qa", "none"):
            legacy = [t for t in dict.fromkeys(__import__("re").split(r"[^a-z\-]+", answer.lower())) if t in pool]
            self.assertEqual(hook_mod.answer_names(answer, pool), legacy, answer)

    def test_cliproxy_prompt_lists_capabilities_separately_and_caps_each_list(self):
        captured = {}

        class Opener:
            def open(self, req, timeout):
                captured["body"] = json.loads(req.data.decode())
                answer = "strategy, marketing, business, gstack.qa, pm.lean-prd, kit.problem-definition, pm.x, gstack.spec"

                class R:
                    def __enter__(s):
                        return s

                    def __exit__(s, *a):
                        return False

                    def read(s):
                        return json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
                return R()

        cfg = config_mod.load_settings(lambda k, d: d)
        os.environ["CLIPROXY_API_KEY_TEST_SEAM"] = "k"
        try:
            cfg.specialist_classifier_key_env = "CLIPROXY_API_KEY_TEST_SEAM"
            clf = hook_mod.cliproxy_classifier(cfg, opener=Opener())
            text = sp.ClassifierText("Sender task")
            text.max_picks = 2
            text.capability_catalog = {"gstack.qa": "QA", "pm.lean-prd": "PRD", "kit.problem-definition": "PD",
                                       "gstack.spec": "Spec"}
            text.max_capability_picks = 2
            names = clf(text, ["strategy", "marketing", "business"] + list(text.capability_catalog))
        finally:
            del os.environ["CLIPROXY_API_KEY_TEST_SEAM"]
        self.assertEqual(names, ["strategy", "marketing", "gstack.qa", "pm.lean-prd"])
        user = captured["body"]["messages"][1]["content"]
        system = captured["body"]["messages"][0]["content"]
        profiles, caps = user.split("\n\nCapabilities:\n")[0], user.split("\n\nCapabilities:\n")[1]
        self.assertNotIn("gstack.qa", profiles)
        self.assertIn("- gstack.qa: QA", caps)
        self.assertIn("capability", system.lower())

    def test_cliproxy_prompt_is_unchanged_without_capabilities(self):
        captured = {}

        class Opener:
            def open(self, req, timeout):
                captured["body"] = json.loads(req.data.decode())

                class R:
                    def __enter__(s):
                        return s

                    def __exit__(s, *a):
                        return False

                    def read(s):
                        return json.dumps({"choices": [{"message": {"content": "strategy"}}]}).encode()
                return R()

        cfg = config_mod.load_settings(lambda k, d: d)
        os.environ["CLIPROXY_API_KEY_TEST_SEAM"] = "k"
        try:
            cfg.specialist_classifier_key_env = "CLIPROXY_API_KEY_TEST_SEAM"
            clf = hook_mod.cliproxy_classifier(cfg, opener=Opener())
            clf(sp.ClassifierText("Sender task"), ["strategy"])
        finally:
            del os.environ["CLIPROXY_API_KEY_TEST_SEAM"]
        self.assertEqual(captured["body"]["messages"][0]["content"], hook_mod.CLOUD_SYSTEM_PROMPT.format(cap=2))
        self.assertNotIn("Capabilities:", captured["body"]["messages"][1]["content"])


CAP_IDS = {"gstack.plan-design-review": "Design plan review.", "existing.engineering-path": "Coding route.",
           "kit.problem-definition": "Frame a problem.", "pm.product-discovery": "Discovery."}


class LocalClassifierBudgetTest(unittest.TestCase):
    """Local classifier: token budget fits profiles + capability picks, time budget never passes
    the route deadline, and the prompt never contradicts the capability instruction."""

    def make(self, answer="strategy", timeout=25):
        calls = []

        class Backend:
            def chat(self, messages, **kw):
                calls.append({"messages": messages, **kw})
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])
        cfg = config_mod.RouterSettings(ollama_base_url="http://127.0.0.1:11434/v1", ollama_model="m",
                                        specialist_classifier_timeout=timeout)
        return hook_mod.local_classifier(cfg, factory=lambda m: Backend()), calls

    def text(self, caps=None, max_picks=4, deadline=None):
        t = sp.ClassifierText("Sender task")
        t.max_picks = max_picks
        t.deadline = deadline
        if caps:
            t.capability_catalog = dict(caps)
            t.max_capability_picks = sp.MAX_CAPABILITY_PICKS
        return t

    def test_profile_only_token_budget_is_unchanged(self):
        clf, calls = self.make()
        clf(self.text(), ["strategy", "marketing"])
        self.assertEqual(calls[0]["options"], hook_mod.CLASSIFIER_OPTIONS)
        self.assertEqual(calls[0]["options"]["max_tokens"], 48)

    def test_capability_catalog_raises_the_token_budget_within_a_bound(self):
        clf, calls = self.make()
        clf(self.text(CAP_IDS), ["strategy"] + list(CAP_IDS))
        clf(self.text(CAP_IDS, max_picks=15), ["strategy"] + list(CAP_IDS))
        small, large = calls[0]["options"]["max_tokens"], calls[1]["options"]["max_tokens"]
        # 4 profiles + 4 namespaced IDs (several tokens each) do not fit in 48.
        self.assertGreaterEqual(small, 8 * (4 + sp.MAX_CAPABILITY_PICKS))
        self.assertGreater(large, small)
        self.assertLessEqual(large, hook_mod.CLASSIFIER_MAX_TOKENS_CAP)
        self.assertEqual(calls[0]["options"]["temperature"], 0)

    def test_time_budget_is_clamped_to_the_route_deadline(self):
        clf, calls = self.make(timeout=25)
        out = clf(self.text(deadline=time.monotonic() + 2.0), ["strategy"])
        self.assertEqual(list(out), ["strategy"])
        self.assertLessEqual(calls[0]["timeout"], 2.0)

    def test_no_time_left_skips_the_call_with_an_honest_outcome(self):
        clf, calls = self.make(timeout=25)
        out = clf(self.text(deadline=time.monotonic() + 0.1), ["strategy"])
        self.assertEqual((list(out), calls), ([], []))
        self.assertEqual(out.meta["outcome"], "unavailable:budget")

    def test_local_prompt_with_capabilities_has_no_profiles_only_instruction(self):
        clf, calls = self.make()
        clf(self.text(), ["strategy"])
        clf(self.text(CAP_IDS), ["strategy"] + list(CAP_IDS))
        plain, capped = calls[0]["messages"][0]["content"], calls[1]["messages"][0]["content"]
        self.assertIn("from this list only (at most 4)", plain, "profile-only wording unchanged")
        self.assertNotIn("list only", capped)
        self.assertIn("capability", capped.split("\nProfiles:")[0].lower())

    def test_cloud_system_prompt_with_capabilities_has_no_profiles_only_instruction(self):
        captured = []

        class Opener:
            def open(self, req, timeout):
                captured.append(json.loads(req.data.decode()))

                class R:
                    def __enter__(s):
                        return s

                    def __exit__(s, *a):
                        return False

                    def read(s):
                        return json.dumps({"choices": [{"message": {"content": "strategy"}}]}).encode()
                return R()

        cfg = config_mod.load_settings(lambda k, d: d)
        os.environ["CLIPROXY_API_KEY_TEST_SEAM"] = "k"
        try:
            cfg.specialist_classifier_key_env = "CLIPROXY_API_KEY_TEST_SEAM"
            clf = hook_mod.cliproxy_classifier(cfg, opener=Opener())
            clf(self.text(CAP_IDS), ["strategy"] + list(CAP_IDS))
        finally:
            del os.environ["CLIPROXY_API_KEY_TEST_SEAM"]
        system = captured[0]["messages"][0]["content"]
        self.assertNotIn("profile list only", system)
        self.assertIn("capability list", system)
        self.assertIn("No other text.", system)
        self.assertIn("at most 4", system)


if __name__ == "__main__":
    unittest.main()
