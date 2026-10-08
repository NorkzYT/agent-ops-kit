"""Unit + canary tests for the model-router plugin.

Run: python3 -m unittest discover -s scripts/tests -p 'test_*.py'

No real secrets are used anywhere here — all sensitive-looking values are
synthetic and constructed to satisfy the deterministic detectors' checksums.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
from _loader import load_plugin, submodule  # noqa: E402

privacy = submodule("privacy")
marker = submodule("privacy_marker")
complexity = submodule("complexity")
router_mod = submodule("router")
config_mod = submodule("router_config")
local_backend = submodule("local_backend")


def req(*user_texts, system=None, tool=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for t in user_texts:
        msgs.append({"role": "user", "content": t})
    if tool:
        msgs.append({"role": "tool", "tool_call_id": "c1", "content": tool})
    return {"model": "gpt-5.6-terra", "messages": msgs}


# Synthetic test fixtures (NOT real secrets) --------------------------------- #
SSN = "219-09-9999"            # valid area/group/serial pattern
PAN = "4242 4242 4242 4242"    # Luhn-valid test card
ABA = "021000021"             # valid ABA checksum (well-known test routing no.)
IBAN = "GB82 WEST 1234 5698 7654 32".replace(" ", "")  # valid mod-97 example
FAKE_SECRET = "api_key = AKIAIOSFODNN7EXAMPLE"
FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"


class TestPrivacyDetectors(unittest.TestCase):
    """The offline content scanner (knowledge-classify). Request routing never
    uses it — see TestPrivacyMarker / TestNoContentClassification."""

    def _private(self, request, expect):
        d = privacy.scan_request(request)
        self.assertTrue(d.private, f"expected private for {expect}: got {d.reasons}")
        self.assertIn(expect, d.reasons)

    def test_ssn(self):
        self._private(req(f"my ssn is {SSN}"), "ssn")

    def test_ssn_invalid_area_is_public(self):
        d = privacy.scan_request(req("number 000-12-3456 is not an ssn"))
        self.assertNotIn("ssn", d.reasons)

    def test_pan_luhn(self):
        self._private(req(f"card {PAN}"), "pan")

    def test_pan_non_luhn_public(self):
        d = privacy.scan_request(req("order 1234 5678 9012 3456 shipped"))
        self.assertNotIn("pan", d.reasons)

    def test_aba(self):
        self._private(req(f"routing {ABA}"), "aba")

    def test_iban(self):
        self._private(req(f"iban {IBAN}"), "iban")

    def test_secret_literal(self):
        self._private(req(FAKE_SECRET), "secret")

    def test_private_key(self):
        self._private(req(FAKE_PEM), "private_key")

    def test_credentials_pair(self):
        self._private(req("username: alice\npassword: hunter2ish"), "credentials")

    def test_url_creds(self):
        self._private(req("clone https://user:pw@example.com/x.git"), "credentials")

    def test_tag(self):
        self._private(req("#private draft this reply"), "tag")

    def test_configured_term_and_path(self):
        d = privacy.scan_request(req("open the ProjectNeptune brief at /vault/secrets/x"),
                                 private_terms=["ProjectNeptune"], private_paths=["/vault/"])
        self.assertTrue(d.private)
        self.assertIn("term", d.reasons)
        self.assertIn("path", d.reasons)

    def test_public_payload(self):
        d = privacy.scan_request(req("what's the weather like in Paris tomorrow?"))
        self.assertFalse(d.private, d.reasons)

    def test_inspects_system_and_tool_results(self):
        self.assertTrue(privacy.scan_request(req("hi", system=f"context: {SSN}")).private)
        self.assertTrue(privacy.scan_request(req("hi", tool=f"file contents: {FAKE_SECRET}")).private)

    def test_responses_api_shape(self):
        r = {"instructions": "assist", "input": [{"role": "user", "content": f"ssn {SSN}"}]}
        self.assertTrue(privacy.scan_request(r).private)

    def test_reasons_never_contain_content(self):
        d = privacy.scan_request(req(f"ssn {SSN} card {PAN}"))
        blob = "".join(d.reasons)
        self.assertNotIn("219", blob)
        self.assertNotIn("4242", blob)


def _user(text):
    return {"role": "user", "content": text}


class TestPrivacyMarker(unittest.TestCase):
    """Only a sender-authored #privacy in the CURRENT user turn makes a request private."""

    def _private(self, request):
        return marker.decide(request).private

    def test_marker_in_current_turn(self):
        for text in ("#privacy draft this", "draft this #privacy", "#PRIVACY\ndraft this",
                     "[Norkz] draft this (#privacy)", "#privacy, draft this"):
            with self.subTest(text=text):
                self.assertTrue(self._private(req(text)))
        self.assertEqual(marker.decide(req("#privacy x")).reasons, ("marker",))

    def test_marker_word_boundaries(self):
        for text in ("#privacy-policy page", "#privacyx", "foo#privacy", "##privacy", "privacy please",
                     "#private-beta page", "#privates", "foo#private", "#local draft this"):
            with self.subTest(text=text):
                self.assertFalse(self._private(req(text)))

    def test_non_sender_sources_cannot_activate(self):
        cases = {
            "system": req("draft this", system="#privacy is on for everything"),
            "tool-result": req("draft this", tool="file says #privacy"),
            "assistant": {"messages": [_user("draft this"),
                                       {"role": "assistant", "content": "#privacy"}]},
            "developer": {"messages": [{"role": "developer", "content": "#privacy"}, _user("draft this")]},
            "earlier-user-turn": req("#privacy old secret task", "now implement the parser"),
            "memory": req("draft this\n<memory-context>\n#privacy\n</memory-context>"),
            "attached-context": req("draft this\n--- Attached Context ---\n#privacy"),
            "backfill": req("[alice] #privacy notes\n\n[New message]\n[Norkz] draft this"),
            "reply-quote": req('[Replying to: "#privacy notes"]\ndraft this'),
            "quoted": req("> #privacy\ndraft this"),
            "fenced": req("```\n#privacy\n```\ndraft this"),
            "kanban-wake": req("[kanban] Task t_0123abcd done.\nTitle: #privacy card\nAssignee: @coder"),
            "tool-result-user-part": {"messages": [_user("draft this"), {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "#privacy"}]}]},
            "responses-output": {"input": [_user("draft this"),
                                           {"type": "function_call_output", "call_id": "c", "output": "#privacy"}]},
            "responses-instructions": {"instructions": "#privacy", "input": "draft this"},
        }
        for name, request in cases.items():
            with self.subTest(source=name):
                self.assertFalse(self._private(request))

    def test_current_turn_wins_over_tool_loop_items(self):
        # The marked task's own tool loop keeps it private on every call.
        request = {"messages": [_user("#privacy fix the parser"),
                                {"role": "assistant", "content": None, "tool_calls": [
                                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                                {"role": "tool", "tool_call_id": "c1", "content": "ok"}]}
        self.assertTrue(self._private(request))

    def test_responses_and_steer_shapes(self):
        self.assertTrue(self._private({"input": "#privacy draft this"}))
        self.assertTrue(self._private({"input": [{"role": "user", "content": [
            {"type": "input_text", "text": "#privacy draft this"}]}]}))
        steer = "[OUT-OF-BAND USER MESSAGE \u2014 x]\n#privacy draft this\n[/OUT-OF-BAND USER MESSAGE]"
        self.assertTrue(self._private(req(steer)))

    def test_malformed_requests_are_public(self):
        for request in (None, {}, {"messages": None}, {"messages": ["x", 3]}, "#privacy"):
            with self.subTest(request=repr(request)[:20]):
                self.assertFalse(self._private(request))


class TestNoContentClassification(unittest.TestCase):
    def test_sensitive_content_without_marker_is_public(self):
        r = _router()
        for text in (f"my ssn is {SSN}", f"card {PAN}", f"routing {ABA}", f"iban {IBAN}", FAKE_SECRET,
                     FAKE_PEM, "username: alice\npassword: hunter2ish", "clone https://user:pw@example.com/x.git"):
            with self.subTest(text=text[:12]):
                self.assertFalse(r.is_private(req(text), "s").private)

    def test_configured_terms_and_paths_do_not_route(self):
        r = _router(private_terms=["ProjectNeptune"], private_paths=["/vault/"])
        self.assertFalse(r.is_private(req("open the ProjectNeptune brief at /vault/x"), "s").private)

    def test_register_warns_that_terms_are_ignored(self):
        settings = {"private_terms": ["x"]}
        ctx = SimpleNamespace(get_config=lambda k, d=None: settings.get(k, d), profile_name="default",
                              register_middleware=lambda *a: None, register_hook=lambda *a: None)
        with self.assertLogs("hermes.plugin.model_router", level="WARNING") as logs:
            load_plugin().register(ctx)
        self.assertIn("private_terms/private_paths are ignored", "\n".join(logs.output))


class TestPrivateHistoryWithheld(unittest.TestCase):
    """A public request after a #privacy turn never replays that turn to the cloud."""

    def _send(self, request, router=None):
        seen = []
        r = router or _router()
        resp = r.on_llm_execution(request=request, next_call=lambda p: seen.append(p) or "CLOUD",
                                  session_id="h", base_url="http://127.0.0.1:8317/v1")
        return resp, seen

    def test_marked_exchange_is_withheld_and_input_not_mutated(self):
        request = {"model": "m", "messages": [
            {"role": "system", "content": "be helpful"},
            _user(f"#privacy card {PAN}"),
            {"role": "assistant", "content": f"noted {PAN}", "reasoning_content": f"think {PAN}"},
            _user("hello"),
            {"role": "assistant", "content": "hi"},
            _user("now implement the parser")]}
        snapshot = json.dumps(request)
        resp, seen = self._send(request)
        self.assertEqual(resp, "CLOUD")
        sent = seen[0]["messages"]
        self.assertEqual(json.dumps(request), snapshot)
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(sent[1]["content"], marker.WITHHELD)
        self.assertEqual(sent[2], {"role": "assistant", "content": marker.WITHHELD})
        self.assertEqual(sent[0], request["messages"][0])
        self.assertEqual(sent[3:], request["messages"][3:])
        self.assertNotIn("4242", json.dumps(seen[0]))

    def test_tool_pairing_is_preserved(self):
        request = {"messages": [
            _user("#privacy read my notes"),
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"p": "/n"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": f"ssn {SSN}"},
            {"role": "assistant", "content": [{"type": "text", "text": f"ssn {SSN}"}]},
            _user("public task"),
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "ls", "arguments": '{"d": "."}'}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "a.py"}]}
        _, seen = self._send(request)
        sent = seen[0]["messages"]
        self.assertEqual(sent[1]["tool_calls"],
                         [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}])
        self.assertIsNone(sent[1]["content"])
        self.assertEqual(sent[2], {"role": "tool", "tool_call_id": "c1", "name": "read", "content": marker.WITHHELD})
        self.assertEqual(sent[3]["content"], [{"type": "text", "text": marker.WITHHELD}])
        self.assertEqual(sent[4:], request["messages"][4:])  # the current task's own loop is untouched
        self.assertNotIn(SSN, json.dumps(seen[0]))

    def test_responses_items(self):
        request = {"model": "m", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"#privacy {SSN}"}]},
            {"type": "reasoning", "id": "r1", "summary": [{"type": "summary_text", "text": SSN}]},
            {"type": "function_call", "call_id": "f1", "name": "read", "arguments": f'{{"q": "{SSN}"}}'},
            {"type": "function_call_output", "call_id": "f1", "output": SSN},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": SSN}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "public task"}]}]}
        _, seen = self._send(request)
        sent = seen[0]["input"]
        self.assertEqual([i["type"] for i in sent],
                         ["message", "function_call", "function_call_output", "message", "message"])
        self.assertEqual(sent[1], {"type": "function_call", "call_id": "f1", "name": "read", "arguments": "{}"})
        self.assertEqual(sent[2]["output"], marker.WITHHELD)
        self.assertEqual(sent[3]["content"], [{"type": "output_text", "text": marker.WITHHELD}])
        self.assertEqual(sent[4], request["input"][5])
        self.assertNotIn(SSN, json.dumps(seen[0]))

    def test_unmarked_history_passes_the_same_object(self):
        request = req("#local old", f"ssn {SSN}", "now implement it")
        _, seen = self._send(request)
        self.assertIs(seen[0], request)

    def test_withholding_error_stays_local(self):
        r = _router()
        spy = _Spy()
        original = marker.withhold_marked_history
        marker.withhold_marked_history = lambda request: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            resp = r.on_llm_execution(request=req("#privacy a", "b"), next_call=spy, session_id="e")
        finally:
            marker.withhold_marked_history = original
        self.assertEqual(spy.calls, 0)
        self.assertTrue(resp._model_router_refusal)

    def test_privacy_disabled_does_not_withhold(self):
        request = req("#privacy a", "b")
        _, seen = self._send(request, router=_router(privacy_routing=False))
        self.assertIs(seen[0], request)


