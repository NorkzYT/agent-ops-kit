"""Local model roles, bounded local fallback, hybrid specialist classifier and
the installer render that carries them.

Run: python3 -m unittest discover -s scripts/tests -p 'test_local_route.py'

Offline: local endpoints are fakes or a stub HTTP server on 127.0.0.1, the
installer renders into a temp HERMES_HOME from a fixture .env, and the
Hermes-native checks (skipped without a Hermes venv) only parse a temp config.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _loader import REPO, submodule  # noqa: E402

lb = submodule("local_backend")
config_mod = submodule("router_config")
router_mod = submodule("router")
sp = submodule("specialists")
hook_mod = submodule("specialist_hook")

LOCAL = "http://127.0.0.1:11434/v1"
ROLES = dict(ollama_base_url=LOCAL, ollama_model="legacy:1b", ollama_routine_model="qwen3.5:27b",
             ollama_fallback_model="qwen3.6:27b", ollama_agent_model="qwen3.8:27b")
SSN = "219-09-9999"  # synthetic, passes the detector's area/group/serial checks
AMBIGUOUS = ("I keep changing my strategy for the family move and the trade-offs feel endless; "
             "help me think it through calmly.")
NO_MATCH = ("I keep going back and forth on what to do next with my life and the people around "
            "me, and I would like a structured way to think it through.")
HARD = ("Architect a multi-region, zero-downtime migration of our billing database with rollback, "
        "data integrity verification across shards, and a phased cutover plan; analyze the "
        "failure modes and tradeoffs.")


def ready(_name):
    return sp.Readiness("ready", True, 3, "knowledge/x-knowledge")


def answer(text, model="m"):
    return lb.make_response(text, model=model)


class FakeBackend:
    """Per-model fake: ``plan`` maps model -> response text or a LocalUnavailable kind."""

    def __init__(self, plan, calls):
        self.plan, self.calls = plan, calls

    def __call__(self, model):
        backend = SimpleNamespace()

        def chat(messages, **kw):
            self.calls.append((model, kw))
            outcome = self.plan.get(model, "http")
            if outcome in ("http", "timeout", "connect", "shape", "config"):
                raise lb.LocalUnavailable(f"synthetic {outcome}", outcome)
            return answer(outcome, model)

        backend.chat = chat
        return backend


class Spy:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return SimpleNamespace(model="CLOUD", choices=[], usage=None)


def req(text, model="gpt-5.6-luna"):
    return {"model": model, "messages": [{"role": "user", "content": text}]}


# --------------------------------------------------------------------------- #
# Settings: explicit roles, legacy single model, bounded chain
# --------------------------------------------------------------------------- #
class LocalRoleSettingsTest(unittest.TestCase):
    def test_explicit_roles(self):
        s = config_mod.RouterSettings(**ROLES)
        self.assertEqual(s.local_model("routine"), "qwen3.5:27b")
        self.assertEqual(s.local_model("fallback"), "qwen3.6:27b")
        self.assertEqual(s.local_model("agent"), "qwen3.8:27b")
        self.assertEqual(s.local_chain("routine"), ["qwen3.5:27b", "qwen3.6:27b"])
        self.assertEqual(s.local_chain("agent"), ["qwen3.8:27b", "qwen3.6:27b"])

    def test_legacy_single_model_keeps_old_behaviour(self):
        s = config_mod.RouterSettings(ollama_base_url=LOCAL, ollama_model="llama3.1:8b")
        self.assertEqual(s.local_chain("routine"), ["llama3.1:8b"])
        self.assertEqual(s.local_chain("agent"), ["llama3.1:8b"])

    def test_chain_is_deduplicated_and_at_most_two(self):
        s = config_mod.RouterSettings(ollama_base_url=LOCAL, ollama_routine_model="a",
                                      ollama_fallback_model="a", ollama_agent_model="b")
        self.assertEqual(s.local_chain("routine"), ["a"])
        self.assertEqual(s.local_chain("agent"), ["b", "a"])

    def test_settings_from_config_and_env(self):
        cfg = {"ollama_routine_model": "r", "ollama_fallback_model": "f", "ollama_agent_model": "g",
               "specialist_classifier": "local", "specialist_classifier_timeout": "12"}
        s = config_mod.load_settings(lambda k, d: cfg.get(k))
        self.assertEqual((s.local_model("routine"), s.local_model("fallback"), s.local_model("agent")),
                         ("r", "f", "g"))
        self.assertEqual(s.specialist_classifier, "local")
        self.assertEqual(s.specialist_classifier_timeout, 12.0)
        with mock.patch.dict(os.environ, {"OLLAMA_ROUTINE_MODEL": "er", "OLLAMA_AGENT_MODEL": "eg",
                                          "MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT": "999"}):
            env = config_mod.load_settings(None)
        self.assertEqual((env.local_model("routine"), env.local_model("agent")), ("er", "eg"))
        self.assertEqual(env.specialist_classifier_timeout, 25.0)  # capped under the 30 s hook limit


class LocalEndpointTest(unittest.TestCase):
    def test_local_and_remote(self):
        for url in ("http://127.0.0.1:11434/v1", "http://localhost:11434", "http://100.65.5.47:11434/v1",
                    "http://192.168.1.9:11434", "http://10.0.0.2", "http://[::1]:11434",
                    "http://ollama:11434/v1", "http://gpu-box.tail1234.ts.net:11434", "http://gpu.local"):
            self.assertTrue(lb.is_local_endpoint(url), url)
        for url in ("https://api.openai.com/v1", "http://8.8.8.8:11434", "http://100.128.0.1",
                    "https://ollama.example.com/v1", "", "not a url",
                    "http://134744072:11434/v1", "http://0x08080808/v1"):  # numeric 8.8.8.8
            self.assertFalse(lb.is_local_endpoint(url), url)


# --------------------------------------------------------------------------- #
# Bounded local chain
# --------------------------------------------------------------------------- #
class LocalChainTest(unittest.TestCase):
    def test_model_level_failure_tries_fallback_once(self):
        for kind in ("http", "timeout", "shape"):
            calls = []
            resp, model, attempt = lb.run_local_chain(
                ["a", "b"], [], FakeBackend({"a": kind, "b": "ok"}, calls))
            self.assertEqual((model, attempt, resp.choices[0].message.content), ("b", 2, "ok"), kind)
            self.assertEqual([c[0] for c in calls], ["a", "b"])

    def test_endpoint_down_does_not_try_fallback(self):
        calls = []
        with self.assertRaises(lb.LocalUnavailable) as ctx:
            lb.run_local_chain(["a", "b"], [], FakeBackend({"a": "connect", "b": "ok"}, calls))
        self.assertEqual(ctx.exception.kind, "connect")
        self.assertEqual([c[0] for c in calls], ["a"])

    def test_all_fail_raises_and_empty_chain_is_config(self):
        with self.assertRaises(lb.LocalUnavailable):
            lb.run_local_chain(["a", "b"], [], FakeBackend({}, []))
        with self.assertRaises(lb.LocalUnavailable) as ctx:
            lb.run_local_chain(["", ""], [], FakeBackend({}, []))
        self.assertEqual(ctx.exception.kind, "config")

    def test_budget_is_passed_down_and_bounds_the_walk(self):
        calls = []
        lb.run_local_chain(["a", "b"], [], FakeBackend({"a": "timeout", "b": "ok"}, calls),
                           budget=5.0, options={"max_tokens": 4})
        self.assertTrue(all(0 < c[1]["timeout"] <= 5.0 for c in calls))
        self.assertEqual(calls[0][1]["options"], {"max_tokens": 4})
        calls = []
        with mock.patch.object(lb.time, "monotonic", side_effect=[0.0, 0.0, 10.0]):
            with self.assertRaises(lb.LocalUnavailable):
                lb.run_local_chain(["a", "b"], [], FakeBackend({"a": "timeout", "b": "ok"}, calls), budget=5.0)
        self.assertEqual([c[0] for c in calls], ["a"])  # budget spent: no second attempt


class _Ollama(http.server.BaseHTTPRequestHandler):
    """Stub OpenAI-compatible endpoint: model 'missing' 404s, anything else answers."""

    bodies: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).bodies.append(body)
        if body.get("model") == "missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"model not found"}')
            return
        if body.get("model") == "thinker":  # qwen3.5 at a small output limit: reasoning only
            message = {"role": "assistant", "content": "", "reasoning": "THINKING-ONLY-TEXT"}
            data = json.dumps({"model": "thinker", "choices": [{"message": message, "finish_reason": "length"}]})
        else:
            data = json.dumps({"model": body["model"], "choices": [{"message": {"content": "philosophy"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *a):
        pass


class StubEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Ollama)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_real_backend_404_falls_back_and_sends_options(self):
        _Ollama.bodies = []
        s = config_mod.RouterSettings(ollama_base_url=self.url, ollama_routine_model="missing",
                                      ollama_fallback_model="qwen3.6:27b", specialist_classifier="local")
        fn = hook_mod.local_classifier(s)
        picks = fn("task", ["philosophy", "strategy"])
        self.assertEqual(list(picks), ["philosophy"])
        self.assertEqual(picks.meta, {"model": "qwen3.6:27b", "attempt": 2, "outcome": "ok"})
        self.assertEqual([b["model"] for b in _Ollama.bodies], ["missing", "qwen3.6:27b"])
        self.assertEqual(_Ollama.bodies[-1]["reasoning_effort"], "none")
        self.assertEqual(_Ollama.bodies[-1]["temperature"], 0)

    def test_proxy_environment_is_ignored(self):
        _Ollama.bodies = []
        with socket_closed_port() as port, mock.patch.dict(os.environ, {
                "http_proxy": f"http://127.0.0.1:{port}", "HTTP_PROXY": f"http://127.0.0.1:{port}",
                "no_proxy": "", "NO_PROXY": ""}):
            resp = lb.LocalBackend(self.url, "m", timeout=5).chat([{"role": "user", "content": "x"}])
        self.assertEqual(resp.choices[0].message.content, "philosophy")
        self.assertEqual(len(_Ollama.bodies), 1)  # reached the endpoint directly

    def test_closed_port_is_connect_and_refuses(self):
        with socket_closed_port() as port:
            backend = lb.LocalBackend(f"http://127.0.0.1:{port}/v1", "m", timeout=2)
            with self.assertRaises(lb.LocalUnavailable) as ctx:
                backend.chat([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.kind, "connect")


    def _private_router(self, routine, fallback):
        s = config_mod.RouterSettings(ollama_base_url=self.url, ollama_routine_model=routine,
                                      ollama_fallback_model=fallback, privacy_routing=True)
        return router_mod.Router(s)

    def test_private_turn_sends_bounded_no_thinking_request(self):
        _Ollama.bodies = []
        spy = Spy()
        resp = self._private_router("qwen3.5:27b", "qwen3.6:27b").on_llm_execution(
            request=req(f"draft a reply to the client, #privacy ssn {SSN}"), next_call=spy, session_id="pv1",
            base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(resp.choices[0].message.content, "philosophy")
        self.assertFalse(getattr(resp, "_model_router_refusal", False))
        body = _Ollama.bodies[-1]
        self.assertEqual(body["model"], "qwen3.5:27b")
        self.assertEqual(body["reasoning_effort"], "none")  # top-level: what Ollama /v1 honours
        self.assertEqual(body["max_tokens"], lb.PRIVATE_MAX_TOKENS)
        self.assertFalse(body["stream"])
        for absent in ("think", "extra_body", "reasoning", "tools"):
            self.assertNotIn(absent, body)

    def test_thinking_only_answer_falls_back_locally_then_refuses(self):
        _Ollama.bodies = []
        backend = lb.LocalBackend(self.url, "thinker", timeout=5)
        with self.assertRaises(lb.LocalUnavailable) as ctx:
            backend.chat([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.kind, "shape")
        self.assertIn("finish_reason=length", str(ctx.exception))

        resp = self._private_router("thinker", "qwen3.6:27b").on_llm_execution(
            request=req(f"#privacy ssn {SSN}"), next_call=Spy(), session_id="pv2", base_url="")
        self.assertEqual(resp.choices[0].message.content, "philosophy")  # local fallback model

        _Ollama.bodies = []
        spy = Spy()
        resp = self._private_router("thinker", "thinker").on_llm_execution(
            request=req(f"#privacy ssn {SSN}"), next_call=spy, session_id="pv3", base_url="")
        self.assertEqual(spy.calls, 0, "private request reached a cloud provider")
        self.assertTrue(resp._model_router_refusal)
        self.assertEqual(resp.choices[0].finish_reason, "stop")  # terminal, never content_filter
        self.assertNotIn("THINKING-ONLY-TEXT", resp.choices[0].message.content)
        self.assertEqual(len(_Ollama.bodies), 1)  # duplicate chain entry is not retried


class socket_closed_port:
    def __enter__(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        return self.port

    def __exit__(self, *a):
        return False


# --------------------------------------------------------------------------- #
# Private routing: role hierarchy, local fallback, never cloud
# --------------------------------------------------------------------------- #
class PrivateHierarchyTest(unittest.TestCase):
    def _router(self, plan, calls, **over):
        s = dict(ROLES, privacy_routing=True, complexity_routing=True)
        s.update(over)
        return router_mod.Router(config_mod.RouterSettings(**s), backend_factory=FakeBackend(plan, calls))

    def _run(self, router, request, base_url="http://127.0.0.1:8317/v1", sid="s"):
        spy = Spy()
        resp = router.on_llm_execution(request=request, next_call=spy, session_id=sid, base_url=base_url,
                                       model=request.get("model"))
        self.assertEqual(spy.calls, 0, "private request reached a cloud provider")
        return resp

    def test_ordinary_private_task_uses_routine_model(self):
        calls = []
        resp = self._run(self._router({"qwen3.5:27b": "LOCAL"}, calls), req(f"#privacy my ssn is {SSN}, file it"))
        self.assertEqual(resp.choices[0].message.content, "LOCAL")
        self.assertEqual([c[0] for c in calls], ["qwen3.5:27b"])

    def test_difficult_private_task_uses_agent_model(self):
        calls = []
        self._run(self._router({"qwen3.8:27b": "LOCAL"}, calls), req(f"{HARD} #privacy ssn {SSN}"))
        self.assertEqual([c[0] for c in calls], ["qwen3.8:27b"])

    def test_private_coding_uses_agent_model(self):
        calls = []
        self._run(self._router({"qwen3.8:27b": "LOCAL"}, calls), req(f"fix it, #privacy ssn {SSN}", model="opus"),
                  base_url="http://127.0.0.1:3456/v1")
        self.assertEqual([c[0] for c in calls], ["qwen3.8:27b"])

    def test_role_failure_falls_back_to_local_fallback_model(self):
        calls = []
        resp = self._run(self._router({"qwen3.5:27b": "http", "qwen3.6:27b": "FALLBACK"}, calls),
                         req(f"#privacy ssn {SSN}"))
        self.assertEqual(resp.choices[0].message.content, "FALLBACK")
        self.assertEqual([c[0] for c in calls], ["qwen3.5:27b", "qwen3.6:27b"])

    def test_local_chain_exhausted_refuses_never_cloud(self):
        calls = []
        resp = self._run(self._router({}, calls), req(f"#privacy ssn {SSN}"))
        self.assertTrue(resp._model_router_refusal)
        self.assertEqual(len(calls), 2)  # bounded: role model + one fallback

    def test_endpoint_down_refuses_without_fallback(self):
        calls = []
        resp = self._run(self._router({"qwen3.5:27b": "connect", "qwen3.6:27b": "OK"}, calls), req(f"#privacy ssn {SSN}"))
        self.assertTrue(resp._model_router_refusal)
        self.assertEqual([c[0] for c in calls], ["qwen3.5:27b"])

    def test_non_local_endpoint_is_refused_unsent(self):
        calls = []
        r = self._router({"qwen3.5:27b": "LOCAL"}, calls, ollama_base_url="https://api.example.com/v1")
        resp = self._run(r, req(f"#privacy ssn {SSN}"))
        self.assertTrue(resp._model_router_refusal)
        self.assertIn("local-endpoint-not-local", resp.choices[0].message.content)
        self.assertEqual(calls, [])

    def test_public_request_is_untouched_by_local_roles(self):
        calls = []
        r = self._router({"qwen3.5:27b": "LOCAL"}, calls)
        spy = Spy()
        resp = r.on_llm_execution(request=req("what is 2+2?"), next_call=spy, session_id="p",
                                  base_url="http://127.0.0.1:8317/v1")
        self.assertEqual((spy.calls, resp.model, calls), (1, "CLOUD", []))

    def test_private_logs_carry_labels_not_content(self):
        calls = []
        r = self._router({"qwen3.5:27b": "LOCAL"}, calls)
        with self.assertLogs("hermes.plugin.model_router", level="INFO") as logs:
            self._run(r, req(f"Zorblax account #privacy ssn {SSN}"))
        text = "\n".join(logs.output)
        self.assertIn("role=routine model=qwen3.5:27b", text)
        self.assertNotIn("Zorblax", text)
        self.assertNotIn(SSN, text)


# --------------------------------------------------------------------------- #
# Private request policy: no thinking, bounded output, prefill-sized budget
# --------------------------------------------------------------------------- #
def history(tokens):
    """Messages whose conservative estimate is about ``tokens``."""
    turn = "lorem ipsum dolor sit amet " * 40
    msgs, n = [{"role": "system", "content": "You are a helpful assistant."}], 0
    while lb.estimate_prompt_tokens(msgs) < tokens:
        msgs.append({"role": "user" if n % 2 == 0 else "assistant", "content": turn})
        n += 1
    return msgs


class PrivatePolicyTest(unittest.TestCase):
    def test_short_turn_gets_full_cap_and_modest_budget(self):
        options, budget = lb.private_chat_policy([{"role": "user", "content": "hi"}], base_timeout=60)
        self.assertEqual(options, {"reasoning_effort": "none", "max_tokens": lb.PRIVATE_MAX_TOKENS})
        self.assertGreater(budget, 60 + lb.PRIVATE_MAX_TOKENS / 12)  # survives worst-case decode
        self.assertLess(budget, 300)

    def test_long_history_fits_the_served_context(self):
        for tokens in (20_000, 50_000, 64_000, 63_500):
            msgs = history(tokens)
            prompt = lb.estimate_prompt_tokens(msgs)
            options, budget = lb.private_chat_policy(msgs, context_tokens=65536, base_timeout=60)
            mt = options["max_tokens"]
            self.assertTrue(lb.PRIVATE_MIN_TOKENS <= mt <= lb.PRIVATE_MAX_TOKENS, (tokens, mt))
            if prompt + lb.PRIVATE_MIN_TOKENS < 65536 - 256:
                self.assertLessEqual(prompt + mt, 65536, tokens)
            self.assertLessEqual(budget, lb.PRIVATE_TIMEOUT_CAP)
            self.assertGreater(budget, 60)
        # a 50k-token history needs minutes of prefill at ~200 tok/s, not the 60 s default
        _, budget = lb.private_chat_policy(history(50_000), base_timeout=60)
        self.assertGreaterEqual(budget, 60 + 50_000 / 200)

    def test_oversized_history_uses_floor_and_cap(self):
        options, budget = lb.private_chat_policy(history(90_000), context_tokens=65536, base_timeout=60)
        self.assertEqual(options["max_tokens"], lb.PRIVATE_MIN_TOKENS)
        # Ollama truncates the prompt to the served context, so prefill is sized to it
        self.assertAlmostEqual(budget, 60 + 65536 / 150 + lb.PRIVATE_MIN_TOKENS / 10)
        self.assertLessEqual(budget, lb.PRIVATE_TIMEOUT_CAP)
        _, budget = lb.private_chat_policy(history(90_000), base_timeout=300)
        self.assertEqual(budget, lb.PRIVATE_TIMEOUT_CAP)

    def test_context_setting_is_read(self):
        s = config_mod.load_settings(lambda k, d: {"ollama_context_tokens": "32768"}.get(k))
        self.assertEqual(s.ollama_context_tokens, 32768)
        self.assertEqual(config_mod.RouterSettings().ollama_context_tokens, 65536)

    def test_router_passes_policy_and_shares_budget_across_chain(self):
        calls = []
        s = config_mod.RouterSettings(**dict(ROLES, privacy_routing=True))
        r = router_mod.Router(s, backend_factory=FakeBackend({"qwen3.5:27b": "timeout",
                                                              "qwen3.6:27b": "LOCAL"}, calls))
        spy = Spy()
        request = {"model": "gpt-5.6-luna", "messages": history(30_000) + [
            {"role": "user", "content": f"#privacy ssn {SSN}"}]}
        resp = r.on_llm_execution(request=request, next_call=spy, session_id="pp", base_url="")
        self.assertEqual((spy.calls, resp.choices[0].message.content), (0, "LOCAL"))
        expected, budget = lb.private_chat_policy(request["messages"], base_timeout=s.ollama_timeout)
        self.assertEqual([c[0] for c in calls], ["qwen3.5:27b", "qwen3.6:27b"])
        for _model, kw in calls:
            self.assertEqual(kw["options"], expected)
            self.assertTrue(0 < kw["timeout"] <= budget)
        self.assertGreater(calls[0][1]["timeout"], s.ollama_timeout)  # long history: not the 60 s default


# --------------------------------------------------------------------------- #
# Hybrid specialist classifier: gate, privacy, high-risk, caps, failure
# --------------------------------------------------------------------------- #
def settings(**kw):
    base = dict(enabled=True, classifier="local")
    base.update(kw)
    return sp.SpecialistSettings(**base)


class ClassifierGateTest(unittest.TestCase):
    def test_strong_keyword_match_never_calls_classifier(self):
        called = []
        t = sp.route_text("Write a marketing campaign for the launch.", settings(), readiness=ready,
                          classifier=lambda v, p: called.append(1) or ["philosophy"])
        self.assertEqual((called, t["classifier"]), ([], "not-needed"))

    def test_no_match_and_weak_only_are_the_gates(self):
        seen = []

        def fake(visible, pool):
            seen.append(list(pool))
            return ["philosophy"]

        none = sp.route_text(NO_MATCH, settings(), readiness=ready, classifier=fake)
        self.assertEqual((none["classifier_gate"], [r["profile"] for r in none["routes"]]),
                         ("no-match", ["philosophy"]))
        weak = sp.route_text(AMBIGUOUS, settings(), readiness=ready, classifier=fake)
        self.assertEqual(weak["classifier_gate"], "ambiguous")
        self.assertEqual([r["profile"] for r in weak["routes"]], ["strategy", "philosophy"])
        self.assertNotIn("strategy", seen[1])  # the pool never repeats a deterministic match

    def test_at_most_two_additions_from_the_whole_catalog(self):
        def greedy(visible, pool):
            return ["security-reviewer", "red-team", "health-fitness", "herbalism",
                    "philosophy", "marketing", "business", "survival"]

        t = sp.route_text(NO_MATCH, settings(max_profiles=6), readiness=ready, classifier=greedy)
        added = [r["profile"] for r in t["routes"] if r["reasons"] == ["classifier"]]
        self.assertEqual(added, ["security-reviewer", "red-team"])   # no high-risk filter
        reasons = {d["profile"]: d["reason"] for d in t["dropped"]}
        for name in ("health-fitness", "herbalism", "philosophy", "marketing", "business", "survival"):
            self.assertEqual(reasons[name], "classifier:cap")
        self.assertEqual(sp.MAX_CLASSIFIER_ADDITIONS, 2)
        self.assertFalse(hasattr(sp, "HIGH_RISK"))

    def test_private_task_never_reaches_classifier(self):
        called = []
        for text in (f"{NO_MATCH} #privacy", f"#privacy\n{NO_MATCH}"):
            t = sp.route_text(text, settings(), readiness=ready,
                              classifier=lambda v, p: called.append(1) or ["philosophy"])
            self.assertEqual(t["classifier"], "skipped:private")
        self.assertEqual(called, [])

    def test_timeout_or_failure_adds_nothing(self):
        def timeout(visible, pool):
            return hook_mod.Picks([], {"model": "-", "attempt": 0, "outcome": "unavailable:timeout"})

        t = sp.route_text(NO_MATCH, settings(), readiness=ready, classifier=timeout)
        self.assertEqual((t["routes"], t["classifier_detail"]["outcome"]), ([], "unavailable:timeout"))
        w = sp.route_text(AMBIGUOUS, settings(), readiness=ready,
                          classifier=lambda v, p: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(([r["profile"] for r in w["routes"]], w["classifier"]),
                         (["strategy"], "error:TimeoutError"))


class LocalClassifierTest(unittest.TestCase):
    def _fn(self, plan, calls, **over):
        s = config_mod.RouterSettings(**dict(ROLES, specialist_classifier="local", **over))
        return hook_mod.local_classifier(s, factory=FakeBackend(plan, calls))

    def test_routine_model_first_with_bounded_options(self):
        calls = []
        picks = self._fn({"qwen3.5:27b": "philosophy, strategy, marketing"}, calls)(
            "task", ["philosophy", "strategy", "marketing"])
        self.assertEqual(list(picks), ["philosophy", "strategy"])  # capped at two
        self.assertEqual(picks.meta["model"], "qwen3.5:27b")
        model, kw = calls[0]
        self.assertEqual(kw["options"], hook_mod.CLASSIFIER_OPTIONS)
        self.assertEqual(kw["options"]["reasoning_effort"], "none")
        self.assertLessEqual(kw["timeout"], 10.0)

    def test_fallback_model_then_nothing(self):
        calls = []
        picks = self._fn({"qwen3.5:27b": "http", "qwen3.6:27b": "<think>x</think>philosophy"}, calls)(
            "task", ["philosophy"])
        self.assertEqual((list(picks), picks.meta["model"]), (["philosophy"], "qwen3.6:27b"))
        calls = []
        picks = self._fn({"qwen3.5:27b": "connect", "qwen3.6:27b": "philosophy"}, calls)("task", ["philosophy"])
        self.assertEqual((list(picks), picks.meta["outcome"], len(calls)), ([], "unavailable:connect", 1))

    def test_refuses_non_local_or_unconfigured(self):
        self.assertIsNone(hook_mod.local_classifier(config_mod.RouterSettings(
            **dict(ROLES, ollama_base_url="https://api.example.com/v1"))))
        self.assertIsNone(hook_mod.local_classifier(config_mod.RouterSettings(ollama_base_url=LOCAL)))

    def test_hook_logs_no_prompt_content(self):
        calls = []
        s = config_mod.RouterSettings(**dict(ROLES, specialist_routing=True, specialist_selection_mode="hybrid", specialist_classifier="local"))
        h = hook_mod.SpecialistHook(s, backend_factory=FakeBackend({"qwen3.5:27b": "philosophy"}, calls))
        self.assertTrue(h.classifier_ready)
        secret_word = "Quuxbarian"
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)), \
                self.assertLogs("hermes.plugin.model_router", level="INFO") as logs:
            out = h.on_pre_llm_call(session_id="s", user_message=f"{NO_MATCH} {secret_word}")
        text = "\n".join(logs.output)
        self.assertIn("gate=no-match classifier_model=qwen3.5:27b classifier_outcome=ok", text)
        self.assertNotIn(secret_word, text)
        self.assertNotIn("structured way", text)
        self.assertIn("philosophy", out["context"])

    def test_hook_private_turn_never_calls_local_classifier(self):
        calls = []
        s = config_mod.RouterSettings(**dict(ROLES, specialist_routing=True, specialist_selection_mode="hybrid", specialist_classifier="local"))
        h = hook_mod.SpecialistHook(s, backend_factory=FakeBackend({"qwen3.5:27b": "philosophy"}, calls))
        with mock.patch.object(sp, "knowledge_readiness", side_effect=lambda n, r: ready(n)):
            self.assertIsNone(h.on_pre_llm_call(session_id="s", user_message=f"#privacy {NO_MATCH}"))
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- #
# Installer render: presence-aware specialist knobs, roles, aux provider
# --------------------------------------------------------------------------- #
FIXTURE_ENV = """CLIPROXY_PORT=8317
CLIPROXY_API_KEY=cptest
CLAUDE_MAX_PROXY_PORT=3456
CLAUDE_MAX_PROXY_API_KEY=secrettoken123
HERMES_MODEL=gpt-6.1-terra
HERMES_CODING_MODEL=opus
REPOS_DIR={tmp}/repos
HERMES_HOME={home}
DISCORD_BOT_TOKEN={token}
OLLAMA_BASE_URL=http://100.65.5.47:11434/v1
"""
LIVE_CONFIG = """plugins:
  enabled:
  - model-router
  entries:
    model-router:
      settings:
        specialist_routing: true
        specialist_max_profiles: 2
        specialist_require_ready: true
        specialist_classifier: local
        specialist_classifier_timeout: 12
        specialist_profiles:
        - strategy
        - philosophy
