"""Auxiliary egress guard extension: cloud proxies on loopback are cloud, and a
``#privacy``/``#private`` payload never reaches them (fail-closed).

Run: python3 -m unittest discover -s scripts/tests -p 'test_aux_guard.py'

Offline: a fake ``agent.aux_egress_guard`` module stands in for Hermes core.
The Hermes-native wire test lives in test_local_route.py (HermesNativeTest).
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from typing import NamedTuple
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _loader import load_plugin, submodule  # noqa: E402

ag = submodule("aux_guard")
config_mod = submodule("router_config")


class PrivacyResult(NamedTuple):
    private: bool
    label: str


def fake_core():
    """Minimal stand-in for Hermes' agent.aux_egress_guard: loopback is local,
    only #private/#local tags are private."""
    mod = types.ModuleType("fake_aux_egress_guard")
    mod.PrivacyResult = PrivacyResult

    def is_local_endpoint(url):
        return "127.0.0.1" in str(url or "") or "localhost" in str(url or "")

    def scan_payload(messages):
        text = repr(messages)
        return PrivacyResult(True, "tag") if ("#private" in text or "#local" in text) else PrivacyResult(False, "public")

    mod.is_local_endpoint = is_local_endpoint
    mod.scan_payload = scan_payload
    return mod


CLIPROXY = "http://127.0.0.1:8317/v1"
PROXIES = (CLIPROXY, "http://127.0.0.1:3456/v1")


class CloudProxyTest(unittest.TestCase):
    def test_loopback_cloud_proxies_are_cloud(self):
        for url in (CLIPROXY, "http://localhost:8317/v1", "http://127.0.0.1:3456/v1",
                    "http://cliproxyapi:8317/v1", "http://claude-max-proxy:3456"):
            self.assertTrue(ag.is_cloud_proxy(url, PROXIES), url)

    def test_configured_proxy_on_another_port(self):
        self.assertTrue(ag.is_cloud_proxy("http://127.0.0.1:9999/v1", ("http://localhost:9999/v1",)))
        self.assertFalse(ag.is_cloud_proxy("http://127.0.0.1:9999/v1", PROXIES))

    def test_ollama_is_not_a_cloud_proxy(self):
        for url in ("http://127.0.0.1:11434/v1", "http://100.64.0.10:11434/v1", "", None):
            self.assertFalse(ag.is_cloud_proxy(url, PROXIES), url)

    def test_proxy_urls_from_settings_and_env(self):
        s = config_mod.load_settings(lambda k, d=None: d)
        with mock.patch.dict(os.environ, {"CLIPROXY_BASE_URL": "http://127.0.0.1:18317/v1",
                                          "CLAUDE_MAX_PROXY_BASE_URL": "http://127.0.0.1:13456/v1"}):
            urls = ag.proxy_urls(s)
        self.assertIn("http://127.0.0.1:18317/v1", urls)
        self.assertIn("http://127.0.0.1:13456/v1", urls)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.core = fake_core()
        self.assertTrue(ag.install(PROXIES, core=self.core))

    def test_cloud_proxy_no_longer_local_ollama_still_local(self):
        self.assertFalse(self.core.is_local_endpoint(CLIPROXY))
        self.assertTrue(self.core.is_local_endpoint("http://127.0.0.1:11434/v1"))

    def test_privacy_marker_is_private(self):
        for text in ("#privacy\nmy salary is 1", "notes: #PRIVATE plan", "[USER]: #privacy what now"):
            r = self.core.scan_payload([{"role": "user", "content": text}])
            self.assertTrue(r.private, text)
            self.assertIsInstance(r, PrivacyResult)
        nested = [{"role": "user", "content": [{"type": "text", "text": "x"},
                                                {"type": "text", "text": "#privacy y"}]}]
        self.assertTrue(self.core.scan_payload(nested).private)
        self.assertEqual(self.core.scan_payload([{"role": "user", "content": "#privacy"}]).label, "privacy-marker")

    def test_public_payload_stays_public(self):
        r = self.core.scan_payload([{"role": "user", "content": "summarize the build log"}])
        self.assertEqual(r, PrivacyResult(False, "public"))
        # Word-bounded like the plugin marker: not a marker inside another token.
        self.assertFalse(self.core.scan_payload([{"role": "user", "content": "see foo#privacy-policy"}]).private)

    def test_core_private_result_wins(self):
        self.assertEqual(self.core.scan_payload([{"content": "#local only"}]), PrivacyResult(True, "tag"))

    def test_scanner_error_fails_closed(self):
        with mock.patch.object(ag, "_has_marker", side_effect=ValueError("boom")):
            r = self.core.scan_payload([{"content": "plain"}])
        self.assertEqual(r, PrivacyResult(True, "scan-error"))

    def test_locality_error_fails_toward_cloud(self):
        with mock.patch.object(ag, "is_cloud_proxy", side_effect=ValueError("boom")):
            self.assertFalse(self.core.is_local_endpoint("http://127.0.0.1:11434/v1"))

    def test_install_is_idempotent(self):
        first = self.core.scan_payload
        self.assertTrue(ag.install(PROXIES, core=self.core))
        self.assertIs(self.core.scan_payload, first)


class RegisterTest(unittest.TestCase):
    def test_missing_core_guard_is_reported(self):
        with self.assertLogs("hermes.plugin.model_router", "ERROR") as logs:
            self.assertFalse(ag.install(PROXIES, core=None, import_name="no_such_module_for_test"))
        self.assertIn("aux privacy guard unavailable", "\n".join(logs.output))

    def test_register_installs_the_guard(self):
        plugin = load_plugin()
        ctx = types.SimpleNamespace(register_middleware=lambda *a: None, register_hook=lambda *a: None,
                                    get_config=lambda k, d=None: d, profile_name="default")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLIPROXY_", "CLAUDE_MAX_PROXY_"))}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(ag, "install", return_value=True) as inst:
            plugin.register(ctx)
        inst.assert_called_once()
        self.assertIn(CLIPROXY, inst.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