class TestComplexity(unittest.TestCase):
    def test_routine(self):
        self.assertEqual(complexity.score_request(req("hi there")).tier, "routine")

    def test_high_keyword_needs_substance(self):
        # Two distinct hard categories (arch + refactor scope) -> high (Sol),
        # never max without explicit authorization.
        self.assertEqual(
            complexity.score_request(req("refactor the architecture across the codebase")).tier,
            "high",
        )

    def test_single_keyword_is_not_sol(self):
        # One keyword alone is ambiguous work, not Sol-hard.
        self.assertEqual(complexity.score_request(req("please debug this small helper")).tier, "medium")
        self.assertEqual(complexity.score_request(req("plan the migration")).tier, "elevated")

    def test_forced_tags(self):
        # Explicit max authorization = LEADING STANDALONE control line only.
        self.assertEqual(complexity.score_request(req("#max\nsimple thing")).tier, "max")
        self.assertEqual(complexity.score_request(req("#astra\nsimple thing")).tier, "max")
        # Inline #max is a hard-work hint (Sol at most), never an Astra grant.
        self.assertEqual(complexity.score_request(req("#max simple thing")).tier, "high")
        # #deep / #hard alone no longer grant Astra either.
        self.assertEqual(complexity.score_request(req("#deep think about this")).tier, "high")
        self.assertEqual(complexity.score_request(req("#quick big architecture migration rewrite")).tier, "routine")

    def test_gpt_tier_models(self):
        # Balanced policy: luna covers routine AND ordinary execution, terra is
        # the ambiguous/planning tier, sol the hard tier, astra explicit-only.
        self.assertEqual(complexity.resolve_model("routine", family="gpt"), "gpt-5.6-luna")
        self.assertEqual(complexity.resolve_model("medium", family="gpt"), "gpt-5.6-luna")
        self.assertEqual(complexity.resolve_model("elevated", family="gpt"), "gpt-5.6-terra")
        self.assertEqual(complexity.resolve_model("high", family="gpt"), "gpt-5.6-sol")
        # Without explicit authorization the max tier clamps to the Sol ceiling.
        self.assertEqual(complexity.resolve_model("max", family="gpt"), "gpt-5.6-sol")
        self.assertEqual(complexity.resolve_model("max", family="gpt", allow_max=True), "gpt-6-astra")

    def test_coding_tier_models(self):
        self.assertEqual(complexity.resolve_model("routine", family="coding"), "opus")
        self.assertEqual(complexity.resolve_model("medium", family="coding"), "opus")
        self.assertEqual(complexity.resolve_model("high", family="coding"), "fable")

    def test_degrade_when_unavailable(self):
        # astra gone -> step down to sol (authorized max degrades too)
        self.assertEqual(
            complexity.resolve_model("max", family="gpt", allow_max=True,
                                     available=["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"]),
            "gpt-5.6-sol",
        )

    def test_degrade_never_upgrades(self):
        # only terra available; requesting high degrades DOWN to terra
        self.assertEqual(
            complexity.resolve_model("high", family="gpt", available=["gpt-5.6-terra"]),
            "gpt-5.6-terra",
        )

    def test_missing_lower_models_never_upshift_to_astra(self):
        # Only astra advertised: the upward walk must NOT silently pick it.
        self.assertIsNone(
            complexity.resolve_model("routine", family="gpt", available=["gpt-6-astra"])
        )
        # With explicit authorization the upward walk may reach it.
        self.assertEqual(
            complexity.resolve_model("routine", family="gpt", available=["gpt-6-astra"],
                                     allow_max=True),
            "gpt-6-astra",
        )

    def test_alias_to_tier(self):
        self.assertEqual(complexity.alias_to_tier("gpt-5.6-luna"), "routine")
        self.assertEqual(complexity.alias_to_tier("luna"), "routine")
        self.assertEqual(complexity.alias_to_tier("gpt-5.6-terra"), "elevated")
        self.assertEqual(complexity.alias_to_tier("terra"), "elevated")
        self.assertEqual(complexity.alias_to_tier("gpt-5.6-sol"), "high")
        self.assertEqual(complexity.alias_to_tier("gpt-6-astra"), "max")
        self.assertEqual(complexity.alias_to_tier("fable"), "high")
        self.assertIsNone(complexity.alias_to_tier("some-unknown-model"))


