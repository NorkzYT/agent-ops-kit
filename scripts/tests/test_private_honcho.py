"""Private Honcho route: #private marker, fail-closed validator, typed seam refusal.

Hermetic: no network, no ~/.hermes, explicit env mappings only.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import REPO, submodule  # noqa: E402

ph = submodule("private_honcho")
marker = submodule("privacy_marker")

PUBLIC_ENV = {"HONCHO_PORT": "8000", "HONCHO_WORKSPACE": "agent-ops", "CLIPROXY_PORT": "8317",
              "CLAUDE_MAX_PROXY_PORT": "3456", "HONCHO_MODEL": "gpt-6.1-terra"}
GOOD = {
    "PRIVATE_HONCHO_BASE_URL": "http://127.0.0.1:8100",
    "PRIVATE_HONCHO_WORKSPACE": "agent-ops-private",
    "PRIVATE_HONCHO_REASONING_BASE_URL": "http://127.0.0.1:11434/v1",
    "PRIVATE_HONCHO_MODEL": "qwen3.5:27b",
    "PRIVATE_HONCHO_EMBED_BASE_URL": "http://ollama:11434/v1",
}


def req(text):
    return {"messages": [{"role": "user", "content": text}]}


def cfg(**overrides):
    return ph.load_private({**GOOD, **overrides})


def public():
    return ph.load_public(PUBLIC_ENV)


class TestPrivateMarker(unittest.TestCase):
    def test_private_and_privacy_are_equivalent(self):
        for text in ("#private draft this", "draft this #PRIVATE", "#privacy draft this", "(#private) x"):
            with self.subTest(text=text):
                self.assertTrue(marker.decide(req(text)).private)

    def test_private_marker_keeps_provenance_rules(self):
        cases = {
            "boundary": req("#private-beta page"),
            "suffix": req("#privates"),
            "quoted": req("> #private\ndraft this"),
            "fenced": req("```\n#private\n```\ndraft this"),
            "memory": req("draft this\n<memory-context>\n#private\n</memory-context>"),
            "backfill": req("[alice] #private notes\n\n[New message]\n[operator] draft this"),
            "system": {"messages": [{"role": "system", "content": "#private"},
                                    {"role": "user", "content": "draft this"}]},
            "earlier-turn": {"messages": [{"role": "user", "content": "#private old"},
                                          {"role": "assistant", "content": "ok"},
                                          {"role": "user", "content": "now this"}]},
            "local-inert": req("#local draft this"),
        }
        for name, request in cases.items():
            with self.subTest(source=name):
                self.assertFalse(marker.decide(request).private)

    def test_earlier_private_turn_is_withheld_from_public(self):
        request = {"messages": [{"role": "user", "content": "#private my secret"},
                                {"role": "assistant", "content": "local answer"},
                                {"role": "user", "content": "public question"}]}
        out, n = marker.withhold_marked_history(request)
        self.assertEqual(n, 2)
        self.assertNotIn("my secret", json.dumps(out))


class TestValidator(unittest.TestCase):
    def test_valid_local_ollama_config(self):
        c = cfg()
        self.assertIs(ph.validate(c, public()), c)

    def assertRefused(self, kind, private, pub=None):
        with self.assertRaises(ph.PrivateHonchoRefused) as ctx:
            ph.validate(private, pub or public())
        self.assertEqual(ctx.exception.kind, kind)

    def test_disabled_and_incomplete(self):
        self.assertRefused("disabled", ph.load_private({}))
        for key in ("PRIVATE_HONCHO_WORKSPACE", "PRIVATE_HONCHO_REASONING_BASE_URL",
                    "PRIVATE_HONCHO_MODEL", "PRIVATE_HONCHO_EMBED_BASE_URL"):
            with self.subTest(missing=key):
                self.assertRefused("config", cfg(**{key: ""}))

    def test_rejects_cliproxyapi_and_claude_max_proxy(self):
        for url in ("http://cliproxyapi:8317/v1", "http://127.0.0.1:8317/v1", "http://localhost:3456/v1",
                    "http://claude-max-proxy:11434/v1", "http://10.0.0.5:8317/v1"):
            for key in ("PRIVATE_HONCHO_REASONING_BASE_URL", "PRIVATE_HONCHO_EMBED_BASE_URL"):
                with self.subTest(url=url, key=key):
                    self.assertRefused("cloud-proxy", cfg(**{key: url}))

    def test_rejects_configured_proxy_urls(self):
        pub = ph.load_public({**PUBLIC_ENV, "CLIPROXY_BASE_URL": "http://127.0.0.1:11434/v1"})
        self.assertRefused("cloud-proxy", cfg(), pub)

    def test_rejects_public_endpoints(self):
        for url in ("https://api.openai.com/v1", "https://ollama.com/v1", "http://8.8.8.8:11434/v1",
                    "http://134744072:11434/v1", "not a url", "https://example.com:11434"):
            with self.subTest(url=url):
                with self.assertRaises(ph.PrivateHonchoRefused) as ctx:
                    ph.validate(cfg(PRIVATE_HONCHO_REASONING_BASE_URL=url), public())
                self.assertIn(ctx.exception.kind, ("public-endpoint", "config"))

    def test_requires_ollama(self):
        for url in ("http://127.0.0.1:8080/v1", "http://192.168.1.9:5000/v1", "http://vllm:8000/v1"):
            with self.subTest(url=url):
                self.assertRefused("not-ollama", cfg(PRIVATE_HONCHO_REASONING_BASE_URL=url))

    def test_private_honcho_must_be_a_separate_local_instance(self):
        for url in ("http://127.0.0.1:8000", "http://localhost:8000", "http://0.0.0.0:8000/"):
            with self.subTest(url=url):
                self.assertRefused("same-instance", cfg(PRIVATE_HONCHO_BASE_URL=url))
        self.assertRefused("public-endpoint", cfg(PRIVATE_HONCHO_BASE_URL="https://honcho.example.com"))
        self.assertRefused("cloud-proxy", cfg(PRIVATE_HONCHO_BASE_URL="http://127.0.0.1:8317"))

    def test_workspace_switch_alone_is_refused(self):
        # Same server, different workspace: that server's reasoning is CLIProxyAPI.
        self.assertRefused("same-instance", cfg(PRIVATE_HONCHO_BASE_URL="http://127.0.0.1:8000",
                                                PRIVATE_HONCHO_WORKSPACE="other"))


class TestRouteDecision(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.pub = public()

    def _dispatch(self, route):
        return ph.dispatch(route, public_call=lambda c: self.calls.append(("public", c)) or "pub",
                           private_call=lambda c: self.calls.append(("private", c)) or "priv")

    def test_public_turn_uses_public_settings_unchanged(self):
        route = ph.decide(False, private=cfg(), public=self.pub)
        self.assertEqual(route.route, "public")
        self.assertIs(route.public, self.pub)
        self.assertEqual(self._dispatch(route), "pub")
        self.assertEqual(self.calls, [("public", self.pub)])
        self.assertEqual(self.pub, ph.load_public(PUBLIC_ENV))

    def test_marked_turn_is_refused_at_the_hermes_seam(self):
        self.assertFalse(ph.HERMES_MEMORY_ROUTE_SEAM)
        route = ph.decide(True, private=cfg(), public=self.pub)
        self.assertEqual((route.route, route.reason), ("refuse", "seam"))
        with self.assertRaises(ph.PrivateHonchoRefused) as ctx:
            self._dispatch(route)
        self.assertEqual(ctx.exception.kind, "seam")
        self.assertEqual(self.calls, [])

    def test_marked_turn_with_bad_config_never_calls_public(self):
        for private in (ph.load_private({}), cfg(PRIVATE_HONCHO_REASONING_BASE_URL="http://cliproxyapi:8317/v1"),
                        cfg(PRIVATE_HONCHO_BASE_URL="http://127.0.0.1:8000")):
            for seam in (False, True):
                with self.subTest(private=private.base_url, seam=seam):
                    route = ph.decide(True, private=private, public=self.pub, seam=seam)
                    self.assertEqual(route.route, "refuse")
                    self.assertIsNone(route.public)
                    with self.assertRaises(ph.PrivateHonchoRefused):
                        self._dispatch(route)
        self.assertEqual(self.calls, [])

    def test_seam_and_valid_config_select_private_only(self):
        route = ph.decide(True, private=cfg(), public=self.pub, seam=True)
        self.assertEqual(route.route, "private")
        self.assertIsNone(route.public)
        self.assertEqual(self._dispatch(route), "priv")
        self.assertEqual([k for k, _ in self.calls], ["private"])

    def test_malformed_route_refuses(self):
        for route in (ph.MemoryRoute("public"), ph.MemoryRoute("private"), ph.MemoryRoute("other")):
            with self.subTest(route=route.route):
                with self.assertRaises(ph.PrivateHonchoRefused):
                    self._dispatch(route)
        self.assertEqual(self.calls, [])

    def test_refusal_carries_no_content(self):
        route = ph.decide(True, private=cfg(), public=self.pub)
        try:
            self._dispatch(route)
        except ph.PrivateHonchoRefused as exc:
            self.assertNotIn("qwen", str(exc))
            self.assertNotIn("11434", str(exc))


class TestPublicHonchoUnchanged(unittest.TestCase):
    def test_load_public_ignores_private_settings(self):
        self.assertEqual(ph.load_public({**PUBLIC_ENV, **GOOD}), ph.load_public(PUBLIC_ENV))
        self.assertEqual(ph.load_public(PUBLIC_ENV).base_url, "http://127.0.0.1:8000")

    def test_honcho_template_has_only_the_public_host(self):
        tmpl = json.loads((REPO / "hermes" / "honcho.json.tmpl").read_text())
        self.assertEqual(tmpl["baseUrl"], "__HONCHO_BASE_URL__")
        self.assertEqual(list(tmpl["hosts"]), ["hermes"])

    def test_compose_public_honcho_is_not_rerouted(self):
        compose = (REPO / "docker-compose.yml").read_text()
        self.assertNotIn("PRIVATE_HONCHO", compose)
        urls = re.findall(r"MODEL_CONFIG__OVERRIDES__BASE_URL: (\S+)", compose)
        self.assertTrue(urls)
        self.assertEqual(set(urls), {"http://cliproxyapi:8317/v1", "http://ollama:11434/v1"})

    def test_module_reads_no_live_state(self):
        src = inspect.getsource(ph)
        for needle in (".hermes", "urlopen", "socket.create_connection", "import requests"):
            self.assertNotIn(needle, src)


class TestHermesSeamDocumented(unittest.TestCase):
    def test_docs_state_the_refusal(self):
        doc = (REPO / "docs" / "model-routing.md").read_text()
        self.assertIn("HERMES_MEMORY_ROUTE_SEAM", doc)
        self.assertIn("#private", doc)


if __name__ == "__main__":
    unittest.main()