"""
SETTINGS = "plugins.entries.model-router.settings"
config_get = None


def _config_get():
    global config_get
    if config_get is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("config_get", REPO / "scripts" / "config-get.py")
        config_get = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config_get)
    return config_get


def _local_route():
    import importlib.util
    spec = importlib.util.spec_from_file_location("local_route", REPO / "scripts" / "local-route.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CLIPROXY = "http://127.0.0.1:8317/v1"


def _nat(title, main=None):
    """Minimal hermes-native-check output: main route + one auxiliary task."""
    return {"main": main if main is not None else {"provider": "custom", "model": "gpt-5.6-luna",
                                                   "base_url": CLIPROXY},
            "fallback_providers": [], "delegation_fallback_providers": [],
            "auxiliary": {"title_generation": dict(title, fallback_chain=[])},
            "plugins_enabled": ["model-router"], "model_router": {}}


class ReadbackInvariantTest(unittest.TestCase):
    """Title generation intentionally follows the main CLIProxyAPI route."""

    def setUp(self):
        self.lr = _local_route()

    def bad(self, nat):
        return self.lr._invariants(nat, lb, CLIPROXY)

    def test_auto_title_following_main_cliproxy_route_is_accepted(self):
        # Hermes resolves an empty YAML model to the string "None".
        for model in (None, "", "None"):
            self.assertEqual(self.bad(_nat({"provider": "auto", "model": model, "base_url": ""})), [], model)
        main = {"provider": "custom", "model": "gpt-5.6-luna", "base_url": CLIPROXY + "/"}
        self.assertEqual(self.bad(_nat({"provider": "auto", "model": "None", "base_url": ""}, main)), [])

    def test_auto_title_fails_when_main_is_not_the_configured_cliproxy(self):
        for main in ({"provider": "custom", "model": "opus", "base_url": "http://127.0.0.1:3456/v1"},
                     {"provider": "openrouter", "model": "x", "base_url": ""}, {}):
            bad = self.bad(_nat({"provider": "auto", "model": "None", "base_url": ""}, main))
            self.assertEqual(len(bad), 1, (main, bad))
            self.assertIn("not the configured CLIProxyAPI", bad[0])

    def test_auto_with_explicit_model_still_fails(self):
        bad = self.bad(_nat({"provider": "auto", "model": "qwen3.5:27b", "base_url": ""}))
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("resolves to the MAIN (cloud) provider", bad[0])


class _InstallerFixture(unittest.TestCase):
    """Temp HERMES_HOME, fixture .env and a stub `hermes` that logs its calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "hermes"
        self.home.mkdir()
        self.bin = self.tmp / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.hermes_log = self.tmp / "hermes-calls.log"
        stub = self.bin / "hermes"
        stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{self.hermes_log}"\n'
                        'case "$1" in --version) echo "hermes-stub 0.0.0";; esac\nexit 0\n')
        stub.chmod(0o755)

    def install(self, extra="", *args, live=None, token="", env=None):
        envf = self.tmp / "fixture.env"
        envf.write_text(FIXTURE_ENV.format(tmp=self.tmp, home=self.home, token=token) + extra)
        if live is not None:
            (self.home / "config.yaml").write_text(live)
        run_env = dict(HOME=str(self.tmp), PATH=f"{self.bin}:/usr/bin:/bin", ENV_FILE=str(envf),
                       HERMES_SKIP_INSTALL="1", HERMES_SKIP_GATEWAY="1")
        run_env.update(env or {})
        return subprocess.run(["bash", "scripts/hermes-install.sh", *args], cwd=REPO, env=run_env,
                              capture_output=True, text=True, timeout=120)

    def get(self, path, key):
        found, value = _config_get().lookup(Path(path).read_text(), key)
        self.assertTrue(found, f"{key} missing from {path}")
        return value


