"""Newest-in-family Sol/Astra routing from the live CLIProxyAPI model catalog.

Run: python3 -m unittest discover -s scripts/tests -p 'test_model_catalog.py'

No network: every catalog fetch goes through an injected fake opener, and the
synthetic bearer value below is not a real credential.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _loader import load_plugin, submodule  # noqa: E402

complexity = submodule("complexity")
try:
    catalog_mod = submodule("model_catalog")
except ModuleNotFoundError:  # RED stage: module not written yet
    catalog_mod = SimpleNamespace(MAX_BYTES=0, MAX_IDS=0)
router_mod = submodule("router")
config_mod = submodule("router_config")

FAKE_KEY = "test-not-a-real-key-0000"
LOGGER = "hermes.plugin.model_router"
LIVE = ["gpt-5.6-luna", "gpt-6.1-terra", "gpt-5.6-sol", "gpt-6.1-sol", "gpt-6.2-sol",
        "gpt-6-astra", "gpt-6.1-astra"]

MALFORMED = [
    "GPT-6.9-SOL", "Gpt-6.9-sol", " gpt-6.9-sol", "gpt-6.9-sol ", "gpt-6.9-sol\n",
    "gpt-6.9-sol-preview", "gpt-6.9-sol-mini", "openai/gpt-6.9-sol", "gpt-6.9.1-sol",
    "gpt-06.9-sol", "gpt-6.09-sol", "gpt-6.-sol", "gpt-.9-sol", "gpt-6.9sol",
    "gpt-6.9_sol", "gpt-6.9-Sol", "gpt-6.9-sols", "gpt6.9-sol", "gpt-6.9-",
    "gpt-1234-sol", "gpt-6.1234-sol", "gpt-６-sol", "gpt-6.9-sol\x00", "",
]


def _user(text):
    return {"model": "gpt-6.1-terra", "messages": [{"role": "user", "content": text}]}


class FakeCatalog:
    """Stand-in live catalog: counts lookups, returns a fixed snapshot."""

    def __init__(self, ids):
        self.ids = None if ids is None else tuple(ids)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self.ids


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests; replies with a body, or raises ``exc``."""

    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.requests = body, exc, []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.exc is not None:
            raise self.exc
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return _Resp(raw)


def _models(*ids):
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _router(catalog=None, **settings):
    base = dict(complexity_routing=True, privacy_routing=True, ollama_base_url="", ollama_model="")
    base.update(settings)
    return router_mod.Router(config_mod.RouterSettings(**base), gpt_catalog=catalog)


def _route(router, text, *, model="gpt-6.1-terra", base_url="http://127.0.0.1:8317/v1", sid="s1", rid=""):
    req = dict(_user(text), model=model)
    out = router.on_llm_request(request=req, base_url=base_url, session_id=sid, api_request_id=rid)
    return (out or {}).get("request", req)["model"]


# --------------------------------------------------------------------- parsing
class TestStrictModelIds(unittest.TestCase):
    def test_parses_canonical_ids(self):
        self.assertEqual(complexity.parse_family_model("gpt-6.2-sol"), ("sol", (6, 2)))
        self.assertEqual(complexity.parse_family_model("gpt-6.10-sol"), ("sol", (6, 10)))
        self.assertEqual(complexity.parse_family_model("gpt-6-astra"), ("astra", (6, 0)))
        self.assertEqual(complexity.parse_family_model("gpt-7-astra"), ("astra", (7, 0)))
        self.assertEqual(complexity.parse_family_model("gpt-5.6-luna"), ("luna", (5, 6)))
        self.assertEqual(complexity.parse_family_model("gpt-6.1-terra"), ("terra", (6, 1)))

    def test_rejects_malformed_variants(self):
        for bad in MALFORMED + [None, 6, b"gpt-6.9-sol", ["gpt-6.9-sol"]]:
            with self.subTest(bad=bad):
                self.assertIsNone(complexity.parse_family_model(bad))


