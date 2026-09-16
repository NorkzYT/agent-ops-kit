"""Unit + canary tests for the model-router plugin.

Run: python3 -m unittest discover -s scripts/tests -p 'test_*.py'

No real secrets are used anywhere here — all sensitive-looking values are
synthetic and constructed to satisfy the deterministic detectors' checksums.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
from _loader import submodule  # noqa: E402

privacy = submodule("privacy")
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


class TestComplexity(unittest.TestCase):
    def test_routine(self):
        self.assertEqual(complexity.score_request(req("hi there")).tier, "routine")

    def test_high_keyword(self):
        self.assertIn(complexity.score_request(req("refactor the architecture across the codebase")).tier,
                      ("high", "max"))

    def test_forced_tags(self):
        self.assertEqual(complexity.score_request(req("#max simple thing")).tier, "max")
        self.assertEqual(complexity.score_request(req("#quick big architecture migration rewrite")).tier, "routine")

    def test_gpt_tier_models(self):
        # Corrected order (per OpenAI docs): luna=fastest/cheapest floor,
        # terra=balanced mid, sol=flagship, astra=hardest.
        self.assertEqual(complexity.resolve_model("routine", family="gpt"), "gpt-5.6-luna")
        self.assertEqual(complexity.resolve_model("medium", family="gpt"), "gpt-5.6-terra")
        self.assertEqual(complexity.resolve_model("high", family="gpt"), "gpt-5.6-sol")
        self.assertEqual(complexity.resolve_model("max", family="gpt"), "gpt-6-astra")

    def test_coding_tier_models(self):
        self.assertEqual(complexity.resolve_model("routine", family="coding"), "opus")
        self.assertEqual(complexity.resolve_model("high", family="coding"), "fable")

    def test_degrade_when_unavailable(self):
        # astra gone -> step down to sol
        self.assertEqual(
            complexity.resolve_model("max", family="gpt",
                                     available=["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"]),
            "gpt-5.6-sol",
        )

    def test_degrade_never_upgrades(self):
        # only terra available; requesting high degrades DOWN to terra
        self.assertEqual(
            complexity.resolve_model("high", family="gpt", available=["gpt-5.6-terra"]),
            "gpt-5.6-terra",
        )

    def test_alias_to_tier(self):
        # luna is the routine floor, terra the balanced mid (corrected order).
        self.assertEqual(complexity.alias_to_tier("gpt-5.6-luna"), "routine")
        self.assertEqual(complexity.alias_to_tier("luna"), "routine")
        self.assertEqual(complexity.alias_to_tier("gpt-5.6-terra"), "medium")
        self.assertEqual(complexity.alias_to_tier("terra"), "medium")
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
        spy, resp = self._run(req(f"ssn {SSN}"), "http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0, "cloud provider was called for private data")
        self.assertEqual(spy.bytes, 0, "private bytes reached the cloud")
        self.assertTrue(getattr(resp, "_model_router_refusal", False))

    def test_private_sends_zero_bytes_to_delegation_3456(self):
        spy, resp = self._run(req(f"card {PAN}"), "http://127.0.0.1:3456/v1")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(spy.bytes, 0)

    def test_public_reaches_cloud(self):
        spy, resp = self._run(req("what is 2+2?"), "http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 1)
        self.assertEqual(resp.model, "CLOUD")

    def test_local_backend_answers_private_without_cloud(self):
        spy = _Spy()
        r = _router()
        r._backend = SimpleNamespace(chat=lambda msgs: local_backend.make_response("LOCAL", model="ollama"))
        resp = r.on_llm_execution(request=req(f"ssn {SSN}"), next_call=spy, session_id="s2",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(resp.choices[0].message.content, "LOCAL")

    def test_session_stickiness(self):
        spy = _Spy()
        r = _router()
        # first turn is private
        r.on_llm_execution(request=req(f"ssn {SSN}"), next_call=spy, session_id="sticky",
                           base_url="http://127.0.0.1:8317/v1")
        # later benign turn in the SAME session must stay local
        r.on_llm_execution(request=req("thanks, summarize that"), next_call=spy, session_id="sticky",
                           base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0)

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
        r.on_llm_execution(request=req(f"ssn {SSN}"), next_call=spy, session_id="s4",
                           base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 1)  # documents that the guard is what blocks egress

    def test_middleware_never_raises(self):
        spy = _Spy()
        r = _router()
        # malformed request must not raise out of the guard
        resp = r.on_llm_execution(request=None, next_call=spy, session_id="s5",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertIsNotNone(resp)


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