class _Spy:
    """Stands in for the cloud provider. Records every byte it would receive."""

    def __init__(self):
        self.calls = 0
        self.bytes = 0

    def __call__(self, request):
        self.calls += 1
        self.bytes += len(str(request).encode("utf-8"))
        return SimpleNamespace(model="CLOUD", choices=[SimpleNamespace(
            message=SimpleNamespace(content="cloud-answer"))], usage=None)


def _router(**settings):
    base = dict(complexity_routing=True, privacy_routing=True,
                ollama_base_url="", ollama_model="")
    base.update(settings)
    return router_mod.Router(config_mod.RouterSettings(**base))


class TestPrivacyCanary(unittest.TestCase):
    """Proves private payloads send ZERO bytes to the cloud (:8317 / :3456)."""

    def _run(self, request, base_url):
        spy = _Spy()
        r = _router()
        resp = r.on_llm_execution(request=request, next_call=spy, session_id="s1", base_url=base_url)
        return spy, resp

    def test_private_sends_zero_bytes_to_orchestrator_8317(self):
        spy, resp = self._run(req(f"#privacy ssn {SSN}"), "http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0, "cloud provider was called for private data")
        self.assertEqual(spy.bytes, 0, "private bytes reached the cloud")
        self.assertTrue(getattr(resp, "_model_router_refusal", False))

    def test_private_sends_zero_bytes_to_delegation_3456(self):
        spy, resp = self._run(req(f"#privacy card {PAN}"), "http://127.0.0.1:3456/v1")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(spy.bytes, 0)

    def test_public_reaches_cloud(self):
        spy, resp = self._run(req("what is 2+2?"), "http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 1)
        self.assertEqual(resp.model, "CLOUD")

    def test_local_backend_answers_private_without_cloud(self):
        spy = _Spy()
        r = _router(ollama_model="local-model")
        seen = []

        def chat(msgs, **kw):
            seen.append(kw)
            return local_backend.make_response("LOCAL", model="ollama")

        r._backend = SimpleNamespace(chat=chat)
        resp = r.on_llm_execution(request=req(f"#privacy ssn {SSN}"), next_call=spy, session_id="s2",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(resp.choices[0].message.content, "LOCAL")
        self.assertEqual(seen[0]["options"]["reasoning_effort"], "none")
        self.assertGreater(seen[0]["options"]["max_tokens"], 256)  # 256 left qwen thinking-only

    def test_marker_is_not_sticky(self):
        spy = _Spy()
        r = _router(ollama_model="local-model")
        r._backend = SimpleNamespace(chat=lambda msgs, **kw: local_backend.make_response("LOCAL", model="ollama"))
        first = req(f"#privacy ssn {SSN}")
        resp = r.on_llm_execution(request=first, next_call=spy, session_id="turns",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertEqual((spy.calls, resp.choices[0].message.content), (0, "LOCAL"))
        # The next turn in the SAME session has no marker: it goes downstream normally.
        later = {"model": "gpt-5.6-terra", "messages": first["messages"] + [
            {"role": "assistant", "content": f"LOCAL answer about {SSN}"},
            {"role": "user", "content": "now implement the parser"}]}
        resp = r.on_llm_execution(request=later, next_call=spy, session_id="turns",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertEqual((spy.calls, resp.model), (1, "CLOUD"))
        self.assertFalse(hasattr(r, "_private_sessions"))

    def test_unmarked_sensitive_content_reaches_cloud(self):
        # Policy: only #privacy forces local; content is never auto-classified.
        spy, resp = self._run(req(f"ssn {SSN} card {PAN} {FAKE_SECRET}"), "http://127.0.0.1:8317/v1")
        self.assertEqual((spy.calls, resp.model), (1, "CLOUD"))

    def test_classifier_error_is_fail_closed(self):
        spy = _Spy()
        r = _router()
        r.is_private = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # force error path
        resp = r.on_llm_execution(request=req("benign"), next_call=spy, session_id="s3",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0, "guard fell open to cloud on internal error")
        self.assertTrue(getattr(resp, "_model_router_refusal", False))

    def test_privacy_disabled_toggle_allows_cloud(self):
        spy = _Spy()
        r = _router(privacy_routing=False)
        r.on_llm_execution(request=req(f"#privacy ssn {SSN}"), next_call=spy, session_id="s4",
                           base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 1)  # documents that the guard is what blocks egress

    def test_middleware_never_raises(self):
        spy = _Spy()
        r = _router()
        # malformed request must not raise out of the guard
        resp = r.on_llm_execution(request=None, next_call=spy, session_id="s5",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertIsNotNone(resp)


class TestProviderExceptionBoundary(unittest.TestCase):
    def test_provider_errors_propagate_unchanged_once(self):
        for enabled in (False, True):
            for error in (RuntimeError("synthetic provider failure"),
                          TimeoutError("synthetic timeout")):
                with self.subTest(privacy=enabled, error=type(error).__name__):
                    router = _router(privacy_routing=enabled)
                    request = req("hello")
                    calls = []

                    def downstream(received):
                        calls.append(received)
                        raise error

                    with self.assertRaises(type(error)) as caught:
                        router.on_llm_execution(request=request, next_call=downstream)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(len(calls), 1)
                    self.assertIs(calls[0], request)

    def test_disabled_privacy_does_not_classify_or_call_local(self):
        router = _router(privacy_routing=False)
        def unexpected(*args, **kwargs):
            self.fail("disabled privacy must not classify or call local")
        router.is_private = unexpected
        router._local_or_refuse = unexpected
        spy = _Spy()
        result = router.on_llm_execution(request=req("hello"), next_call=spy)
        self.assertEqual(spy.calls, 1)
        self.assertEqual(result.model, "CLOUD")

    def test_private_backend_error_never_calls_downstream(self):
        router = _router(ollama_model="local-model")
        def broken_local(messages):
            raise RuntimeError("synthetic local failure")
        router._backend = SimpleNamespace(chat=broken_local)
        spy = _Spy()
        result = router.on_llm_execution(request=req("#privacy synthetic"), next_call=spy)
        self.assertEqual(spy.calls, 0)
        self.assertTrue(result._model_router_refusal)

    def test_unexpected_local_guard_error_still_refuses(self):
        router = _router()
        def broken_guard(*args):
            raise RuntimeError("synthetic guard failure")
        router._local_or_refuse = broken_guard
        spy = _Spy()
        result = router.on_llm_execution(request=req("#privacy synthetic"), next_call=spy)
        self.assertEqual(spy.calls, 0)
        self.assertTrue(result._model_router_refusal)


def _local_down(kind):
    """Backend factory whose every local model fails with ``kind``."""
    calls = []

    def factory(model):
        def chat(messages, **kw):
            calls.append(model)
            raise local_backend.LocalUnavailable(f"synthetic {kind}", kind)
        return SimpleNamespace(chat=chat)
    return factory, calls


LOCAL_ROLES = dict(ollama_base_url="http://127.0.0.1:11434/v1", ollama_routine_model="qwen3.5:27b",
                   ollama_fallback_model="qwen3.6:27b", ollama_agent_model="qwen3.8:27b")


class TestPrivateRefusalIsTerminal(unittest.TestCase):
    """Incident regression: a private request whose local inference timed out was
    refused, then Hermes activated cloud fallback (gpt-5.6-luna -> opus) because
    the refusal carried finish_reason=content_filter. The refusal must be final."""

    def _router(self, factory, **over):
        s = dict(LOCAL_ROLES, privacy_routing=True, complexity_routing=True)
        s.update(over)
        return router_mod.Router(config_mod.RouterSettings(**s), backend_factory=factory)

    def test_local_timeout_refuses_with_zero_cloud_calls(self):
        factory, local_calls = _local_down("timeout")
        r = self._router(factory)
        spy = _Spy()
        with self.assertLogs("hermes.plugin.model_router", level="WARNING") as logs:
            resp = r.on_llm_execution(request=req(f"#privacy card {PAN}"), next_call=spy, session_id="inc",
                                      base_url="http://127.0.0.1:8317/v1", model="gpt-5.6-luna")
        self.assertIn("private->refuse (local unavailable: timeout)", "\n".join(logs.output))
        self.assertEqual((spy.calls, spy.bytes), (0, 0), "private payload reached a cloud provider")
        self.assertEqual(local_calls, ["qwen3.5:27b", "qwen3.6:27b"])  # role model + one local fallback
        self.assertTrue(resp._model_router_refusal)
        self.assertEqual(resp._model_router_refusal_reason, "local-unavailable")
        self.assertEqual(resp.choices[0].finish_reason, "stop")
        self.assertNotIn(PAN, resp.choices[0].message.content)
        # A fallback restart re-sends the same marked turn: it stays off the cloud.
        retry = r.on_llm_execution(request=req(f"#privacy card {PAN}"), next_call=spy, session_id="inc",
                                   base_url="http://127.0.0.1:3456/v1", model="opus")
        self.assertEqual(spy.calls, 0)
        self.assertTrue(retry._model_router_refusal)
        # A new unmarked turn is public, but the marked turn is withheld from it.
        seen = []
        follow = r.on_llm_execution(request=req(f"#privacy card {PAN}", "ok, write the release notes"),
                                    next_call=lambda p: seen.append(p) or "CLOUD", session_id="inc",
                                    base_url="http://127.0.0.1:3456/v1", model="opus")
        self.assertEqual(follow, "CLOUD")
        self.assertNotIn(PAN.replace(" ", ""), str(seen).replace(" ", ""))

    def test_no_refusal_path_signals_content_filter(self):
        factory, _ = _local_down("timeout")
        cases = {
            "local-unavailable": (self._router(factory), req(f"#privacy ssn {SSN}")),
            "local-endpoint-not-local": (self._router(factory, ollama_base_url="https://api.example.com/v1"),
                                         req(f"#privacy ssn {SSN}")),
            "astra-unauthorized": (self._router(factory, astra_policy="never"),
                                   {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}]}),
        }
        guard = self._router(factory)
        guard.is_private = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        cases["router-guard"] = (guard, req("benign"))
        for reason, (router, request) in cases.items():
            with self.subTest(reason=reason):
                spy = _Spy()
                resp = router.on_llm_execution(request=request, next_call=spy, session_id=reason)
                self.assertEqual(spy.calls, 0)
                self.assertTrue(resp._model_router_refusal)
                self.assertEqual(resp._model_router_refusal_reason, reason)
                self.assertNotEqual(resp.choices[0].finish_reason, "content_filter")
                self.assertTrue(resp.choices[0].message.content.strip())

    def test_empty_local_content_is_a_local_failure_not_an_answer(self):
        body = b'{"model": "qwen3.5:27b", "choices": [{"message": {"content": "  "}}]}'

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        backend = local_backend.LocalBackend("http://127.0.0.1:11434/v1", "qwen3.5:27b")
        original = local_backend._OPENER
        local_backend._OPENER = SimpleNamespace(open=lambda *a, **k: _Resp())
        try:
            with self.assertRaises(local_backend.LocalUnavailable) as caught:
                backend.chat([{"role": "user", "content": "x"}])
        finally:
            local_backend._OPENER = original
        self.assertEqual(caught.exception.kind, "shape")

    def test_disabled_privacy_preserves_provider_exception_for_private_payload(self):
        factory, local_calls = _local_down("timeout")
        r = self._router(factory, privacy_routing=False)
        error = TimeoutError("synthetic provider timeout")
        calls = []

        def downstream(received):
            calls.append(received)
            raise error

        request = req(f"#privacy ssn {SSN}")
        with self.assertRaises(TimeoutError) as caught:
            r.on_llm_execution(request=request, next_call=downstream, session_id="off")
        self.assertIs(caught.exception, error)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], request)
        self.assertEqual(local_calls, [])


AGENT_DIR = os.environ.get("HERMES_AGENT_DIR") or os.path.expanduser("~/.hermes/hermes-agent")
VENV_PY = os.path.join(AGENT_DIR, "venv", "bin", "python")

# Runs inside the Hermes venv: the router as registered llm_execution middleware,
# the real middleware chain, and Hermes' own response checks.
_CORE_SEAM = r"""
import json, os, sys, tempfile
os.environ["HERMES_HOME"] = tempfile.mkdtemp()
sys.path[:0] = [os.environ["HERMES_AGENT_DIR"], os.environ["TESTS_DIR"]]
from types import SimpleNamespace
from _loader import submodule
router_mod, config_mod, lb = submodule("router"), submodule("router_config"), submodule("local_backend")
from hermes_cli.middleware import LLM_EXECUTION_MIDDLEWARE, run_llm_execution_middleware
from hermes_cli.plugins import get_plugin_manager
from agent.transports import get_transport
from agent.turn_recovery import validate_response_shape
from agent.turn_response_check import _derive_finish_reason

def factory(model):
    def chat(messages, **kw):
        raise lb.LocalUnavailable("synthetic timeout", "timeout")
    return SimpleNamespace(chat=chat)

out = {}
def run(privacy, request, terminal):
    s = config_mod.RouterSettings(privacy_routing=privacy, complexity_routing=True,
                                  ollama_base_url="http://127.0.0.1:11434/v1",
                                  ollama_routine_model="qwen3.5:27b", ollama_fallback_model="qwen3.6:27b")
    mw = get_plugin_manager()._middleware
    mw[LLM_EXECUTION_MIDDLEWARE] = [router_mod.Router(s, backend_factory=factory).on_llm_execution]
    return run_llm_execution_middleware(request, terminal, session_id="seam", model=request["model"],
                                        base_url="http://127.0.0.1:8317/v1", api_mode="chat_completions")

cloud = []
private = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "#privacy card 4242 4242 4242 4242"}]}
resp = run(True, private, lambda p: cloud.append(p))
agent = SimpleNamespace(api_mode="chat_completions", _get_transport=lambda: get_transport("chat_completions"),
                        _should_treat_stop_as_truncated=lambda *a, **k: False)
out["cloud_calls"] = len(cloud)
out["refusal"] = bool(getattr(resp, "_model_router_refusal", False))
out["shape_invalid"] = validate_response_shape(agent, resp)[0]
out["finish_reason"] = _derive_finish_reason(agent, resp, private["messages"])
legacy = lb.make_response("x", model="m", finish_reason="content_filter")
out["legacy_finish_reason"] = _derive_finish_reason(agent, legacy, private["messages"])

sent = []
later = {"model": "gpt-5.6-luna", "messages": private["messages"] + [
    {"role": "assistant", "content": "local answer"}, {"role": "user", "content": "now write the notes"}]}
run(True, later, lambda p: sent.append(p) or lb.make_response("cloud", model="c"))
out["public_after_private_calls"] = len(sent)
out["public_after_private_leaks"] = "4242" in json.dumps(sent)

for privacy in (False, True):
    err = TimeoutError("synthetic provider timeout")
    def boom(p):
        raise err
    try:
        run(privacy, {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}]}, boom)
        out[f"exc_privacy_{privacy}"] = "none"
    except TimeoutError as caught:
        out[f"exc_privacy_{privacy}"] = "same" if caught is err else "different"
print(json.dumps(out))
"""


@unittest.skipUnless(os.path.exists(VENV_PY), "Hermes venv not installed")
class TestHermesCoreSeam(unittest.TestCase):
    """The refusal as installed Hermes sees it through the real middleware chain."""

    def test_private_refusal_is_terminal_in_hermes_core(self):
        r = subprocess.run([VENV_PY, "-c", _CORE_SEAM], capture_output=True, text=True, timeout=180,
                           env=dict(os.environ, HERMES_AGENT_DIR=AGENT_DIR,
                                    TESTS_DIR=os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["cloud_calls"], 0)
        self.assertTrue(out["refusal"])
        self.assertFalse(out["shape_invalid"])  # an invalid shape also triggers eager fallback
        self.assertEqual(out["finish_reason"], "stop")
        # Why the old shape leaked: core routes content_filter to its fallback handler.
        self.assertEqual(out["legacy_finish_reason"], "content_filter")
        # Not sticky: the next unmarked turn reaches the provider, minus the marked turn.
        self.assertEqual(out["public_after_private_calls"], 1)
        self.assertFalse(out["public_after_private_leaks"])
        self.assertEqual(out["exc_privacy_False"], "same")
        self.assertEqual(out["exc_privacy_True"], "same")


class TestComplexityRewrite(unittest.TestCase):
    def test_orchestrator_upgrades_high(self):
        r = _router()
        out = r.on_llm_request(
            request=req("refactor the whole architecture and migrate the schema end-to-end"),
            base_url="http://127.0.0.1:8317/v1",
        )
        self.assertIsNotNone(out)
        self.assertIn(out["request"]["model"], ("gpt-5.6-sol", "gpt-6-astra"))

    def test_coding_family_high_picks_fable(self):
        r = _router()
        request = {"model": "opus",
                   "messages": [{"role": "user", "content": "refactor architecture, migrate schema, rewrite module"}]}
        out = r.on_llm_request(request=request, base_url="http://127.0.0.1:3456/v1")
        self.assertIsNotNone(out)
        self.assertEqual(out["request"]["model"], "fable")

    def test_unmanaged_model_untouched(self):
        r = _router()
        request = {"model": "some-custom-id", "messages": [{"role": "user", "content": "hi"}]}
        self.assertIsNone(r.on_llm_request(request=request, base_url="http://127.0.0.1:8317/v1"))

    def test_disabled_complexity_noop(self):
        r = _router(complexity_routing=False)
        self.assertIsNone(r.on_llm_request(request=req("#max huge"), base_url="http://127.0.0.1:8317/v1"))


if __name__ == "__main__":
    unittest.main()