class InstallerRenderTest(_InstallerFixture):
    def test_force_keeps_live_specialist_values_when_env_is_silent(self):
        r = self.install("", "--force", live=LIVE_CONFIG)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        cfg = self.home / "config.yaml"
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_routing"), "true")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_max_profiles"), "2")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_require_ready"), "true")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_classifier"), "local")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_classifier_timeout"), "12")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_profiles"), "strategy,philosophy")
        self.assertIn("specialist_routing=true (live config)", r.stdout)
        self.assertTrue(list(self.home.glob("config.yaml.bak.*")))

    def test_explicit_env_false_and_true_win_over_live(self):
        r = self.install("MODEL_ROUTER_SPECIALISTS=false\nMODEL_ROUTER_SPECIALIST_MAX=0\n"
                         "MODEL_ROUTER_SPECIALIST_CLASSIFIER=none\nMODEL_ROUTER_SPECIALIST_PROFILES=all\n",
                         "--force", live=LIVE_CONFIG)
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.home / "config.yaml"
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_routing"), "false")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_max_profiles"), "0")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_classifier"), "none")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_profiles"), "")
        r = self.install("MODEL_ROUTER_SPECIALIST_CLASSIFIER=cliproxy\n", "--force", live=LIVE_CONFIG)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.specialist_classifier"), "cliproxy")
        r = self.install("", "--force", live=LIVE_CONFIG.replace("specialist_classifier: local",
                                                                 "specialist_classifier: cliproxy"))
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.specialist_classifier"), "cliproxy")
        self.assertIn("classifier=cliproxy (live config)", r.stdout)
        live_off = LIVE_CONFIG.replace("specialist_routing: true", "specialist_routing: false")
        r = self.install("MODEL_ROUTER_SPECIALISTS=yes\n", "--force", live=live_off)
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.specialist_routing"), "true")

    def test_fresh_install_defaults_and_invalid_values(self):
        r = self.install("MODEL_ROUTER_SPECIALISTS=maybe\nMODEL_ROUTER_SPECIALIST_MAX=16\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.home / "config.yaml"
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_routing"), "false")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_max_profiles"), "4")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.specialist_classifier"), "none")
        self.assertIn("is not a boolean", r.stderr)
        self.assertIn("is not 0-15", r.stderr)

    def test_render_pins_schema_version_kanban_and_title_effort(self):
        r = self.install("", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.home / "config.yaml"
        self.assertEqual(self.get(cfg, "_config_version"), "44")
        self.assertEqual(self.get(cfg, "kanban.auto_decompose"), "true")
        self.assertEqual(self.get(cfg, "auxiliary.title_generation.reasoning_effort"), "low")
        self.assertEqual(self.get(cfg, "auxiliary.title_generation.provider"), "auto")
        self.assertEqual(self.get(cfg, "agent.reasoning_effort"), "high")

    def test_local_roles_fallback_chains_and_aux_provider(self):
        r = self.install("AUX_BASE_URL=http://100.65.5.47:11434/v1\nAUX_PROVIDER=auto\n"
                         "AUX_COMPRESSION_ROUTE=aux\n", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.home / "config.yaml").read_text()
        self.assertNotRegex(text, r"__[A-Z0-9_]+__")
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.ollama_routine_model"), "qwen3.5:27b")
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.ollama_fallback_model"), "qwen3.6:27b")
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.ollama_agent_model"), "qwen3.8:27b")
        self.assertEqual(self.get(self.home / "config.yaml", "auxiliary.compression.provider"), "custom")
        self.assertEqual(self.get(self.home / "config.yaml", "auxiliary.compression.model"), "qwen3.5:27b")
        self.assertIn('fallback_chain: [{provider: custom, model: "qwen3.6:27b", '
                      'base_url: "http://100.65.5.47:11434/v1"}]', text)
        top = text.split("# Subagents spawned")[0]
        self.assertLess(top.index("model: qwen3.8:27b"), top.index("model: qwen3.6:27b"))
        # Local compression: thinking off (extra_body only), prefill-sized budgets.
        comp = text.split("  compression:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn('reasoning_effort: ""', comp)
        self.assertIn("extra_body: {reasoning_effort: none}", comp)
        self.assertIn("timeout: 600", comp)
        self.assertEqual(self.get(self.home / "config.yaml", "compression.context_timeout_seconds"), "420")
        self.assertEqual(self.get(self.home / "config.yaml", "compression.context_total_ceiling_seconds"), "900")
        # Without a local aux endpoint provider stays auto (inherit main) with no chain.
        self.install("AUX_COMPRESSION_ROUTE=aux\n", "--force")
        text = (self.home / "config.yaml").read_text()
        self.assertEqual(self.get(self.home / "config.yaml", "auxiliary.compression.provider"), "auto")
        self.assertIn("fallback_chain: []", text)
        self.assertEqual(self.get(self.home / "config.yaml", "auxiliary.compression.reasoning_effort"), "low")
        self.assertEqual(self.get(self.home / "config.yaml", "compression.context_timeout_seconds"), "120")
        # Privacy stays default-on in the template; an explicit opt-out is preserved.
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.privacy_routing"), "true")
        self.install("MODEL_ROUTER_PRIVACY=false\n", "--force")
        self.assertEqual(self.get(self.home / "config.yaml", f"{SETTINGS}.privacy_routing"), "false")

    def test_compression_defaults_to_cliproxy_not_ollama(self):
        """Even with title generation pinned to Ollama, compression renders
        CLIProxyAPI gpt-6.1-terra with a gpt-5.6-luna fallback on the same proxy."""
        r = self.install("AUX_BASE_URL=http://100.65.5.47:11434/v1\n", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.home / "config.yaml"
        text = cfg.read_text()
        self.assertNotRegex(text, r"__[A-Z0-9_]+__")
        self.assertEqual(self.get(cfg, "auxiliary.title_generation.base_url"), "http://100.65.5.47:11434/v1")
        get = lambda k: self.get(cfg, f"auxiliary.compression.{k}")
        self.assertEqual((get("provider"), get("model"), get("base_url"), get("api_key")),
                         ("custom", "gpt-6.1-terra", "http://127.0.0.1:8317/v1", "${CLIPROXY_API_KEY}"))
        self.assertEqual((get("reasoning_effort"), get("timeout")), ("low", "120"))
        comp = text.split("  compression:\n", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("11434", comp)
        self.assertNotIn("qwen", comp)
        self.assertIn("extra_body: {}", comp)
        self.assertIn('fallback_chain: [{provider: custom, model: "gpt-5.6-luna", '
                      'base_url: "http://127.0.0.1:8317/v1", key_env: CLIPROXY_API_KEY}]', comp)
        self.assertNotIn("cptest", text)  # the key stays in .env
        self.assertEqual(self.get(cfg, "compression.context_timeout_seconds"), "120")
        self.assertEqual(self.get(cfg, "compression.context_total_ceiling_seconds"), "600")
        # Same render with no Ollama aux at all.
        self.install("", "--force")
        self.assertEqual(self.get(cfg, "auxiliary.compression.model"), "gpt-6.1-terra")
        self.assertEqual(self.get(cfg, "auxiliary.title_generation.provider"), "auto")

    def test_compression_model_overrides_and_invalid_route(self):
        cfg = self.home / "config.yaml"
        r = self.install("AUX_COMPRESSION_MODEL=gpt-5.6-luna\n", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.get(cfg, "auxiliary.compression.model"), "gpt-5.6-luna")
        comp = cfg.read_text().split("  compression:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("fallback_chain: []", comp)  # no duplicate of the primary
        self.install("AUX_COMPRESSION_FALLBACK_MODEL=none\n", "--force")
        comp = cfg.read_text().split("  compression:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("fallback_chain: []", comp)
        r = self.install("AUX_COMPRESSION_ROUTE=ollama\n", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("AUX_COMPRESSION_ROUTE=ollama", r.stderr)
        self.assertEqual(self.get(cfg, "auxiliary.compression.model"), "gpt-6.1-terra")

    def test_legacy_chat_model_keeps_single_model_installs_working(self):
        r = self.install("OLLAMA_CHAT_MODEL=llama3.1:8b\n", "--force")
        cfg = self.home / "config.yaml"
        self.assertEqual(self.get(cfg, f"{SETTINGS}.ollama_routine_model"), "llama3.1:8b")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.ollama_agent_model"), "llama3.1:8b")
        self.assertIsNone(self.get(cfg, f"{SETTINGS}.ollama_fallback_model"))  # no unpulled default
        self.assertIn("set OLLAMA_ROUTINE_MODEL", r.stderr)
        self.install("OLLAMA_CHAT_MODEL=llama3.1:8b\nOLLAMA_ROUTINE_MODEL=qwen3.5:27b\n"
                     "OLLAMA_FALLBACK_MODEL=qwen3.6:27b\nOLLAMA_AGENT_MODEL=qwen3.8:27b\n", "--force")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.ollama_routine_model"), "qwen3.5:27b")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.ollama_fallback_model"), "qwen3.6:27b")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.ollama_agent_model"), "qwen3.8:27b")

    def test_private_terms_survive_force_and_env_wins(self):
        live = LIVE_CONFIG + '        private_terms:\n        - Project Neptune\n        - say "hi"\n' \
                             '        private_paths: ["/vault/"]\n'
        r = self.install("", "--force", live=live)
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.home / "config.yaml"
        self.assertEqual(self.get(cfg, f"{SETTINGS}.private_terms"), 'Project Neptune,say "hi"')
        self.assertEqual(self.get(cfg, f"{SETTINGS}.private_paths"), "/vault/")
        self.assertNotIn("Neptune", r.stdout + r.stderr)  # never logged
        before = cfg.read_text()
        self.install("", "--force")  # re-render from the rendered file: stable, no re-escaping
        self.assertEqual(self.get(cfg, f"{SETTINGS}.private_terms"), 'Project Neptune,say "hi"')
        self.assertEqual(before.count("Neptune"), cfg.read_text().count("Neptune"))
        self.install("MODEL_ROUTER_PRIVATE_TERMS=acme-internal, Project: X\n", "--force")
        self.assertEqual(self.get(cfg, f"{SETTINGS}.private_terms"), "acme-internal,Project: X")

    def test_render_only_writes_nothing_under_hermes_home(self):
        out = self.tmp / "render.yaml"
        r = self.install("", "--force", "--render-only", str(out), live=LIVE_CONFIG)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.get(out, f"{SETTINGS}.specialist_routing"), "true")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.yaml"])
        self.assertEqual((self.home / "config.yaml").read_text(), LIVE_CONFIG)
        self.assertFalse(self.hermes_log.exists())

    def test_no_gateway_restart_from_inside_hermes(self):
        r = self.install("", "--force", token="t0ken", env={"HERMES_SKIP_GATEWAY": "0",
                                                            "HERMES_GATEWAY_SESSION": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not restarting the gateway", r.stderr)
        self.assertNotIn("gateway", self.hermes_log.read_text())


# --------------------------------------------------------------------------- #
# Hermes' own resolver on the rendered config (skipped without a Hermes venv)
# --------------------------------------------------------------------------- #
AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
VENV_PY = AGENT_DIR / "venv" / "bin" / "python"


# Runs in the Hermes venv: the rendered compression route against a stub
# endpoint that records request bodies (primary, then the stall-retry pin).
_COMPRESSION_WIRE = r"""
import http.server, json, os, sys, tempfile, threading, yaml
seen = []
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        out = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                          "choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant", "content": "ok"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/v1"
cfg = yaml.safe_load(open(sys.argv[1]))
aux = cfg["auxiliary"]["compression"]
for route in [aux] + aux["fallback_chain"]:
    route["base_url"] = url
home = tempfile.mkdtemp()
yaml.safe_dump({k: cfg[k] for k in ("model", "auxiliary", "compression")}, open(f"{home}/config.yaml", "w"))
os.environ["HERMES_HOME"] = home
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
from agent.auxiliary_client import _effective_aux_timeout, call_llm
from agent.conversation_compression import resolve_context_compression_timeouts
msgs = [{"role": "user", "content": "summarize"}]
call_llm(task="compression", messages=msgs)
call_llm(task="compression", provider="custom", model=aux["fallback_chain"][0]["model"], base_url=url, messages=msgs)
strip = lambda b: {k: v for k, v in b.items() if k != "messages"}
print(json.dumps({"primary": strip(seen[0]), "stall_retry": strip(seen[1]),
                  "aux_timeout": _effective_aux_timeout("compression", None),
                  "budgets": list(resolve_context_compression_timeouts(cfg["compression"]))}))
"""


# Runs in the Hermes venv: the rendered CLIProxyAPI compression route through
# Hermes' own call_llm against a stub "CLIProxyAPI" on loopback. argv: config,
# "guard"|"noguard" (install the model-router aux guard first), JSON list of
# models the stub answers with 429 (a per-model usage limit).
_CLIPROXY_COMPRESSION_WIRE = r"""
import http.server, importlib.util, json, os, sys, tempfile, threading, yaml
seen = []
missing = set(json.loads(sys.argv[3]))
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append({"path": self.path, "model": body.get("model"), "auth": self.headers.get("Authorization"),
                     "marker": "#privacy" in json.dumps(body.get("messages"))})
        if body.get("model") in missing:
            code, out = 429, {"error": {"message": "usage limit reached for this model",
                                        "type": "rate_limit_error", "code": "rate_limit_exceeded"}}
        else:
            code, out = 200, {"id": "x", "object": "chat.completion", "created": 0, "model": body.get("model"),
                              "choices": [{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": "ok"}}]}
        out = json.dumps(out).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/v1"
cfg = yaml.safe_load(open(sys.argv[1]))
aux = cfg["auxiliary"]["compression"]
for route in [cfg["model"], aux] + aux["fallback_chain"]:
    route["base_url"] = url
home = tempfile.mkdtemp()
yaml.safe_dump({k: cfg[k] for k in ("model", "auxiliary", "compression")}, open(f"{home}/config.yaml", "w"))
os.environ.update(HERMES_HOME=home, CLIPROXY_API_KEY="cptest", CLIPROXY_BASE_URL=url)
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
if sys.argv[2] == "guard":
    pdir = os.environ["PLUGIN_DIR"]
    spec = importlib.util.spec_from_file_location("model_router_wire", f"{pdir}/__init__.py",
                                                  submodule_search_locations=[pdir])
    mod = importlib.util.module_from_spec(spec); sys.modules["model_router_wire"] = mod
    spec.loader.exec_module(mod)
    from model_router_wire import aux_guard, router_config
    assert aux_guard.install(aux_guard.proxy_urls(router_config.load_settings(lambda k, d=None: d)))
from agent.auxiliary_client import call_llm
from agent.aux_egress_guard import AuxCloudEgressRefused
res = {}
for name, text in (("public", "summarize: the build passed"),
                   ("privacy", "[USER]: #privacy\nmy salary is 123"),
                   ("private", "[USER]: #private notes")):
    before = len(seen)
    try:
        call_llm(task="compression", messages=[{"role": "user", "content": text}])
        res[name] = "sent"
    except AuxCloudEgressRefused:
        res[name] = "refused"
    except Exception as exc:
        res[name] = type(exc).__name__
    res[name + "_requests"] = seen[before:]
print(json.dumps(res))
"""


@unittest.skipUnless(VENV_PY.exists(), "Hermes venv not installed")
class HermesNativeTest(_InstallerFixture):
    def native(self, config):
        r = subprocess.run([str(VENV_PY), str(REPO / "scripts" / "hermes-native-check.py"), "--config", str(config)],
                           capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, HERMES_AGENT_DIR=str(AGENT_DIR)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_hermes_walks_three_tier_chain_and_custom_aux(self):
        out = self.tmp / "render.yaml"
        self.install("AUX_BASE_URL=http://100.65.5.47:11434/v1\n", "--render-only", str(out))
        nat = self.native(out)
        self.assertEqual([e["model"] for e in nat["fallback_providers"]], ["opus", "qwen3.8:27b", "qwen3.6:27b"])
        self.assertEqual([e["model"] for e in nat["delegation_fallback_providers"]], ["qwen3.8:27b", "qwen3.6:27b"])
        aux = nat["auxiliary"]["title_generation"]
        self.assertEqual((aux["provider"], aux["model"], aux["base_url"]),
                         ("custom", "qwen3.5:27b", "http://100.65.5.47:11434/v1"))
        self.assertEqual([e["model"] for e in aux["fallback_chain"]], ["qwen3.6:27b"])
        aux = nat["auxiliary"]["compression"]
        self.assertEqual((aux["provider"], aux["model"], aux["base_url"]),
                         ("custom", "gpt-6.1-terra", "http://127.0.0.1:8317/v1"))
        self.assertEqual([(e["model"], e["base_url"]) for e in aux["fallback_chain"]],
                         [("gpt-5.6-luna", "http://127.0.0.1:8317/v1")])

    def test_local_compression_wire_disables_thinking(self):
        """Ollama's /v1 honors only a top-level reasoning_effort; a `reasoning`
        object (what Hermes emits for the reasoning_effort key) overrides it."""
        out = self.tmp / "render.yaml"
        self.install("AUX_BASE_URL=http://100.65.5.47:11434/v1\nAUX_COMPRESSION_ROUTE=aux\n",
                     "--render-only", str(out))
        r = subprocess.run([str(VENV_PY), "-c", _COMPRESSION_WIRE, str(out)], capture_output=True, text=True,
                           timeout=120, env=dict(os.environ, HERMES_AGENT_DIR=str(AGENT_DIR)))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        res = json.loads(r.stdout.strip().splitlines()[-1])
        for route in ("primary", "stall_retry"):
            self.assertEqual(res[route].get("reasoning_effort"), "none", res)
            self.assertNotIn("reasoning", res[route], res)
        self.assertEqual(res["aux_timeout"], 600.0)
        self.assertEqual(res["budgets"], [420.0, 900.0])

    def cliproxy_wire(self, mode, missing=()):
        out = self.tmp / "render.yaml"
        self.install("AUX_BASE_URL=http://100.65.5.47:11434/v1\n", "--render-only", str(out))
        r = subprocess.run([str(VENV_PY), "-c", _CLIPROXY_COMPRESSION_WIRE, str(out), mode, json.dumps(missing)],
                           capture_output=True, text=True, timeout=180,
                           env=dict(os.environ, HERMES_AGENT_DIR=str(AGENT_DIR),
                                    PLUGIN_DIR=str(REPO / "hermes" / "plugins" / "model-router")))
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_cliproxy_compression_wire_and_private_refusal(self):
        """Compression reaches CLIProxyAPI as gpt-6.1-terra with the proxy key;
        a #privacy/#private payload is refused before any byte is sent."""
        res = self.cliproxy_wire("guard")
        self.assertEqual(res["public"], "sent", res)
        self.assertEqual([(q["model"], q["auth"]) for q in res["public_requests"]],
                         [("gpt-6.1-terra", "Bearer cptest")])
        for name in ("privacy", "private"):
            self.assertEqual(res[name], "refused", res)
            self.assertEqual(res[name + "_requests"], [], res)

    def test_cliproxy_compression_falls_back_to_luna(self):
        res = self.cliproxy_wire("guard", missing=["gpt-6.1-terra"])
        self.assertEqual(res["public"], "sent", res)
        chat = [q for q in res["public_requests"] if q["path"].endswith("/chat/completions")]
        self.assertEqual([(q["model"], q["auth"]) for q in chat],
                         [("gpt-6.1-terra", "Bearer cptest"), ("gpt-5.6-luna", "Bearer cptest")])
        self.assertEqual((res["privacy"], res["privacy_requests"]), ("refused", []), res)

    def test_core_guard_alone_leaks_loopback_cloud_proxy(self):
        """Why the plugin extension is load-bearing: Hermes core treats the
        loopback CLIProxyAPI as local and skips the privacy scan."""
        res = self.cliproxy_wire("noguard")
        self.assertEqual(res["privacy"], "sent", res)
        self.assertTrue(res["privacy_requests"][0]["marker"], res)

    def test_auto_provider_with_local_base_url_resolves_to_main(self):
        """Why the installer never renders `auto` next to a local base_url."""
        out = self.tmp / "auto.yaml"
        out.write_text("auxiliary:\n  compression:\n    provider: auto\n    model: qwen3.8:27b\n"
                       "    base_url: http://100.65.5.47:11434/v1\n")
        aux = self.native(out)["auxiliary"]["compression"]
        self.assertEqual((aux["provider"], aux["base_url"]), ("auto", ""))

    def test_readback_accepts_title_route_on_main_cliproxy(self):
        cfg = self.home / "config.yaml"
        self.install("", "--render-only", str(cfg))
        nat = self.native(cfg)
        self.assertEqual(nat["main"], {"provider": "custom", "model": "gpt-6.1-terra",
                                       "base_url": "http://127.0.0.1:8317/v1"})
        self.assertEqual((nat["auxiliary"]["title_generation"]["provider"],
                          nat["auxiliary"]["title_generation"]["base_url"]), ("auto", ""))
        env = dict(os.environ, HERMES_HOME=str(self.home), HERMES_AGENT_DIR=str(AGENT_DIR),
                   ENV_FILE=str(self.tmp / "fixture.env"))
        ok = subprocess.run([sys.executable, "scripts/local-route.py", "readback"], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=300)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("auxiliary.title_generation follows main gpt-6.1-terra @ 127.0.0.1:8317", ok.stdout)

    def test_readback_matches_render_and_flags_drift(self):
        cfg = self.home / "config.yaml"
        self.install("AUX_BASE_URL=http://100.65.5.47:11434/v1\n", "--render-only", str(cfg))
        env = dict(os.environ, HERMES_HOME=str(self.home), HERMES_AGENT_DIR=str(AGENT_DIR),
                   ENV_FILE=str(self.tmp / "fixture.env"))
        ok = subprocess.run([sys.executable, "scripts/local-route.py", "readback"], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=300)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("live config matches the render", ok.stdout)
        cfg.write_text(cfg.read_text().replace("provider: custom\n    model: qwen3.5:27b",
                                               "provider: auto\n    model: qwen3.5:27b", 1))
        bad = subprocess.run([sys.executable, "scripts/local-route.py", "readback"], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=300)
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertIn("DIFF auxiliary", bad.stdout)
        self.assertIn("resolves to the MAIN (cloud) provider", bad.stdout)


class ConfigGetTest(unittest.TestCase):
    def test_subset_reader(self):
        cg = _config_get()
        text = LIVE_CONFIG + "x:\n  a:\n    settings:\n      k: wrong\n  settings:\n    k: right  # c\n" \
                             "y:\n  n: null\n  q: 'v: 1'\n  f: [a, \"b\"]\n"
        self.assertEqual(cg.lookup(text, "x.settings.k"), (True, "right"))
        self.assertEqual(cg.lookup(text, "y.n"), (True, None))
        self.assertEqual(cg.lookup(text, "y.q"), (True, "v: 1"))
        self.assertEqual(cg.lookup(text, "y.f"), (True, "a,b"))
        self.assertEqual(cg.lookup(text, f"{SETTINGS}.specialist_profiles"), (True, "strategy,philosophy"))
        self.assertEqual(cg.lookup(text, f"{SETTINGS}.missing"), (False, None))


if __name__ == "__main__":
    unittest.main()