class TestNewestInFamily(unittest.TestCase):
    def test_6_2_beats_6_1(self):
        self.assertEqual(complexity.newest_family_model("sol", ["gpt-6.1-sol", "gpt-6.2-sol"]), "gpt-6.2-sol")
        self.assertEqual(complexity.newest_family_model("sol", ["gpt-6.2-sol", "gpt-6.1-sol"]), "gpt-6.2-sol")

    def test_numeric_not_lexical_ordering(self):
        ids = ["gpt-6.9-sol", "gpt-6.10-sol", "gpt-6.2-sol", "gpt-5.60-sol"]
        self.assertEqual(complexity.newest_family_model("sol", ids), "gpt-6.10-sol")
        self.assertEqual(complexity.newest_family_model("sol", ids + ["gpt-7-sol"]), "gpt-7-sol")
        self.assertEqual(complexity.newest_family_model("astra", ["gpt-6-astra", "gpt-6.1-astra"]),
                         "gpt-6.1-astra")

    def test_malformed_and_other_families_ignored(self):
        ids = ["gpt-6.1-sol"] + [m.replace("6.9", "9.9") for m in MALFORMED] + ["gpt-9-astra", "gpt-9-terra"]
        self.assertEqual(complexity.newest_family_model("sol", ids), "gpt-6.1-sol")
        self.assertIsNone(complexity.newest_family_model("sol", ["gpt-9-astra", "GPT-9-SOL"]))
        self.assertIsNone(complexity.newest_family_model("sol", []))
        self.assertIsNone(complexity.newest_family_model("bogus", ["gpt-6.1-sol"]))


class TestResolveNewest(unittest.TestCase):
    def test_sol_tier_picks_newest_live_sol(self):
        self.assertEqual(complexity.resolve_model("high", family="gpt", available=LIVE), "gpt-6.2-sol")

    def test_authorized_max_picks_newest_live_astra(self):
        self.assertEqual(complexity.resolve_model("max", family="gpt", available=LIVE, allow_max=True),
                         "gpt-6.1-astra")

    def test_unauthorized_max_is_newest_sol_never_astra(self):
        live = LIVE + ["gpt-9-astra"]
        self.assertEqual(complexity.resolve_model("max", family="gpt", available=live), "gpt-6.2-sol")
        self.assertIsNone(complexity.resolve_model("routine", family="gpt", available=["gpt-9-astra"]))

    def test_older_version_only_on_availability_fallback(self):
        self.assertEqual(complexity.resolve_model("high", family="gpt", available=["gpt-6.1-sol", "gpt-5.6-sol"]),
                         "gpt-6.1-sol")
        self.assertEqual(complexity.resolve_model("high", family="gpt", available=["gpt-5.6-sol"]), "gpt-5.6-sol")
        self.assertEqual(complexity.resolve_model("max", family="gpt", allow_max=True,
                                                  available=["gpt-6-astra", "gpt-6.2-sol"]), "gpt-6-astra")

    def test_malformed_newer_ids_are_not_selected(self):
        live = ["gpt-6.1-sol", "gpt-6.9-sol-preview", "GPT-7-SOL", "gpt-7-sol ", "gpt-6.1-astra",
                "gpt-9-astra-mini"]
        self.assertEqual(complexity.resolve_model("high", family="gpt", available=live), "gpt-6.1-sol")
        self.assertEqual(complexity.resolve_model("max", family="gpt", available=live, allow_max=True),
                         "gpt-6.1-astra")

    def test_unknown_live_set_keeps_static_primary(self):
        self.assertEqual(complexity.resolve_model("high", family="gpt"), "gpt-6.1-sol")
        self.assertEqual(complexity.resolve_model("max", family="gpt", allow_max=True), "gpt-6-astra")

    def test_coding_family_never_uses_gpt_ids(self):
        self.assertIsNone(complexity.resolve_model("high", family="coding", available=LIVE))

    def test_strict_ids_are_managed_by_tier(self):
        self.assertEqual(complexity.alias_to_tier("gpt-6.2-sol"), "high")
        self.assertEqual(complexity.alias_to_tier("gpt-7-astra"), "max")
        self.assertEqual(complexity.alias_to_tier("gpt-6.10-astra"), "max")
        self.assertIsNone(complexity.alias_to_tier("gpt-7-astra-preview"))
        self.assertIsNone(complexity.alias_to_tier("openai/gpt-7-astra"))


