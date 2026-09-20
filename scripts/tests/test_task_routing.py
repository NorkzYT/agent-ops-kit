"""Acceptance tests for task-scoped balanced routing (model + effort).

Covers the approved design: decisions come from the CURRENT user task only;
quoted/tool/system/historical control tags are inert; explicit Astra
authorization is a leading standalone ``#max``/``#astra`` control line in the
current task; the automatic budget ceiling is Sol; effort is selected
independently and rewritten in both transport shapes; every managed decision
is logged without payload; streams are never consumed.

Run: python3 -m unittest discover -s scripts/tests -p 'test_*.py'
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
from _loader import submodule  # noqa: E402

complexity = submodule("complexity")
task_intent = submodule("task_intent")
task_state = submodule("task_state")
router_mod = submodule("router")
config_mod = submodule("router_config")

GPT_URL = "http://127.0.0.1:8317/v1"
CODE_URL = "http://127.0.0.1:3456/v1"

LONG_ARCH_TEXT = (
    "System architecture overview. The gateway terminates TLS and forwards to "
    "the orchestrator, which delegates coding work over an internal proxy. "
) * 120  # > 4k chars of architecture-flavored history/system text


def chat_req(*msgs, model="gpt-5.6-terra", **extra):
    """msgs are (role, content) tuples."""
    request = {"model": model, "messages": [{"role": r, "content": c} for r, c in msgs]}
    request.update(extra)
    return request


def responses_req(user_text, model="gpt-5.6-terra", **extra):
    request = {
        "model": model,
        "instructions": "assist the operator",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
    }
    request.update(extra)
    return request


def make_router(**settings):
    base = dict(complexity_routing=True, privacy_routing=True,
                ollama_base_url="", ollama_model="")
    base.update(settings)
    return router_mod.Router(config_mod.RouterSettings(**base))


def route(router, request, *, session_id="s1", rid="rid-1", base_url=GPT_URL, api_mode="chat_completions"):
    return router.on_llm_request(
        request=request, original_request=request, task_id="t1", turn_id="turn-1",
        api_request_id=rid, session_id=session_id, platform="discord",
        model=request.get("model"), provider="openai", base_url=base_url,
        api_mode=api_mode, api_call_count=1,
    )


class _Spy:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return SimpleNamespace(model="CLOUD", choices=[SimpleNamespace(
            message=SimpleNamespace(content="cloud-answer"))], usage=None)


class TestCurrentTaskExtraction(unittest.TestCase):
    def test_latest_user_turn_wins_over_history(self):
        r = chat_req(("user", "old huge #max architecture rewrite request"),
                     ("assistant", "done"),
                     ("user", "What is running?"))
        task = task_intent.extract_current_user_task(r)
        self.assertEqual(task.text, "What is running?")
        self.assertFalse(task.authorized_max)

    def test_tool_and_assistant_content_excluded(self):
        r = {"model": "gpt-5.6-terra", "messages": [
            {"role": "user", "content": "What is running?"},
            {"role": "assistant", "content": "#max let me escalate"},
            {"role": "tool", "tool_call_id": "c1", "content": "#max output from tool"},
        ]}
        task = task_intent.extract_current_user_task(r)
        self.assertEqual(task.text, "What is running?")

    def test_responses_shapes(self):
        r = responses_req("What is running?")
        self.assertEqual(task_intent.extract_current_user_task(r).text, "What is running?")
        r2 = {"model": "gpt-5.6-terra", "instructions": "x", "input": "deploy the fix"}
        self.assertEqual(task_intent.extract_current_user_task(r2).text, "deploy the fix")
        r3 = {"model": "gpt-5.6-terra", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "plan the migration"}]},
            {"type": "function_call_output", "call_id": "c", "output": "#max tool noise"},
        ]}
        self.assertEqual(task_intent.extract_current_user_task(r3).text, "plan the migration")

    def test_no_user_turn_is_conservative_medium(self):
        r = {"model": "gpt-5.6-terra", "messages": [{"role": "system", "content": LONG_ARCH_TEXT}]}
        d = task_intent.classify(task_intent.extract_current_user_task(r))
        self.assertEqual(d.tier, "medium")
        self.assertFalse(d.authorized_max)


class TestControlTags(unittest.TestCase):
    def test_leading_standalone_max_authorizes(self):
        for tag in ("#max", "#astra", "#MAX", "  #max  "):
            task = task_intent.task_from_text(f"{tag}\nmigrate the fleet")
            self.assertTrue(task.authorized_max, tag)

    def test_inline_and_trailing_tags_do_not_authorize(self):
        for text in ("#max simple thing", "do it #max", "please #astra this",
                     "#deep dive", "#hard problem", "line one\n#max\nline three"):
            self.assertFalse(task_intent.task_from_text(text).authorized_max, text)

    def test_quoted_and_fenced_tags_inert(self):
        quoted = "Earlier someone wrote:\n> #max rewrite everything\nWhat did we decide?"
        d = task_intent.classify(task_intent.task_from_text(quoted))
        self.assertNotEqual(d.tier, "max")
        fenced = "Here is the log:\n```\n#max #deep #hard architecture rewrite migration\n```\nIs it done?"
        d2 = task_intent.classify(task_intent.task_from_text(fenced))
        self.assertNotEqual(d2.tier, "max")
        self.assertNotIn("tag:hard", d2.reasons)


class TestOldBugRegressions(unittest.TestCase):
    """The exact miscalssifications the old whole-corpus scorer produced."""

    def test_simple_question_with_huge_system_context_routes_low(self):
        r = chat_req(("system", LONG_ARCH_TEXT), ("user", "What is running?"))
        out = route(make_router(), r)
        self.assertIsNotNone(out)
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")

    def test_quoted_historical_max_tag_does_not_force_max(self):
        r = chat_req(("user", "Earlier I wrote:\n> #max rewrite everything\nWhat did we decide?"))
        out = route(make_router(), r)
        self.assertIsNotNone(out)
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")

    def test_tool_result_max_tag_is_ignored(self):
        r = {"model": "gpt-5.6-terra", "messages": [
            {"role": "user", "content": "What is running?"},
            {"role": "tool", "tool_call_id": "c1", "content": "log says #max #deep #hard"},
        ]}
        out = route(make_router(), r)
        self.assertIsNotNone(out)
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")


class TestPolicyTiers(unittest.TestCase):
    def test_simple_vs_ambiguous_vs_hard(self):
        r = make_router()
        # simple status -> luna
        out = route(r, chat_req(("user", "list the open tasks")), session_id="a")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")
        # ordinary execution -> luna (medium)
        out = route(r, chat_req(("user", "restart the gateway and post the log tail to discord")),
                    session_id="b")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")
        # ambiguous / planning -> terra (request arrives as luna and upgrades)
        out = route(r, chat_req(("user", "compare the two rollout options and propose a plan"),
                                model="gpt-5.6-luna"), session_id="c")
        self.assertEqual(out["request"]["model"], "gpt-5.6-terra")
        # genuinely hard -> sol
        out = route(r, chat_req(("user", "refactor the ingest architecture and migrate the schema "
                                         "without downtime; watch for race conditions")),
                    session_id="d")
        self.assertEqual(out["request"]["model"], "gpt-5.6-sol")

    def test_hard_task_still_reaches_sol_and_fable(self):
        r = make_router()
        coding = {"model": "opus", "messages": [
            {"role": "user", "content": "refactor the worker architecture, migrate the schema, "
                                        "and fix the deadlock in the queue"}]}
        out = route(r, coding, session_id="cod", base_url=CODE_URL)
        self.assertEqual(out["request"]["model"], "fable")

    def test_coding_effort_fields_never_touched(self):
        r = make_router()
        coding = {"model": "opus", "reasoning_effort": "high", "messages": [
            {"role": "user", "content": "quick question: what does the Makefile doctor target do?"}]}
        out = route(r, coding, session_id="cod2", base_url=CODE_URL)
        # model stays opus (routine) -> no rewrite dict at all, effort untouched
        self.assertIsNone(out)
        self.assertEqual(coding["reasoning_effort"], "high")


class TestAstraAuthorization(unittest.TestCase):
    def test_authorized_current_task_reaches_astra(self):
        r = make_router()
        req = chat_req(("user", "#max\nmigrate the production database this weekend"))
        out = route(r, req, session_id="auth1", rid="r1")
        self.assertEqual(out["request"]["model"], "gpt-6-astra")
        # and the execution seam lets it through
        spy = _Spy()
        resp = r.on_llm_execution(request=out["request"], next_call=spy,
                                  session_id="auth1", api_request_id="r1", base_url=GPT_URL)
        self.assertEqual(spy.calls, 1)
        self.assertEqual(resp.model, "CLOUD")

    def test_authorization_does_not_bleed_to_next_task(self):
        r = make_router()
        route(r, chat_req(("user", "#max\nmigrate the production database")), session_id="s", rid="r1")
        out = route(r, chat_req(("user", "now refactor the ingest architecture and migrate the "
                                         "schema end-to-end")), session_id="s", rid="r2")
        self.assertEqual(out["request"]["model"], "gpt-5.6-sol")  # hard, but not astra

    def test_authorization_does_not_bleed_across_sessions(self):
        r = make_router()
        route(r, chat_req(("user", "#max\nmigrate the production database")), session_id="s1", rid="r1")
        out = route(r, chat_req(("user", "#deep\nmigrate the production database")), session_id="s2", rid="r2")
        self.assertNotEqual(out["request"]["model"], "gpt-6-astra")

    def test_unauthorized_astra_request_downgrades(self):
        r = make_router()
        req = chat_req(("user", "summarize yesterday"), model="gpt-6-astra")
        out = route(r, req, session_id="dg")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")

    def test_unauthorized_astra_blocked_at_execution_seam(self):
        r = make_router()
        spy = _Spy()
        req = chat_req(("user", "summarize yesterday"), model="gpt-6-astra")
        resp = r.on_llm_execution(request=req, next_call=spy, session_id="blk",
                                  api_request_id="", base_url=GPT_URL)
        self.assertEqual(spy.calls, 0, "unauthorized astra reached the provider")
        self.assertTrue(getattr(resp, "_model_router_refusal", False))

    def test_astra_policy_always_and_never(self):
        always = make_router(astra_policy="always")
        spy = _Spy()
        req = chat_req(("user", "hi"), model="gpt-6-astra")
        always.on_llm_execution(request=req, next_call=spy, session_id="p1", base_url=GPT_URL)
        self.assertEqual(spy.calls, 1)
        never = make_router(astra_policy="never")
        out = route(never, chat_req(("user", "#max\ndo the thing")), session_id="p2")
        self.assertNotEqual(out["request"]["model"] if out else "gpt-5.6-terra", "gpt-6-astra")

    def test_no_silent_upshift_when_lower_models_missing(self):
        r = make_router(available_gpt_models=["gpt-6-astra"])
        req = chat_req(("user", "What is running?"), model="gpt-5.6-luna")
        out = route(r, req, session_id="up")
        self.assertIsNone(out, "request must stay on its configured model, not upshift")

    def test_unavailable_everything_refuses_forbidden_astra(self):
        r = make_router(available_gpt_models=["something-else"])
        req = chat_req(("user", "small question?"), model="gpt-6-astra")
        self.assertIsNone(route(r, req, session_id="rf", rid="rr1"))
        spy = _Spy()
        resp = r.on_llm_execution(request=req, next_call=spy, session_id="rf",
                                  api_request_id="rr1", base_url=GPT_URL)
        self.assertEqual(spy.calls, 0)
        self.assertTrue(getattr(resp, "_model_router_refusal", False))


class TestContinuationAndState(unittest.TestCase):
    def test_continue_retains_running_tier_and_status_routes_low(self):
        r = make_router()
        hard = chat_req(("user", "refactor the ingest architecture and migrate the schema "
                                 "carefully; there is a deadlock risk"))
        out = route(r, hard, session_id="cont", rid="c1")
        self.assertEqual(out["request"]["model"], "gpt-5.6-sol")
        # status question mid-task -> low, state untouched
        out = route(r, chat_req(("user", "what's the status?")), session_id="cont", rid="c2")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")
        # continue -> retained sol
        out = route(r, chat_req(("user", "continue")), session_id="cont", rid="c3")
        self.assertEqual(out["request"]["model"], "gpt-5.6-sol")

    def test_continue_without_state_is_medium(self):
        out = route(make_router(), chat_req(("user", "continue")), session_id="fresh")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")

    def test_state_expires_on_ttl(self):
        clock = [0.0]
        store = task_state.TaskStateStore(ttl=100.0, clock=lambda: clock[0])
        store.put("s", task_state.TaskState("h", "high", "high", True))
        self.assertIsNotNone(store.get("s"))
        clock[0] = 101.0
        self.assertIsNone(store.get("s"), "state (and authorization) must expire")

    def test_state_absolute_cap_despite_touches(self):
        clock = [0.0]
        store = task_state.TaskStateStore(ttl=100.0, max_age_factor=2.0, clock=lambda: clock[0])
        store.put("s", task_state.TaskState("h", "high", "high", False))
        for step in range(1, 6):
            clock[0] = step * 50.0
            store.touch("s")
        self.assertIsNone(store.get("s"), "sliding touches must not defeat the absolute cap")

    def test_store_is_lru_bounded(self):
        store = task_state.TaskStateStore(ttl=100.0, max_entries=8, clock=lambda: 0.0)
        for i in range(20):
            store.put(f"s{i}", task_state.TaskState(f"h{i}", "medium", "medium"))
        self.assertLessEqual(len(store._states), 8)


class TestEffortTransport(unittest.TestCase):
    def test_chat_completions_effort_fields_rewritten_consistently(self):
        req = chat_req(("user", "What is running?"),
                       reasoning_effort="high",
                       extra_body={"reasoning": {"enabled": True, "effort": "high"}})
        out = route(make_router(), req)
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")
        self.assertEqual(out["request"]["reasoning_effort"], "low")
        self.assertEqual(out["request"]["extra_body"]["reasoning"]["effort"], "low")
        # no stale conflicting field left behind
        self.assertEqual(len({out["request"]["reasoning_effort"],
                              out["request"]["extra_body"]["reasoning"]["effort"]}), 1)

    def test_responses_effort_field_rewritten(self):
        req = responses_req("What is running?", reasoning={"effort": "high", "summary": "auto"})
        out = route(make_router(), req, api_mode="codex_responses")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")
        self.assertEqual(out["request"]["reasoning"]["effort"], "low")
        self.assertEqual(out["request"]["reasoning"]["summary"], "auto")  # siblings preserved

    def test_effort_never_raised_above_request_ceiling(self):
        req = chat_req(("user", "refactor the ingest architecture and migrate the schema; "
                                "deadlock risk everywhere; end-to-end rewrite"),
                       reasoning_effort="medium")
        out = route(make_router(), req)
        self.assertEqual(out["request"]["model"], "gpt-5.6-sol")
        self.assertEqual(out["request"]["reasoning_effort"], "medium")  # ceiling respected

    def test_no_effort_field_is_never_invented(self):
        req = chat_req(("user", "What is running?"))
        out = route(make_router(), req)
        self.assertNotIn("reasoning_effort", out["request"])
        self.assertNotIn("reasoning", out["request"])

    def test_disabled_reasoning_untouched(self):
        req = responses_req("What is running?", reasoning={"enabled": False, "effort": "high"})
        out = route(make_router(), req)
        self.assertEqual(out["request"]["reasoning"], {"enabled": False, "effort": "high"})

    def test_effort_routing_can_be_disabled(self):
        req = chat_req(("user", "What is running?"), reasoning_effort="high")
        out = route(make_router(effort_routing=False), req)
        self.assertEqual(out["request"]["reasoning_effort"], "high")
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")

    def test_unrelated_fields_preserved_and_no_mutation(self):
        req = chat_req(("user", "What is running?"),
                       reasoning_effort="high", temperature=0.3,
                       metadata={"keep": "me"})
        import copy
        snapshot = copy.deepcopy(req)
        out = route(make_router(), req)
        self.assertEqual(req, snapshot, "original request must not be mutated")
        self.assertEqual(out["request"]["temperature"], 0.3)
        self.assertEqual(out["request"]["metadata"], {"keep": "me"})


class TestObservability(unittest.TestCase):
    CANARY = "ZanzibarPurpleTelescope"

    def test_every_managed_decision_logged_even_unchanged(self):
        r = make_router()
        req = chat_req(("user", "restart the gateway please"), model="gpt-5.6-luna")
        with self.assertLogs("hermes.plugin.model_router", level="INFO") as captured:
            out = route(r, req, session_id="log1", rid="ridA")
        self.assertIsNone(out)  # luna medium -> luna: unchanged
        joined = "\n".join(captured.output)
        self.assertIn("outcome=unchanged", joined)
        self.assertIn("rid=ridA", joined)

    def test_logs_never_contain_payload(self):
        r = make_router()
        req = chat_req(("user", f"refactor the {self.CANARY} architecture and migrate the schema"))
        with self.assertLogs("hermes.plugin.model_router", level="INFO") as captured:
            route(r, req, session_id=f"sess-{self.CANARY}", rid="ridB")
            spy = _Spy()
            r.on_llm_execution(request=req, next_call=spy, session_id=f"sess-{self.CANARY}",
                               api_request_id="ridB", base_url=GPT_URL)
        joined = "\n".join(captured.output)
        self.assertNotIn(self.CANARY, joined, "payload/session text leaked into logs")

    def test_completion_outcome_logged_success_and_failure(self):
        r = make_router()
        req = chat_req(("user", "What is running?"))
        with self.assertLogs("hermes.plugin.model_router", level="INFO") as captured:
            spy = _Spy()
            r.on_llm_execution(request=req, next_call=spy, session_id="oc", api_request_id="r1",
                               base_url=GPT_URL)
        self.assertIn("status=completed", "\n".join(captured.output))
        self.assertIn("served=CLOUD", "\n".join(captured.output))

        boom = RuntimeError("secret-laden provider message " + self.CANARY)
        def failing(_request):
            raise boom
        with self.assertLogs("hermes.plugin.model_router", level="INFO") as captured:
            with self.assertRaises(RuntimeError) as caught:
                r.on_llm_execution(request=req, next_call=failing, session_id="oc",
                                   api_request_id="r2", base_url=GPT_URL)
        self.assertIs(caught.exception, boom)  # identity preserved, not masked
        joined = "\n".join(captured.output)
        self.assertIn("status=failed", joined)
        self.assertIn("error=RuntimeError", joined)
        self.assertNotIn(self.CANARY, joined, "raw provider error leaked into logs")

    def test_streaming_response_not_consumed(self):
        r = make_router()
        req = chat_req(("user", "What is running?"))
        def gen():
            yield "chunk-1"
            yield "chunk-2"
        stream = gen()
        resp = r.on_llm_execution(request=req, next_call=lambda _rq: stream,
                                  session_id="st", api_request_id="r3", base_url=GPT_URL)
        self.assertIs(resp, stream)
        self.assertEqual(list(resp), ["chunk-1", "chunk-2"], "stream was consumed by logging")


class TestCompatibility(unittest.TestCase):
    def test_old_settings_shape_still_works(self):
        # Pre-upgrade config: none of the new knobs present.
        settings = config_mod.RouterSettings(
            complexity_routing=True, privacy_routing=True,
            ollama_base_url="", ollama_model="")
        self.assertEqual(settings.astra_policy, "task")
        self.assertTrue(settings.effort_routing)
        self.assertEqual(settings.task_ttl, 1800.0)

    def test_env_fallbacks_for_new_knobs(self):
        old = {k: os.environ.get(k) for k in
               ("MODEL_ROUTER_EFFORT", "MODEL_ROUTER_ASTRA", "MODEL_ROUTER_TASK_TTL")}
        try:
            os.environ["MODEL_ROUTER_EFFORT"] = "false"
            os.environ["MODEL_ROUTER_ASTRA"] = "never"
            os.environ["MODEL_ROUTER_TASK_TTL"] = "600"
            settings = config_mod.load_settings(None)
            self.assertFalse(settings.effort_routing)
            self.assertEqual(settings.astra_policy, "never")
            self.assertEqual(settings.task_ttl, 600.0)
            os.environ["MODEL_ROUTER_ASTRA"] = "bogus-value"
            self.assertEqual(config_mod.load_settings(None).astra_policy, "task")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_complexity_disabled_disables_rewrite_and_gate(self):
        r = make_router(complexity_routing=False)
        self.assertIsNone(route(r, chat_req(("user", "#max\nhuge task"))))
        spy = _Spy()
        req = chat_req(("user", "hello"), model="gpt-6-astra")
        r.on_llm_execution(request=req, next_call=spy, session_id="dis", base_url=GPT_URL)
        self.assertEqual(spy.calls, 1, "disabled complexity must not gate models")

    def test_unmanaged_model_untouched_by_gate(self):
        r = make_router()
        spy = _Spy()
        req = chat_req(("user", "hello"), model="some-custom-id")
        r.on_llm_execution(request=req, next_call=spy, session_id="um", base_url=GPT_URL)
        self.assertEqual(spy.calls, 1)

    def test_score_request_string_compat(self):
        self.assertEqual(complexity.score_request("hi there").tier, "routine")
        self.assertEqual(complexity.score_request("#max\nbig job").tier, "max")


class TestCoreMiddlewareContract(unittest.TestCase):
    """The router's assumptions about Hermes core middleware metadata."""

    CORE = os.environ.get(
        "HERMES_CORE_DIR", os.path.expanduser("~/.hermes/hermes-agent"))

    def test_full_core_kwargs_signature_accepted(self):
        # Mirrors hermes_cli.middleware payload keys for llm_request/llm_execution.
        r = make_router()
        common = dict(
            task_id="task-1", turn_id="turn-1", api_request_id="rid-x",
            session_id="sess-x", platform="discord", provider="openai",
            base_url=GPT_URL, api_mode="chat_completions", api_call_count=3,
            telemetry_schema_version="hermes.observer.v1",
            middleware_schema_version="hermes.middleware.v1",
        )
        req = chat_req(("user", "What is running?"))
        out = r.on_llm_request(request=req, original_request=req, model=req["model"], **common)
        self.assertEqual(out["request"]["model"], "gpt-5.6-luna")
        spy = _Spy()
        resp = r.on_llm_execution(request=req, next_call=spy, model=req["model"],
                                  middleware_trace=[], **common)
        self.assertEqual(spy.calls, 1)
        self.assertIsNotNone(resp)

    @unittest.skipUnless(os.path.isdir(os.path.join(CORE, "agent")), "Hermes core not present")
    def test_core_passes_session_and_request_ids(self):
        """Read-only check against the installed core: both middleware call
        sites pass session_id and api_request_id, which the task-state and
        correlation logic depend on."""
        for rel in ("agent/turn_api_request.py", "agent/turn_api_call.py"):
            path = os.path.join(self.CORE, rel)
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("session_id=agent.session_id", src, rel)
            self.assertIn("api_request_id=api_request_id", src, rel)


if __name__ == "__main__":
    unittest.main()
