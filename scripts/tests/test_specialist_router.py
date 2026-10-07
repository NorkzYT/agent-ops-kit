"""Tests for the specialist-router pilot (model-router ``specialists.py``).

Offline and deterministic: readiness runs against temp-dir fixtures only, the
optional classifier is a fake, and no Hermes runtime or network is involved.

Run: python3 -m unittest discover -s scripts/tests -p 'test_specialist*.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _loader import REPO, load_plugin, submodule  # noqa: E402

sp = submodule("specialists")
hook_mod = submodule("specialist_hook")
config_mod = submodule("router_config")
task_intent = submodule("task_intent")

BOOK_BACKED = (
    "business", "offers-sales", "marketing", "leadership-teamwork", "software-engineering",
    "security-reviewer", "red-team", "negotiation-communication", "health-fitness",
    "herbalism", "survival", "martial-arts", "philosophy", "strategy",
)


def ready(_name):
    return sp.Readiness("ready", True, 3, "knowledge/x-knowledge")


def settings(**kw):
    base = dict(enabled=True)
    base.update(kw)
    return sp.SpecialistSettings(**base)


def route(text, readiness=ready, session_private=False, classifier=None, **kw):
    return sp.route_text(text, settings(**kw), readiness=readiness,
                         session_private=session_private, classifier=classifier)


def names(trace):
    return [r["profile"] for r in trace["routes"]]


class RegistryTest(unittest.TestCase):
    def test_registry_covers_every_book_backed_profile_plus_local_engineer(self):
        self.assertEqual(set(sp.BY_NAME), set(BOOK_BACKED) | {"local-engineer"})
        for name in sp.BY_NAME:
            self.assertTrue((REPO / "hermes" / "profiles" / name / "SOUL.md").is_file(), name)

    def test_high_risk_set(self):
        self.assertEqual(set(sp.HIGH_RISK),
                         {"health-fitness", "herbalism", "security-reviewer", "red-team"})

    def test_each_profile_is_reachable(self):
        samples = {
            "strategy": "Should we pivot? Map our competitive advantage and business model.",
            "business": "Build a financial model with unit economics and our cash flow runway.",
            "offers-sales": "Design an offer stack and pricing for my coaching program.",
            "marketing": "Write positioning and a launch campaign for the new app.",
            "leadership-teamwork": "How do I run one-on-ones with my direct reports?",
            "software-engineering": "Review the architecture and our testing strategy.",
            "security-reviewer": "Do a security review and threat model of the login flow.",
            "red-team": "Scope a red team exercise for our own staging app.",
            "negotiation-communication": "Help me negotiate a counteroffer for the contract.",
            "health-fitness": "Build a strength training plan with protein targets.",
            "herbalism": "What are traditional uses of chamomile and how is a tincture made?",
            "survival": "Prepare an emergency kit and a water purification plan.",
            "martial-arts": "Drill guard passing and armbars for my bjj class.",
            "philosophy": "What do the stoics say about anger? Compare with Nietzsche.",
            "local-engineer": "Implement the parser and add a regression test.",
        }
        self.assertEqual(set(samples), set(sp.BY_NAME))
        for profile, text in samples.items():
            self.assertIn(profile, names(route(text)), text)


class MatchTest(unittest.TestCase):
    def test_single_match(self):
        t = route("Help me design an irresistible offer and pricing for my course.")
        self.assertEqual(names(t), ["offers-sales"])
        r = t["routes"][0]
        self.assertFalse(r["high_risk"])
        self.assertTrue(r["advisory_only"])
        self.assertTrue(r["reasons"] and all(x.startswith("kw:") for x in r["reasons"]))
        self.assertEqual(r["knowledge"]["status"], "ready")

    def test_multi_match_ordered_by_score(self):
        t = route("Write a marketing campaign and positioning for the launch, "
                  "and plan the cold email outreach and sales funnel with pricing.")
        self.assertEqual(set(names(t)), {"marketing", "offers-sales"})
        scores = [r["score"] for r in t["routes"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_match(self):
        t = route("Rename the meeting on Thursday to Friday afternoon please.")
        self.assertEqual(t["routes"], [])
        self.assertEqual(t["reason"], "no-match")

    def test_no_current_task(self):
        t = route("")
        self.assertFalse(t["task_found"])
        self.assertEqual(t["routes"], [])

    def test_status_continuation_and_greeting_do_not_route(self):
        for text in ("What's the status of the marketing campaign?", "continue",
                     "thanks", "Any update on the pricing work?"):
            self.assertEqual(route(text)["routes"], [], text)

    def test_specialist_question_still_routes(self):
        t = route("How should I prepare a salary negotiation with my manager?")
        self.assertEqual(names(t), ["negotiation-communication"])

    def test_execution_modes(self):
        short = route("What do the stoics say about anger?")["routes"][0]
        self.assertEqual(short["execution"], "short")
        self.assertEqual(short["toolsets"], "skills")
        deep = route("Plan a 12-week strategy: compare options and trade-offs for market entry, "
                     "prioritise the roadmap, and propose a business model.")["routes"]
        self.assertEqual(deep[0]["profile"], "strategy")
        self.assertEqual(deep[0]["execution"], "kanban")
        impl = route("Implement the parser and add a regression test.")["routes"][0]
        self.assertEqual((impl["profile"], impl["execution"], impl["advisory_only"]),
                         ("local-engineer", "kanban", False))


class UnderSelectionRegressionTest(unittest.TestCase):
    """Live dry runs under-selected on tasks that name a profile's own domain.

    Root causes: the domain nouns "strategy" and "offer" were weak-only, the
    weak security pattern ``auth`` never matched "authentication" (word
    boundary), "authentication design" was not recognised as system design, and
    negotiation-communication had no signal for drafting/rewriting a message.
    """

    def test_cross_functional_plan_routes_each_named_commerce_domain(self):
        t = route("Revise the strategy, marketing, offer, and execution plan for Kairo")
        # Exactly the directly evidenced profiles: no business / leadership
        # noise without a deterministic word for them.
        self.assertEqual(set(names(t)), {"strategy", "marketing", "offers-sales"})
        scores = [r["score"] for r in t["routes"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_domain_nouns_need_commercial_context_to_be_strong(self):
        # "strategy"/"offer" outside a commercial context stay weak (no route).
        self.assertNotIn("strategy", names(route("Explain the opening strategy in chess.")))
        self.assertNotIn("offers-sales", names(route("Should I accept the job offer from them?")))
        self.assertNotIn("strategy", names(route("Review the architecture and our testing strategy.")))
        # "marketing strategy" is marketing's domain, not strategy's.
        self.assertEqual(names(route("Draft our marketing strategy for Q3.")), ["marketing"])

    def test_rewrite_message_routes_negotiation_communication(self):
        t = route("Rewrite this message to a business partner and keep it clear and firm")
        self.assertEqual(names(t), ["negotiation-communication"])
        self.assertIn("negotiation-communication", names(route("Draft an email to the vendor declining the renewal.")))
        # Code-shaped "message" tasks are not communication drafting.
        self.assertEqual(route("Rewrite this commit message to follow conventional commits.")["routes"], [])
        self.assertEqual(route("Reword the error message shown on a failed upload.")["routes"], [])

    def test_auth_design_review_with_fixes_routes_security_engineering_and_local(self):
        t = route("Review the authentication design and implement fixes with tests")
        self.assertEqual(set(names(t)), {"security-reviewer", "software-engineering", "local-engineer"})
        sec = next(r for r in t["routes"] if r["profile"] == "security-reviewer")
        # High-risk controls are untouched: advisory, short, skills-only, constrained.
        self.assertTrue(sec["high_risk"])
        self.assertEqual((sec["execution"], sec["toolsets"]), ("short", sp.ADVISORY_TOOLSETS))
        self.assertEqual(sec["constraint"], sp.ADVISORY_CONSTRAINT)
        impl = next(r for r in t["routes"] if r["profile"] == "local-engineer")
        self.assertEqual(impl["execution"], "kanban")

    def test_authentication_word_counts_as_security_evidence(self):
        t = route("Harden the authentication flow of the API.")
        sec = next(r for r in t["routes"] if r["profile"] == "security-reviewer")
        self.assertIn("kw:security", sec["reasons"])
        # A bare mention is still only weak evidence.
        self.assertNotIn("security-reviewer", names(route("Add docs for the authentication module.")))

    def test_private_auth_task_still_local_only(self):
        t = route("#private review the authentication design and implement fixes with tests")
        self.assertEqual(t["privacy"], "local-only")
        self.assertEqual(names(t), ["local-engineer"])
        dropped = {d["profile"] for d in t["dropped"] if d["reason"] == "privacy:local-only"}
        self.assertEqual(dropped, {"security-reviewer", "software-engineering"})


class HighRiskTest(unittest.TestCase):
    def test_high_risk_routes_are_advisory_short_and_constrained(self):
        t = route("Scope a red team exercise and a security review for our app, build a "
                  "strength training plan with protein targets, and list herbs for tea. "
                  "Compare options and trade-offs in a deep plan.")
        hr = [r for r in t["routes"] if r["high_risk"]]
        self.assertEqual({r["profile"] for r in hr},
                         {"red-team", "security-reviewer", "health-fitness", "herbalism"})
        for r in hr:
            self.assertTrue(r["advisory_only"])
            self.assertEqual(r["execution"], "short")  # even when the task is deep
            self.assertEqual(r["toolsets"], sp.ADVISORY_TOOLSETS)
            self.assertIn("no external actions", r["constraint"])
        self.assertIn("HIGH-RISK", sp.render_plan(t))

    def test_classifier_cannot_add_high_risk_or_local(self):
        seen = {}

        def fake(visible, pool):
            seen["pool"] = list(pool)
            return ["red-team", "health-fitness", "local-engineer", "philosophy", "bogus"]

        t = sp.route_text(
            "I keep going back and forth on what to do next with my life and the people "
            "around me, and I would like a structured way to think it through.",
            settings(classifier="local"), readiness=ready, classifier=fake)
        self.assertEqual(t["classifier"], "used")
        self.assertEqual(names(t), ["philosophy"])
        self.assertTrue(set(seen["pool"]).isdisjoint(set(sp.HIGH_RISK) | {"local-engineer"}))
        self.assertTrue(all(d["reason"] == "classifier:not-eligible" for d in t["dropped"]))

    def test_classifier_skipped_for_clear_or_short_tasks_and_errors_are_contained(self):
        calls = []
        clear = sp.route_text("Write a marketing campaign.", settings(classifier="local"),
                              readiness=ready, classifier=lambda v, p: calls.append(1) or [])
        self.assertEqual(calls, [])
        self.assertEqual(clear["classifier"], "not-needed")

        def boom(visible, pool):
            raise RuntimeError("down")

        t = sp.route_text("I would like a structured way to think through what I do next with "
                          "the people around me and my plans.", settings(classifier="local"),
                          readiness=ready, classifier=boom)
        self.assertEqual(t["classifier"], "error:RuntimeError")
        self.assertEqual(t["routes"], [])


class PrivacyTest(unittest.TestCase):
    def test_private_tag_drops_cloud_specialists(self):
        t = route("#private plan my workout and diet for next month.")
        self.assertEqual(t["privacy"], "local-only")
        self.assertIn("tag", t["privacy_reasons"])
        self.assertEqual(t["routes"], [])
        self.assertIn({"profile": "health-fitness", "reason": "privacy:local-only"}, t["dropped"])

    def test_private_task_keeps_only_local_engineer(self):
        t = route("#private negotiate my rent and implement a budget script with tests")
        self.assertEqual(names(t), ["local-engineer"])
        self.assertTrue(t["routes"][0]["local_only"])
        self.assertIn("negotiation-communication",
                      [d["profile"] for d in t["dropped"] if d["reason"] == "privacy:local-only"])

    def test_detector_signal_is_private(self):
        # Same deterministic detectors as the privacy guard (credential pair).
        t = route("Write a marketing campaign. username: ops password: hunter2xyz")
        self.assertEqual(t["privacy"], "local-only")
        self.assertIn("credentials", t["privacy_reasons"])
        self.assertEqual(t["routes"], [])

    def test_session_sticky_private(self):
        t = route("Write a marketing campaign for the launch.", session_private=True)
        self.assertEqual(t["privacy_reasons"], ["session-sticky"])
        self.assertEqual(t["routes"], [])

    def test_operator_private_terms(self):
        t = route("Positioning for Project Neptune launch campaign",
                  private_terms=("project neptune",))
        self.assertEqual(t["privacy"], "local-only")
        self.assertEqual(t["routes"], [])

    def test_private_signal_in_quoted_or_appended_text_still_private(self):
        quoted = route("> #private note\nWrite a marketing campaign.")
        self.assertEqual(quoted["privacy"], "local-only")
        appended = route("Write a marketing campaign.\n<memory-context>\n#private\n</memory-context>")
        self.assertEqual(appended["privacy"], "local-only")

    def test_classifier_never_called_when_private(self):
        called = []
        t = sp.route_text("#private I would like a structured way to think through my next "
                          "steps in life, it is a long and winding question for me",
                          settings(classifier="local"), readiness=ready,
                          classifier=lambda v, p: called.append(1) or ["philosophy"])
        self.assertEqual(called, [])
        self.assertEqual(t["classifier"], "skipped:private")

    def test_privacy_scan_error_fails_closed(self):
        with mock.patch.object(sp.pv, "scan_request", side_effect=RuntimeError("boom")):
            t = route("Write a marketing campaign for the launch.")
        self.assertEqual(t["privacy"], "local-only")
        self.assertEqual(t["routes"], [])


class ProvenanceTest(unittest.TestCase):
    def test_quoted_and_fenced_signals_are_inert(self):
        t = route("> red team security review\n```\nmarketing pricing offer stack\n```\n"
                  "Rename this file please.")
        self.assertEqual(t["routes"], [])

    def test_history_system_and_tool_output_ignored(self):
        request = {"messages": [
            {"role": "system", "content": "You know marketing, pricing and security review."},
            {"role": "user", "content": "Plan a marketing campaign and pricing."},
            {"role": "assistant", "content": "Here is a campaign and pricing plan."},
            {"role": "tool", "tool_call_id": "c1", "content": "herbal tincture protein diet"},
            {"role": "user", "content": "Rename the doc to final please."},
        ]}
        t = sp.route_request(request, settings(), readiness=ready)
        self.assertEqual(t["routes"], [])

    def test_backfill_memory_and_attachments_ignored(self):
        wire = ("[alice] Write a marketing campaign and pricing plan.\n\n[New message]\n"
                "[bob] Rename the doc to final please.\n\n<memory-context>\n"
                "operator likes stoicism and bjj\n</memory-context>")
        self.assertEqual(route(wire)["routes"], [])
        attached = ("Rename the doc to final please.\n\n--- Attached Context ---\n"
                    "negotiation playbook and workout plan")
        self.assertEqual(route(attached)["routes"], [])

    def test_own_note_is_cut_from_current_task(self):
        note = sp.render_plan(route("Write a marketing campaign for the launch."))
        self.assertTrue(note.startswith(sp.PLAN_OPEN))
        self.assertEqual(task_intent.genuine_user_text("Rename the doc.\n\n" + note),
                         "Rename the doc.")
        self.assertEqual(route("Rename the doc.\n\n" + note)["routes"], [])

    def test_trace_and_note_carry_no_task_text(self):
        text = "Write a marketing campaign for Zorblax Widgets launch."
        t = route(text)
        blob = json.dumps(t) + sp.render_plan(t)
        self.assertNotIn("Zorblax", blob)
        self.assertRegex(t["task"], r"^[0-9a-f]{16}$")


# A Hermes async-delegation completion envelope (tools/process_registry_notifications.py)
# whose subagent report is full of specialist vocabulary.
ASYNC_REPORT = (
    "--- \u2713 TASK 1/1: Fix the router  (status=completed, api_calls=12) ---\n"
    "Refactored the module and implemented fixes with tests. Drafted the marketing campaign "
    "and pricing plan, rewrote the email to the vendor to negotiate the renewal, and wrote "
    "a team leadership plan for the launch."
)


def async_envelope(header, sender="[Norkz] "):
    return (f"{sender}{header}\n"
            "A background fan-out unit you dispatched earlier \u2014 1 subagent(s) \u2014 has finished; "
            "its consolidated results are below.\n"
            "Dispatched: 2026-10-06 10:00:00 (5m ago)\n"
            "Role: leaf   Model: opus   Total duration: 300s\n\n" + ASYNC_REPORT)


class AsyncDelegationEnvelopeTest(unittest.TestCase):
    """Hermes re-injects async delegate_task results as a user-role message.

    That envelope is runtime output, not a new operator task: classifying the
    embedded subagent report fanned out unrelated specialists.
    """

    HEADERS = (
        "[ASYNC DELEGATION BATCH COMPLETE \u2014 deleg_2787883d]",
        "[ASYNC DELEGATION COMPLETE \u2014 deleg_2787883d]",
        "[ASYNC DELEGATION TASK FAILED \u2014 deleg_2787883d, task 1/3]",
    )

    def test_report_body_alone_would_route(self):
        # Guard: the fixture really carries routable vocabulary.
        self.assertTrue(route(ASYNC_REPORT)["routes"])

    def test_envelopes_route_nothing_and_render_no_note(self):
        for header in self.HEADERS:
            for sender in ("[Norkz] ", ""):
                with self.subTest(header=header, sender=sender):
                    t = route(async_envelope(header, sender))
                    self.assertEqual(t["routes"], [])
                    self.assertEqual(t["dropped"], [])
                    self.assertEqual(t["reason"], "runtime-envelope")
                    self.assertEqual(sp.render_plan(t), "")

    def test_envelope_with_runtime_wrappers_routes_nothing(self):
        env = async_envelope(self.HEADERS[0])
        backfill = "[alice] earlier chatter\n\n[New message]\n" + env
        memory = env + "\n\n<memory-context>\nnotes\n</memory-context>"
        for wire in (backfill, memory):
            with self.subTest(wire=wire[:30]):
                self.assertEqual(route(wire)["routes"], [])

    def test_prose_about_async_delegation_still_routes(self):
        prose = ("Write a marketing campaign explaining how our ASYNC DELEGATION BATCH "
                 "COMPLETE notices work.")
        self.assertEqual(names(route(prose)), ["marketing"])
        quoted = ("Draft a marketing campaign for the launch. The log showed "
                  "[ASYNC DELEGATION BATCH COMPLETE \u2014 deleg_1] earlier.")
        self.assertEqual(names(route(quoted)), ["marketing"])
        self.assertNotEqual(route(quoted).get("reason"), "runtime-envelope")

    def test_hook_returns_none_for_envelope(self):
        h = hook_mod.SpecialistHook(config_mod.RouterSettings(specialist_routing=True))
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)):
            self.assertIsNone(h.on_pre_llm_call(
                session_id="s", user_message=async_envelope(self.HEADERS[0])))
            self.assertIsNotNone(h.on_pre_llm_call(
                session_id="s", user_message="[Norkz] Write a marketing campaign for the launch."))


class CapAndStateTest(unittest.TestCase):
    WIDE = ("Plan marketing positioning, pricing for the offer, a business plan with unit "
            "economics, competitive advantage, negotiation of the contract, and one-on-ones "
            "with my direct reports.")

    def test_cap(self):
        t = route(self.WIDE)
        self.assertEqual(len(t["routes"]), 4)
        self.assertGreaterEqual(len([d for d in t["dropped"] if d["reason"] == "cap"]), 2)
        self.assertEqual(route(self.WIDE, max_profiles=2)["routes"].__len__(), 2)
        self.assertEqual(route(self.WIDE, max_profiles=0)["routes"], [])

    def test_clamp(self):
        self.assertEqual(sp.clamp_max(99), sp.HARD_MAX_PROFILES)
        self.assertEqual(sp.clamp_max(-3), 0)
        self.assertEqual(sp.clamp_max("x"), sp.DEFAULT_MAX_PROFILES)

    def test_deterministic_and_no_state_bleed(self):
        s = settings()
        a1 = sp.route_text(self.WIDE, s, readiness=ready)
        b = sp.route_text("What do the stoics say about anger?", s, readiness=ready)
        a2 = sp.route_text(self.WIDE, s, readiness=ready)
        self.assertEqual(names(b), ["philosophy"])
        self.assertEqual(a1, a2)
        # A private task does not make the next public task private (that is
        # the session-sticky flag's job, passed in explicitly).
        sp.route_text("#private plan my diet", s, readiness=ready)
        self.assertEqual(sp.route_text("Write a marketing campaign.", s, readiness=ready)["privacy"],
                         "public")

    def test_hook_has_no_cross_session_bleed(self):
        cfg = config_mod.RouterSettings(specialist_routing=True)
        private = {"s1"}
        h = hook_mod.SpecialistHook(cfg, session_private=lambda sid: sid in private)
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)):
            self.assertIsNone(h.on_pre_llm_call(session_id="s1",
                                                user_message="Write a marketing campaign."))
            out = h.on_pre_llm_call(session_id="s2", user_message="Write a marketing campaign.")
        self.assertIn("marketing", out["context"])


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "profiles"
        self.root.mkdir()
        (Path(self.tmp.name) / "skills" / "agent-ops" / "kit-skill" / "references").mkdir(parents=True)
        shared = Path(self.tmp.name) / "skills" / "agent-ops" / "kit-skill"
        (shared / "SKILL.md").write_text("---\nname: kit-skill\n---\n")
        (shared / "references" / "a.md").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def profile(self, name, books=0):
        home = self.root / name
        (home / "knowledge").mkdir(parents=True)
        (home / "skills").mkdir()
        for i in range(books):
            (home / "knowledge" / f"book{i}.md").write_text("chapter")
        return home

    def skill(self, home, rel, frontmatter="name: x", refs=True):
        d = home / "skills" / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n")
        if refs:
            (d / "references").mkdir()
            (d / "references" / "ch01.md").write_text("notes")
        return d

    def test_statuses(self):
        r = sp.knowledge_readiness
        self.assertEqual(r("nope", self.root).status, "unknown-profile")
        self.assertEqual(r("strategy", self.root).status, "not-installed")
        self.profile("strategy")
        self.assertEqual(r("strategy", self.root).status, "no-knowledge")
        self.profile("business", books=2)
        nb = r("business", self.root)
        self.assertEqual((nb.status, nb.knowledge_files), ("needs-learn", 2))

    def test_named_knowledge_skill_is_ready(self):
        home = self.profile("philosophy", books=1)
        self.skill(home, "research/philosophy-knowledge", refs=False)
        rd = sp.knowledge_readiness("philosophy", self.root)
        self.assertEqual((rd.status, rd.skill), ("ready", "research/philosophy-knowledge"))

    def test_references_heuristic_skips_bundled_and_shared(self):
        home = self.profile("survival", books=1)
        self.skill(home, "research/llm-wiki", frontmatter="description: knowledge-base wiki")
        (home / "skills" / ".bundled_manifest").write_text("llm-wiki:abc\n")
        self.skill(home, "agent-ops/kit-skill")
        self.skill(home, ".curator_backups/old/prep-guide")
        self.assertEqual(sp.knowledge_readiness("survival", self.root).status, "needs-learn")
        self.skill(home, "preparedness/field-manual")
        rd = sp.knowledge_readiness("survival", self.root)
        self.assertEqual((rd.status, rd.skill), ("ready", "preparedness/field-manual"))

    def test_frontmatter_reference_and_stale(self):
        home = self.profile("strategy", books=1)
        skill = self.skill(home, "business/strat-notes",
                           frontmatter="description: from ~/.hermes/profiles/strategy/knowledge",
                           refs=False)
        old = time.time() - 3600
        for f in skill.rglob("*"):
            os.utime(f, (old, old))
        rd = sp.knowledge_readiness("strategy", self.root)
        self.assertEqual(rd.status, "ready")
        self.assertTrue(rd.stale)

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_books_are_never_opened(self):
        home = self.profile("herbalism", books=2)
        for f in (home / "knowledge").iterdir():
            f.chmod(0)
        try:
            self.assertEqual(sp.knowledge_readiness("herbalism", self.root).knowledge_files, 2)
        finally:
            for f in (home / "knowledge").iterdir():
                f.chmod(0o644)

    def test_routing_uses_real_readiness(self):
        self.profile("marketing", books=1)
        s = settings(profiles_root=self.root)
        t = sp.route_text("Write a marketing campaign and pricing for the offer stack.", s)
        self.assertEqual(names(t), ["marketing"])
        self.assertEqual(t["routes"][0]["knowledge"]["status"], "needs-learn")
        self.assertIn({"profile": "offers-sales", "reason": "readiness:not-installed"}, t["dropped"])
        strict = sp.route_text("Write a marketing campaign.", settings(profiles_root=self.root,
                                                                       require_ready=True))
        self.assertEqual(strict["routes"], [])
        self.assertIn({"profile": "marketing", "reason": "readiness:needs-learn"}, strict["dropped"])

    def test_unknown_allowlist_names_reported_not_routed(self):
        known, unknown = sp.split_allowed(["marketing", "Bogus", "", "marketing"])
        self.assertEqual((known, unknown), (("marketing",), ("bogus",)))
        t = route("Write a marketing campaign and pricing for the offer stack.",
                  allowed=known, unknown=unknown)
        self.assertEqual(names(t), ["marketing"])
        self.assertEqual(t["unknown_allowed"], ["bogus"])
        self.assertIn({"profile": "offers-sales", "reason": "not-allowed"}, t["dropped"])


class FakeCtx:
    def __init__(self, cfg=None, profile="default"):
        self.cfg = cfg or {}
        self.profile_name = profile
        self.hooks, self.middleware, self.commands = {}, {}, {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_middleware(self, kind, fn):
        self.middleware[kind] = fn

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in list(os.environ):
            if k.startswith("MODEL_ROUTER_SPECIALIST"):
                del os.environ[k]

    def tearDown(self):
        self.env.stop()

    def test_off_by_default(self):
        self.assertFalse(config_mod.load_settings(None).specialist_routing)
        ctx = FakeCtx()
        load_plugin().register(ctx)
        self.assertNotIn("pre_llm_call", ctx.hooks)
        self.assertIn("route-specialists", ctx.commands)  # dry-run is always available
        self.assertEqual(set(ctx.middleware), {"llm_request", "llm_execution"})

    def test_enabled_only_in_orchestrator_profile(self):
        on = {"specialist_routing": True}
        ctx = FakeCtx(on)
        load_plugin().register(ctx)
        self.assertIn("pre_llm_call", ctx.hooks)
        spec_ctx = FakeCtx(on, profile="marketing")
        load_plugin().register(spec_ctx)
        self.assertNotIn("pre_llm_call", spec_ctx.hooks)

    def test_settings_parsing(self):
        s = config_mod.load_settings(lambda k, d: {"specialist_routing": "yes",
                                                   "specialist_classifier": "cloud",
                                                   "specialist_max_profiles": "x"}.get(k))
        self.assertTrue(s.specialist_routing)
        self.assertEqual(s.specialist_classifier, "none")  # only none|local accepted
        self.assertEqual(s.specialist_max_profiles, 4)
        with mock.patch.dict(os.environ, {"MODEL_ROUTER_SPECIALISTS": "true",
                                          "MODEL_ROUTER_SPECIALIST_MAX": "2"}):
            env = config_mod.load_settings(None)
        self.assertTrue(env.specialist_routing)
        self.assertEqual(env.specialist_max_profiles, 2)

    def test_hook_behaviour(self):
        h = hook_mod.SpecialistHook(config_mod.RouterSettings(specialist_routing=True))
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)):
            out = h.on_pre_llm_call(session_id="s", user_message=[
                {"type": "text", "text": "Write a marketing campaign for the launch."}])
            self.assertIn(sp.PLAN_OPEN, out["context"])
            self.assertIn("kanban_create", out["context"])
            self.assertIsNone(h.on_pre_llm_call(session_id="s", parent_session_id="p",
                                                user_message="Write a marketing campaign."))
            self.assertIsNone(h.on_pre_llm_call(session_id="s", user_message="Rename the doc."))
        with mock.patch.object(sp, "route_text", side_effect=RuntimeError("x")):
            self.assertIsNone(h.on_pre_llm_call(session_id="s", user_message="anything"))
        off = hook_mod.SpecialistHook(config_mod.RouterSettings())
        self.assertIsNone(off.on_pre_llm_call(session_id="s",
                                              user_message="Write a marketing campaign."))

    def test_dry_run_command(self):
        h = hook_mod.SpecialistHook(config_mod.RouterSettings(
            specialist_profiles_root="/nonexistent/profiles"))
        self.assertIn("usage", h.dry_run(""))
        out = json.loads(h.dry_run("Write a marketing campaign."))
        self.assertFalse(out["active_in_this_profile"])
        self.assertIn({"profile": "marketing", "reason": "readiness:not-installed"}, out["dropped"])

    def test_local_classifier_requires_configured_local_backend(self):
        self.assertIsNone(hook_mod.local_classifier(SimpleNamespace(configured=False)))
        backend = SimpleNamespace(configured=True, chat=lambda msgs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="philosophy, strategy"))]))
        fn = hook_mod.local_classifier(backend)
        self.assertEqual(fn("x", ["philosophy", "strategy", "business"]), ["philosophy", "strategy"])

    def test_cli_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "specialist-route.py"),
                 "--profiles-root", tmp, "--plan", "Write a Zorblax marketing campaign."],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Zorblax", proc.stdout)
        body = proc.stdout[: proc.stdout.rindex("}") + 1]
        trace = json.loads(body)
        self.assertEqual(trace["profiles_root"], tmp)
        self.assertIn({"profile": "marketing", "reason": "readiness:not-installed"}, trace["dropped"])


if __name__ == "__main__":
    unittest.main()