# --------------------------------------------------------------- live catalog
class TestLiveCatalog(unittest.TestCase):
    def _cat(self, opener, clock=None, **kw):
        env = mock.patch.dict(os.environ, {"TEST_CATALOG_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        return catalog_mod.GptModelCatalog("http://127.0.0.1:8317/v1", "TEST_CATALOG_KEY",
                                           opener=opener, clock=clock or Clock(), **kw)

    def test_fetches_models_with_bearer_and_bounded_timeout(self):
        op = FakeOpener(_models(*LIVE))
        self.assertEqual(self._cat(op).snapshot(), tuple(LIVE))
        req, timeout = op.requests[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8317/v1/models")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertIsNone(req.data)
        self.assertLessEqual(timeout, 5.0)

    def test_ttl_cache_single_fetch_then_refresh(self):
        clock = Clock()
        op = FakeOpener(_models("gpt-6.1-sol"))
        cat = self._cat(op, clock, ttl=300)
        cat.snapshot(); cat.snapshot()
        self.assertEqual(len(op.requests), 1)
        op.body = _models("gpt-6.1-sol", "gpt-6.2-sol")
        clock.t += 301
        self.assertEqual(cat.snapshot(), ("gpt-6.1-sol", "gpt-6.2-sol"))
        self.assertEqual(len(op.requests), 2)

    def test_failures_are_unavailable(self):
        cases = {
            "http": FakeOpener(exc=urllib.error.HTTPError("u", 401, "no", {}, None)),
            "timeout": FakeOpener(exc=TimeoutError()),
            "connect": FakeOpener(exc=urllib.error.URLError(ConnectionRefusedError())),
            "not-json": FakeOpener(b"<html>"),
            "no-data": FakeOpener({"object": "list"}),
            "data-not-list": FakeOpener({"data": {"id": "gpt-6.2-sol"}}),
            "entry-not-object": FakeOpener({"data": ["gpt-6.2-sol"]}),
            "id-not-string": FakeOpener({"data": [{"id": 6.2}]}),
            "oversized": FakeOpener(b" " * (catalog_mod.MAX_BYTES + 1)),
            "too-many": FakeOpener(_models(*[f"m{i}" for i in range(catalog_mod.MAX_IDS + 1)])),
        }
        for kind, op in cases.items():
            with self.subTest(kind=kind):
                self.assertIsNone(self._cat(op).snapshot())

    def test_failure_is_negatively_cached(self):
        clock = Clock()
        op = FakeOpener(exc=TimeoutError())
        cat = self._cat(op, clock, ttl=300, retry=30)
        cat.snapshot(); cat.snapshot()
        self.assertEqual(len(op.requests), 1)
        clock.t += 31
        cat.snapshot()
        self.assertEqual(len(op.requests), 2)

    def test_last_good_snapshot_survives_a_brief_outage_only(self):
        clock = Clock()
        op = FakeOpener(_models("gpt-6.2-sol"))
        cat = self._cat(op, clock, ttl=300, retry=30, stale=900)
        self.assertEqual(cat.snapshot(), ("gpt-6.2-sol",))
        op.exc = TimeoutError()
        clock.t += 301
        self.assertEqual(cat.snapshot(), ("gpt-6.2-sol",))   # stale but within bound
        clock.t += 900
        self.assertIsNone(cat.snapshot())                     # too old: unavailable

    def test_missing_key_or_bad_url_never_fetches(self):
        op = FakeOpener(_models(*LIVE))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_ABSENT_KEY", None)
            cat = catalog_mod.GptModelCatalog("http://127.0.0.1:8317/v1", "TEST_ABSENT_KEY",
                                              opener=op, clock=Clock())
            self.assertIsNone(cat.snapshot())
        bad = catalog_mod.GptModelCatalog("file:///etc", "TEST_ABSENT_KEY", opener=op, clock=Clock())
        self.assertIsNone(bad.snapshot())
        self.assertEqual(op.requests, [])

    def test_logs_carry_outcome_labels_never_key_or_body(self):
        op = FakeOpener(exc=urllib.error.HTTPError("u", 401, f"bad {FAKE_KEY}", {}, None))
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self._cat(op).snapshot()
            self._cat(FakeOpener(_models("gpt-6.2-sol", "secret-ish-model"))).snapshot()
        text = "\n".join(logs.output)
        self.assertIn("unavailable:http", text)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("secret-ish-model", text)

    def test_default_opener_ignores_env_proxies_and_redirects(self):
        with mock.patch.dict(os.environ, {"http_proxy": "http://proxy.invalid:1",
                                          "HTTP_PROXY": "http://proxy.invalid:1"}):
            opener = catalog_mod._OPENER
        for h in opener.handlers:
            self.assertFalse(getattr(h, "proxies", None), "env proxy must not be used")
        redirect = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirect), 1)
        req = urllib.request.Request("http://127.0.0.1:8317/v1/models",
                                     headers={"Authorization": f"Bearer {FAKE_KEY}"})
        self.assertIsNone(redirect[0].redirect_request(req, None, 302, "Found", {}, "http://evil.invalid/"))

    def test_from_settings_respects_off_switch(self):
        s = config_mod.RouterSettings(gpt_catalog="off")
        self.assertIsNone(catalog_mod.from_settings(s))
        s = config_mod.RouterSettings(gpt_catalog="live", gpt_catalog_base_url="http://127.0.0.1:9/v1")
        cat = catalog_mod.from_settings(s)
        self.assertIsInstance(cat, catalog_mod.GptModelCatalog)
        self.assertEqual(cat.url, "http://127.0.0.1:9/v1/models")

    def test_from_settings_never_uses_specialist_classifier_endpoint(self):
        # The classifier endpoint/key are a separate credential boundary: the
        # catalog bearer (gpt_catalog_key_env) must never go to it.
        spec = "http://specialist.invalid:7001/v1"
        env = {"CLIPROXY_BASE_URL": "http://cliproxy.invalid:7002/v1/", "CLIPROXY_PORT": "7003",
               "SYNTH_CATALOG_KEY": "k-catalog", "SYNTH_SPECIALIST_KEY": "k-spec"}
        base = dict(gpt_catalog="live", gpt_catalog_key_env="SYNTH_CATALOG_KEY",
                    specialist_classifier="cliproxy", specialist_classifier_base_url=spec,
                    specialist_classifier_key_env="SYNTH_SPECIALIST_KEY")
        with mock.patch.dict(os.environ, env):
            cat = catalog_mod.from_settings(config_mod.RouterSettings(**base))
            self.assertEqual(cat.url, "http://cliproxy.invalid:7002/v1/models")
            self.assertEqual(cat.key_env, "SYNTH_CATALOG_KEY")
            explicit = catalog_mod.from_settings(config_mod.RouterSettings(
                **base, gpt_catalog_base_url="http://catalog.invalid:7004/v1"))
            self.assertEqual(explicit.url, "http://catalog.invalid:7004/v1/models")
        env.pop("CLIPROXY_BASE_URL")
        with mock.patch.dict(os.environ, env):
            os.environ.pop("CLIPROXY_BASE_URL", None)
            cat = catalog_mod.from_settings(config_mod.RouterSettings(**base))
            self.assertEqual(cat.url, "http://127.0.0.1:7003/v1/models")
            os.environ.pop("CLIPROXY_PORT")
            cat = catalog_mod.from_settings(config_mod.RouterSettings(**base))
            self.assertEqual(cat.url, "http://127.0.0.1:8317/v1/models")
        # The bearer actually sent goes to the CLIProxyAPI host, not the classifier's.
        opener = FakeOpener(_models("gpt-6.2-sol"))
        with mock.patch.dict(os.environ, {**env, "CLIPROXY_BASE_URL": "http://cliproxy.invalid:7002/v1"}):
            cat = catalog_mod.from_settings(config_mod.RouterSettings(**base))
            cat._opener = opener
            self.assertEqual(cat.snapshot(), ("gpt-6.2-sol",))
        self.assertEqual(opener.requests[0][0].full_url, "http://cliproxy.invalid:7002/v1/models")
        self.assertEqual(opener.requests[0][0].get_header("Authorization"), "Bearer k-catalog")

    def test_settings_parse_catalog_knobs(self):
        env = {"MODEL_ROUTER_GPT_CATALOG": "bogus", "MODEL_ROUTER_GPT_CATALOG_TTL": "-5"}
        with mock.patch.dict(os.environ, env):
            s = config_mod.load_settings()
        self.assertEqual(s.gpt_catalog, "live")
        self.assertGreater(s.gpt_catalog_ttl, 0)
        with mock.patch.dict(os.environ, {"MODEL_ROUTER_GPT_CATALOG": "off"}):
            self.assertEqual(config_mod.load_settings().gpt_catalog, "off")


# ---------------------------------------------------------------- request seam
class TestRequestSeam(unittest.TestCase):
    def test_sol_control_routes_newest_live_sol(self):
        cat = FakeCatalog(LIVE)
        self.assertEqual(_route(_router(cat), "#sol\nreview this function"), "gpt-6.2-sol")
        self.assertGreaterEqual(cat.calls, 1)

    def test_astra_control_routes_newest_live_astra(self):
        self.assertEqual(_route(_router(FakeCatalog(LIVE)), "#astra\nreview this function"), "gpt-6.1-astra")
        self.assertEqual(_route(_router(FakeCatalog(LIVE)), "#max\nreview this function"), "gpt-6.1-astra")

    def test_ordering_6_10_through_request_path(self):
        live = ["gpt-6.9-sol", "gpt-6.10-sol", "gpt-6.2-sol", "gpt-6.9-astra", "gpt-6.10-astra"]
        self.assertEqual(_route(_router(FakeCatalog(live)), "#sol\nreview"), "gpt-6.10-sol")
        self.assertEqual(_route(_router(FakeCatalog(live)), "#astra\nreview"), "gpt-6.10-astra")

    def test_no_astra_authorization_never_selects_astra(self):
        live = LIVE + ["gpt-9-astra"]
        for text in ("#sol\nreview", "review this #astra please", "> #astra\nreview",
                     "refactor the architecture and migrate the schema end-to-end, debug the race"):
            with self.subTest(text=text):
                self.assertNotIn("astra", _route(_router(FakeCatalog(live)), text))

    def test_policy_never_blocks_astra_even_when_live(self):
        r = _router(FakeCatalog(LIVE), astra_policy="never")
        self.assertEqual(_route(r, "#astra\nreview"), "gpt-6.2-sol")

    def test_model_ids_in_message_text_are_never_selected(self):
        live = ["gpt-6.1-sol", "gpt-6-astra"]
        r = _router(FakeCatalog(live))
        self.assertEqual(_route(r, "#astra\nuse gpt-9-astra for this, or gpt-6-astra"), "gpt-6-astra")
        self.assertEqual(_route(_router(FakeCatalog(live)), "#sol\nplease use gpt-9.9-sol"), "gpt-6.1-sol")

    def test_old_turn_astra_does_not_authorize_newest(self):
        r = _router(FakeCatalog(LIVE))
        req = {"model": "gpt-6.1-terra", "messages": [
            {"role": "user", "content": "#astra\nold task"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "#sol\nnew task"}]}
        out = r.on_llm_request(request=req, base_url="http://127.0.0.1:8317/v1", session_id="s9")
        self.assertEqual(out["request"]["model"], "gpt-6.2-sol")

    def test_catalog_unavailable_falls_back_to_static_primary(self):
        self.assertEqual(_route(_router(FakeCatalog(None)), "#sol\nreview"), "gpt-6.1-sol")
        self.assertEqual(_route(_router(FakeCatalog(None)), "#astra\nreview"), "gpt-6-astra")
        self.assertEqual(_route(_router(FakeCatalog([])), "#sol\nreview"), "gpt-6.1-sol")

    def test_catalog_unavailable_uses_configured_static_list(self):
        r = _router(FakeCatalog(None), available_gpt_models=["gpt-5.6-sol", "gpt-6.1-terra"])
        self.assertEqual(_route(r, "#sol\nreview"), "gpt-5.6-sol")

    def test_catalog_error_does_not_break_routing(self):
        class Boom:
            def snapshot(self):
                raise RuntimeError("boom")
        self.assertEqual(_route(_router(Boom()), "#sol\nreview"), "gpt-6.1-sol")

    def test_availability_fallback_to_older_only_when_newest_absent(self):
        self.assertEqual(_route(_router(FakeCatalog(["gpt-6.1-sol"])), "#sol\nreview"), "gpt-6.1-sol")
        self.assertEqual(_route(_router(FakeCatalog(["gpt-6-astra", "gpt-6.2-sol"])), "#astra\nreview"),
                         "gpt-6-astra")

    def test_private_turn_never_consults_catalog(self):
        cat = FakeCatalog(LIVE)
        _route(_router(cat), "#privacy\n#sol\nreview")
        _route(_router(cat), "#sol\n#privacy review")
        self.assertEqual(cat.calls, 0)

    def test_coding_seam_never_consults_gpt_catalog(self):
        cat = FakeCatalog(LIVE)
        got = _route(_router(cat), "#sol\nreview", model="opus", base_url="http://127.0.0.1:3456/v1")
        self.assertEqual(got, "fable")
        self.assertEqual(cat.calls, 0)

    def test_route_log_has_no_catalog_contents(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            _route(_router(FakeCatalog(LIVE + ["private-looking-id"])), "#sol\nreview")
        text = "\n".join(logs.output)
        self.assertIn("catalog=live", text)
        self.assertNotIn("private-looking-id", text)

    def test_live_catalog_end_to_end_through_router(self):
        op = FakeOpener(_models(*LIVE))
        with mock.patch.dict(os.environ, {"TEST_CATALOG_KEY": FAKE_KEY}):
            cat = catalog_mod.GptModelCatalog("http://127.0.0.1:8317/v1", "TEST_CATALOG_KEY",
                                              opener=op, clock=Clock())
            self.assertEqual(_route(_router(cat), "#sol\nreview"), "gpt-6.2-sol")
        self.assertEqual(len(op.requests), 1)


# -------------------------------------------------------------- execution seam
class TestExecutionSeam(unittest.TestCase):
    def _exec(self, router, text, model, rid=""):
        calls = []
        resp = router.on_llm_execution(request=dict(_user(text), model=model), session_id="x",
                                       api_request_id=rid, base_url="http://127.0.0.1:8317/v1",
                                       next_call=lambda r: calls.append(r) or {"model": r["model"]})
        return resp, calls

    def test_unauthorized_newer_astra_is_refused_unsent(self):
        for model in ("gpt-7-astra", "gpt-6.1-astra", "gpt-6.10-astra"):
            with self.subTest(model=model):
                _resp, calls = self._exec(_router(FakeCatalog(LIVE)), "review this", model)
                self.assertEqual(calls, [])

    def test_authorized_newest_astra_is_sent(self):
        r = _router(FakeCatalog(LIVE))
        req = dict(_user("#astra\nreview"))
        out = r.on_llm_request(request=req, base_url="http://127.0.0.1:8317/v1", session_id="x",
                               api_request_id="r1")
        self.assertEqual(out["request"]["model"], "gpt-6.1-astra")
        _resp, calls = self._exec(r, "#astra\nreview", "gpt-6.1-astra", rid="r1")
        self.assertEqual([c["model"] for c in calls], ["gpt-6.1-astra"])


# -------------------------------------------------------------------- wiring
class TestRegisterWiring(unittest.TestCase):
    def _register(self, env):
        plugin = load_plugin()
        seen = {}

        class Ctx:
            profile_name = "default"

            def get_config(self, key, default=None):
                return None

            def register_middleware(self, kind, fn):
                seen[kind] = fn

            def register_hook(self, *a, **k):
                pass

            def register_command(self, *a, **k):
                pass

        with mock.patch.dict(os.environ, env):
            plugin.register(Ctx())
        return seen["llm_request"].__self__

    def test_register_wires_live_catalog_without_fetching(self):
        router = self._register({"MODEL_ROUTER_GPT_CATALOG": "live"})
        self.assertIsInstance(router._gpt_catalog, catalog_mod.GptModelCatalog)
        self.assertEqual(router._gpt_catalog.fetches, 0)

    def test_register_off_switch_leaves_static_routing(self):
        router = self._register({"MODEL_ROUTER_GPT_CATALOG": "off"})
        self.assertIsNone(router._gpt_catalog)


if __name__ == "__main__":
    logging.getLogger(LOGGER).setLevel(logging.INFO)
    unittest.main()
