"""Router: task-scoped complexity rewrite (llm_request) + fail-closed privacy
and Astra budget enforcement (llm_execution).

The privacy handler is the load-bearing security boundary. A request is private
only when its current user turn carries a sender-authored ``#privacy`` marker
(``privacy_marker``); there is no content classification and no session
stickiness. Errors while classifying or routing private traffic must return a
local result or refusal, never fall through to cloud. Once a provider call is
permitted, its exceptions propagate unchanged instead of being mislabeled as
privacy refusals.

Routing decisions come from the CURRENT user task only (``task_intent``), with
bounded per-session state (``task_state``) so a "continue" retains the running
task's tier while a status question routes cheap. Every managed decision — even
an unchanged one — is logged with opaque hashes, tier/reason labels and
transport outcomes; never prompt content, secrets, or raw provider errors.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import logging
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

from . import complexity as cx
from . import privacy as pv
from . import privacy_marker as pm
from . import task_intent as ti
from .local_backend import (
    LocalBackend,
    LocalUnavailable,
    is_local_endpoint,
    messages_from_request,
    private_chat_policy,
    refusal_response,
    run_local_chain,
)
from .router_config import RouterSettings
from .task_state import TaskState, TaskStateStore

logger = logging.getLogger("hermes.plugin.model_router")

# Low -> high; used only to compare/clamp efforts already present on a request.
_EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _is_coding_family(request: Any, model: str, base_url: str) -> bool:
    """Delegation/coding calls go to claude-max-proxy; orchestrator to CLIProxy."""
    hay = f"{base_url or ''}".lower()
    if ":3456" in hay or "claude-max" in hay or "claude_max" in hay:
        return True
    m = (model or "").strip().lower()
    return m in ("opus", "fable", "sonnet", "haiku", "default", "best")


def _hash8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else "-"


def _effort_rank(effort: Any) -> Optional[int]:
    e = str(effort or "").strip().lower()
    return _EFFORT_LADDER.index(e) if e in _EFFORT_LADDER else None


def _present_efforts(request: Dict[str, Any]) -> Dict[str, str]:
    """Effort fields already on the wire payload (both transport shapes).

    Chat Completions carries top-level ``reasoning_effort`` and/or
    ``extra_body.reasoning.effort``; the Responses API carries top-level
    ``reasoning.effort``. Fields with reasoning explicitly disabled
    (``enabled: false`` or effort ``none``) are excluded — the router never
    re-enables or alters disabled thinking.
    """
    found: Dict[str, str] = {}
    v = request.get("reasoning_effort")
    if isinstance(v, str) and v.strip().lower() != "none":
        found["reasoning_effort"] = v.strip().lower()
    r = request.get("reasoning")
    if isinstance(r, dict) and r.get("enabled") is not False:
        e = r.get("effort")
        if isinstance(e, str) and e.strip().lower() != "none":
            found["reasoning.effort"] = e.strip().lower()
    eb = request.get("extra_body")
    if isinstance(eb, dict):
        ebr = eb.get("reasoning")
        if isinstance(ebr, dict) and ebr.get("enabled") is not False:
            e = ebr.get("effort")
            if isinstance(e, str) and e.strip().lower() != "none":
                found["extra_body.reasoning.effort"] = e.strip().lower()
    return found


def _apply_effort(new_request: Dict[str, Any], request: Dict[str, Any], effort: str) -> bool:
    """Set every present (enabled) effort field to ``effort`` via copied containers.

    Rewriting all present fields keeps the two transport shapes consistent, so
    a stale conflicting ``high`` can never ride along next to the routed value.
    Fields that do not exist are never invented. The original request is not
    mutated.
    """
    changed = False
    v = request.get("reasoning_effort")
    if isinstance(v, str) and v.strip().lower() not in ("none", effort):
        new_request["reasoning_effort"] = effort
        changed = True
    r = request.get("reasoning")
    if isinstance(r, dict) and r.get("enabled") is not False:
        e = r.get("effort")
        if isinstance(e, str) and e.strip().lower() not in ("none", effort):
            nr = dict(r)
            nr["effort"] = effort
            new_request["reasoning"] = nr
            changed = True
    eb = request.get("extra_body")
    if isinstance(eb, dict):
        ebr = eb.get("reasoning")
        if isinstance(ebr, dict) and ebr.get("enabled") is not False:
            e = ebr.get("effort")
            if isinstance(e, str) and e.strip().lower() not in ("none", effort):
                neb = dict(eb)
                nebr = dict(ebr)
                nebr["effort"] = effort
                neb["reasoning"] = nebr
                new_request["extra_body"] = neb
                changed = True
    return changed


# Request keys that carry (or explicitly disable) reasoning in any shape the
# coding proxy reads. When none is present the coding request has no effort
# field at all and the proxy would fall back to its DEFAULT_THINKING_BUDGET.
_REASONING_KEYS = ("reasoning_effort", "reasoning", "thinking", "output_config")


def _route_coding_effort(new_request: Dict[str, Any], request: Dict[str, Any], target: str) -> tuple:
    """Set the coding effort to ``target``; returns ``(changed, log_note)``.

    Present enabled fields are rewritten in every shape (``_apply_effort``).
    A request carrying no reasoning field gets a top-level ``reasoning_effort``
    (the field claude-max-proxy reads; the Hermes custom provider emits the
    same one). Explicitly disabled or unrecognized reasoning shapes are left
    alone — thinking is never re-enabled.
    """
    present = _present_efforts(request)
    if present:
        changed = _apply_effort(new_request, request, target)
        return changed, f"{'/'.join(sorted(set(present.values())))}->{target}"
    eb = request.get("extra_body")
    if any(k in request for k in _REASONING_KEYS) or (
            isinstance(eb, dict) and any(k in eb for k in _REASONING_KEYS)):
        return False, "off"
    new_request["reasoning_effort"] = target
    return True, f"unset->{target}"


def _looks_like_stream(response: Any) -> bool:
    """True when ``next_call`` returned a lazy object whose completion we will
    never observe (generator, async iterator, awaitable, or an SDK stream
    wrapper). Inspection is attribute-only — the object is never advanced."""
    if response is None or isinstance(response, (dict, list, tuple, str, bytes)):
        return False
    if inspect.isgenerator(response) or inspect.isasyncgen(response) or inspect.isawaitable(response):
        return True
    if hasattr(response, "__aiter__"):
        return True
    # Iterable wrapper carrying no final-response evidence (usage/choices/output).
    if hasattr(response, "__iter__") and not any(
        getattr(response, attr, None) is not None for attr in ("usage", "choices", "output")
    ):
        return True
    return False


def _usage_summary(response: Any) -> str:
    """Token counts from a completed (non-streaming) response, else 'unavailable'.

    Streaming responses are never touched or consumed — a missing ``usage``
    attribute simply reports as unavailable rather than pretending a count.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return "unavailable"
    def _num(*names: str) -> Optional[int]:
        for name in names:
            v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if isinstance(v, int):
                return v
        return None
    parts = []
    inp, out = _num("prompt_tokens", "input_tokens"), _num("completion_tokens", "output_tokens")
    if inp is not None:
        parts.append(f"in:{inp}")
    if out is not None:
        parts.append(f"out:{out}")
    return ",".join(parts) or "unavailable"


class Router:
    def __init__(self, settings: RouterSettings, *, backend: Optional[LocalBackend] = None,
                 backend_factory: Optional[Callable[[str], Any]] = None) -> None:
        self.settings = settings
        # ``backend`` (tests) answers for every local model; otherwise each
        # model in the role chain gets its own client on the same endpoint.
        self._backend = backend
        self._backend_factory = backend_factory
        self._tasks = TaskStateStore(ttl=settings.task_ttl)
        # Request-stage decisions keyed by api_request_id so the execution seam
        # can enforce Astra authorization and correlate outcomes without
        # re-deriving (or logging) anything from the payload. Bounded.
        self._decisions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ---------------------------------------------------------------- privacy
    def is_private(self, request: Any, session_id: str = "") -> pv.PrivacyDecision:
        """Per-request, fail-closed decision: private only when the current user
        turn carries ``#privacy``. Nothing is remembered for the session. Any
        internal error => private."""
        try:
            return pm.decide(request)
        except Exception:  # never let classification failure fall open
            logger.warning("model_router: privacy marker check errored; treating as private")
            return pv.PrivacyDecision(True, ("classifier-error",))

    def _local_backend(self, model: str) -> Any:
        if self._backend is not None:
            return self._backend
        if self._backend_factory is not None:
            return self._backend_factory(model)
        return LocalBackend(self.settings.ollama_base_url, model, timeout=self.settings.ollama_timeout)

    def local_role(self, request: Any, session_id: str, kwargs: Dict[str, Any]) -> str:
        """Which local role answers a private request: ``agent`` for delegated
        coding, a forced coding control or a high/max task; ``routine`` for
        everything else. Decided from the current task only; an error picks
        the stronger ``agent`` model (the role never affects where data goes)."""
        try:
            model = str((request.get("model") if isinstance(request, dict) else "")
                        or kwargs.get("model") or "")
            if _is_coding_family(request, model, str(kwargs.get("base_url") or "")):
                return "agent"
            decision = ti.classify(ti.extract_current_user_task(request), self._tasks.get(session_id))
            if decision.forced_coding or decision.tier in ("high", "max"):
                return "agent"
            return "routine"
        except Exception:
            return "agent"

    def _local_or_refuse(self, request: Any, reason: str, *, role: str = "routine") -> Any:
        """Answer locally (role model, then the one fallback model) or refuse.
        Never calls a cloud provider; a non-local endpoint is refused unsent."""
        try:
            url = self.settings.ollama_base_url
            if self._backend is None and url and not is_local_endpoint(url):
                logger.warning("model_router: private->refuse (endpoint not local) reason=%s role=%s",
                               reason, role)
                return refusal_response(reason="local-endpoint-not-local")
            chain = self.settings.local_chain(role)
            messages = messages_from_request(request)
            options, budget = private_chat_policy(
                messages, context_tokens=self.settings.ollama_context_tokens,
                base_timeout=self.settings.ollama_timeout)
            resp, model, attempt = run_local_chain(chain, messages, self._local_backend,
                                                   budget=budget, options=options)
            logger.info("model_router: private->local ok reason=%s role=%s model=%s attempt=%d/%d "
                        "max_tokens=%d budget=%.0fs", reason, role, model, attempt, len(chain),
                        options["max_tokens"], budget)
            return resp
        except LocalUnavailable as exc:
            logger.warning("model_router: private->refuse (local unavailable: %s) reason=%s role=%s",
                           getattr(exc, "kind", "error"), reason, role)
            return refusal_response(reason="local-unavailable")
        except Exception:  # defensive: still must not raise
            logger.warning("model_router: private->refuse (unexpected local error)")
            return refusal_response(reason="local-error")

    # ----------------------------------------------------- astra authorization
    def _astra_authorized(self, rid: str, session_id: str, request: Any) -> bool:
        """Is a max-tier (Astra) request authorized for the CURRENT task?

        Order: recorded request-stage decision for this api_request_id, then
        fresh (session-state-aware) evaluation of the current user task. The
        policy knob may force "always" (legacy) or "never".
        """
        policy = self.settings.astra_policy
        if policy == "always":
            return True
        if policy == "never":
            return False
        if rid and rid in self._decisions:
            return bool(self._decisions[rid].get("authorized"))
        task = ti.extract_current_user_task(request)
        decision = ti.classify(task, self._tasks.get(session_id))
        # A hash whose absolute lifetime is spent cannot re-authorize: the same
        # historical `#max` message at the tail of the transcript stays inert.
        return decision.authorized_max and not self._tasks.retired(session_id, task.task_hash)

    def on_llm_execution(self, **kwargs: Any) -> Any:
        """Fail closed on privacy errors; preserve permitted provider errors."""
        request = kwargs.get("request")
        next_call: Callable[[Any], Any] = kwargs.get("next_call")  # type: ignore[assignment]
        session_id = str(kwargs.get("session_id") or "")
        rid = str(kwargs.get("api_request_id") or "")
        try:
            if self.settings.privacy_routing:
                decision = self.is_private(request, session_id)
                if decision.private:
                    logger.info("model_router: private detected reasons=%s (no cloud egress)", decision.label)
                    return self._local_or_refuse(request, decision.label,
                                                 role=self.local_role(request, session_id, kwargs))
                # Public request: earlier #privacy exchanges in the history stay
                # off the cloud. A withholding error keeps the request local.
                try:
                    request, withheld = pm.withhold_marked_history(request)
                except Exception:
                    logger.warning("model_router: private-history withholding errored; treating as private")
                    return self._local_or_refuse(request, "withhold-error",
                                                 role=self.local_role(request, session_id, kwargs))
                if withheld:
                    logger.info("model_router: public request; withheld %d private-history item(s) "
                                "sid=%s", withheld, _hash8(session_id))
        except Exception:  # privacy failures must never fall open to cloud
            logger.warning("model_router: llm_execution guard tripped; refusing")
            return refusal_response(reason="router-guard")

        # Forced-coding seam: a #opus/#fable control names a Claude coding model
        # that lives behind the coding proxy. The middleware contract cannot move
        # a call to another provider, so a forced control on the orchestrator/GPT
        # seam is refused here with a typed response rather than sent to the wrong
        # provider or faked with a GPT alias. Privacy is enforced ABOVE, so a
        # private forced request has already gone local/refused and never reaches
        # this cloud path — the privacy/local route stays the higher priority.
        try:
            if self.settings.complexity_routing and isinstance(request, dict):
                forced = self._forced_coding_unroutable(rid, session_id, request, kwargs)
                if forced:
                    logger.warning(
                        "model_router: refuse rid=%s sid=%s reason=forced-coding-unroutable model=%s",
                        rid or "-", _hash8(session_id), forced,
                    )
                    return refusal_response(
                        model="model-router/forced-coding-unroutable",
                        reason="forced-coding-unroutable",
                        text=(
                            "This request begins with a `#opus`/`#fable` control, which "
                            "forces the Claude coding model. That model is reachable only "
                            "on delegated/coding requests (the coding proxy); this "
                            "orchestrator request cannot be redirected to it, so it was "
                            "not sent. Delegate the task (it routes to the coding proxy) "
                            "or drop the control to use the orchestrator normally."
                        ),
                    )
        except Exception:  # the forced-coding gate must not fall open
            logger.warning("model_router: forced-coding gate errored; refusing")
            return refusal_response(reason="forced-coding-gate-error")

        # Budget seam: an unauthorized max-tier (Astra) request is refused with
        # a typed response rather than sent (or silently left) on the wire.
        # Raising here would trip Hermes' fail-open middleware handling and let
        # the request through, so a refusal object is the fail-closed shape.
        managed = False
        try:
            if self.settings.complexity_routing and isinstance(request, dict):
                model = str(request.get("model") or kwargs.get("model") or "")
                tier = cx.alias_to_tier(model)
                managed = tier is not None
                if tier == "max" and not self._astra_authorized(rid, session_id, request):
                    logger.warning(
                        "model_router: refuse rid=%s sid=%s reason=astra-unauthorized model=%s",
                        rid or "-", _hash8(session_id), model,
                    )
                    return refusal_response(
                        model="model-router/astra-blocked",
                        reason="astra-unauthorized",
                        text=(
                            "This request targets the max-tier model but the current "
                            "task carries no explicit authorization (start the task "
                            "with a standalone `#max` or `#astra` line), so it was "
                            "not sent. Re-issue with authorization or use a lower tier."
                        ),
                    )
        except Exception:  # the budget gate must not fall open into Astra
            logger.warning("model_router: astra gate errored; refusing max-tier request")
            return refusal_response(reason="astra-gate-error")

        # Public traffic (or an explicit privacy opt-out) is permitted here.
        # Provider failures belong to normal error/retry handling, not to the
        # privacy guard: catching them above falsely reports private content.
        if not managed:
            return next_call(request)
        # Managed request: record the transport outcome. The error is re-raised
        # unchanged (identity preserved) — only its type name is logged, never
        # its message, which can embed payload fragments.
        try:
            response = next_call(request)
        except BaseException as exc:
            self._log_outcome(rid, session_id, kwargs, "failed", error=type(exc).__name__)
            raise
        if _looks_like_stream(response):
            # The transport handed back a lazy stream: the call is opened, not
            # finished. Claiming "completed" here would be evidence-free — the
            # stream may still error or be abandoned downstream. It is returned
            # untouched (never advanced), so completion stays unobserved.
            self._log_outcome(rid, session_id, kwargs, "stream-opened", stream=True)
        else:
            self._log_outcome(rid, session_id, kwargs, "completed", response=response)
        return response

    def _log_outcome(
        self, rid: str, session_id: str, kwargs: Dict[str, Any], status: str,
        *, response: Any = None, error: str = "", stream: bool = False,
    ) -> None:
        """Completion/outcome telemetry. ``served`` comes only from actual
        response evidence (the provider-reported ``model`` attribute); a
        selected/requested model is never presented as the model executed.
        For a lazy stream nothing final exists yet, so served/usage report as
        unobserved rather than pretending a completion happened."""
        try:
            entry = self._decisions.get(rid, {})
            served = getattr(response, "model", None)
            if served is None and isinstance(response, dict):
                served = response.get("model")
            req = kwargs.get("request")
            requested = "-"
            if isinstance(req, dict) and req.get("model"):
                requested = str(req["model"])
            elif kwargs.get("model"):
                requested = str(kwargs["model"])
            logger.info(
                "model_router: outcome rid=%s sid=%s task=%s status=%s requested=%s served=%s usage=%s%s%s",
                rid or "-", _hash8(session_id), entry.get("task8", "-"), status, requested,
                served if isinstance(served, str) else ("unobserved" if stream else "unknown"),
                "unobserved" if stream else (
                    _usage_summary(response) if response is not None else "unavailable"),
                " completion=unobserved" if stream else "",
                f" error={error}" if error else "",
            )
        except Exception:  # telemetry must never affect the call path
            pass

    # ------------------------------------------------------------- complexity
    def _record_decision(self, rid: str, entry: Dict[str, Any]) -> None:
        if not rid:
            return
        self._decisions[rid] = entry
        self._decisions.move_to_end(rid)
        while len(self._decisions) > 512:
            self._decisions.popitem(last=False)

    # --------------------------------------------------------- forced coding
    def _route_forced_coding(
        self, request: Dict[str, Any], rid: str, session_id: str, coding: bool,
        model_alias: str, current: str, task: Any,
    ) -> Optional[Dict[str, Any]]:
        """Honor a leading #opus/#fable control. On a coding-family seam force the
        exact coding model and pick its effort from the task body (the control
        names the model, not the effort; it never authorizes max effort); on the
        GPT seam the provider cannot be switched here, so record the forced
        request for the execution seam to refuse and leave the body unchanged."""
        sid8 = _hash8(session_id)
        task8 = task.task_hash[:8] if task.task_hash else "-"
        if not coding:
            # Orchestrator/GPT seam: the middleware contract cannot move a call
            # to the coding provider (base_url/provider are bound on the agent
            # before middleware; only the request body is rewritable). Do NOT
            # substitute a GPT alias — the execution seam refuses this request.
            self._record_decision(rid, {"forced_coding": model_alias, "unroutable": True, "task8": task8})
            logger.info(
                "model_router: route rid=%s sid=%s task=%s family=gpt tier=force auth=0 "
                "model=%s->%s effort=- reasons=force:%s outcome=forced-coding-unroutable",
                rid or "-", sid8, task8, current, current, model_alias,
            )
            return None
        chosen = cx.resolve_forced_coding(
            model_alias, available=self.settings.available_coding_models or None)
        if not chosen:
            # The named coding model is not in the live set: keep the request as
            # configured and let normal error/fallback handling apply. The log
            # still identifies the forced request.
            self._record_decision(rid, {"forced_coding": model_alias, "task8": task8})
            logger.info(
                "model_router: route rid=%s sid=%s task=%s family=coding tier=force auth=0 "
                "model=%s->%s effort=- reasons=force:%s outcome=no-candidate",
                rid or "-", sid8, task8, current, current, model_alias,
            )
            return None
        new_request = dict(request)
        model_changed = chosen.strip().lower() != current.strip().lower()
        if model_changed:
            new_request["model"] = chosen
        effort_note, effort_changed = "-", False
        if self.settings.coding_effort_routing:
            body = ti.classify(dataclasses.replace(task, forced_coding=""), self._tasks.get(session_id))
            tier = "high" if body.tier == "max" else body.tier
            target = cx.coding_effort(tier, body.effort, body.reasons,
                                      ceiling=self.settings.coding_effort_ceiling)
            effort_changed, effort_note = _route_coding_effort(new_request, request, target)
        self._record_decision(rid, {"forced_coding": model_alias, "selected": chosen, "task8": task8})
        logger.info(
            "model_router: route rid=%s sid=%s task=%s family=coding tier=force auth=0 "
            "model=%s->%s effort=%s reasons=force:%s outcome=%s",
            rid or "-", sid8, task8, current, chosen if model_changed else current, effort_note,
            model_alias, "forced-coding" if model_changed else (
                "rewritten" if effort_changed else "unchanged"),
        )
        if not (model_changed or effort_changed):
            return None
        return {"request": new_request, "source": "model-router", "reason": f"force:{model_alias}"}

    def _forced_coding_unroutable(
        self, rid: str, session_id: str, request: Any, kwargs: Dict[str, Any]) -> str:
        """The forced coding model ('opus'/'fable') when the CURRENT task carries
        a leading #opus/#fable control but this seam is NOT coding-family (so the
        call cannot reach the coding provider). '' otherwise — including on a
        coding seam, where the force is already honored at the request stage."""
        model = str(request.get("model") or kwargs.get("model") or "")
        base_url = str(kwargs.get("base_url") or "")
        if _is_coding_family(request, model, base_url):
            return ""
        if rid and rid in self._decisions:
            entry = self._decisions[rid]
            return str(entry.get("forced_coding") or "") if entry.get("unroutable") else ""
        return ti.extract_current_user_task(request).forced_coding or ""

    def on_llm_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Rewrite model and reasoning effort for the current task."""
        if not self.settings.complexity_routing:
            return None
        try:
            request = kwargs.get("request")
            if not isinstance(request, dict):
                return None
            current = str(request.get("model") or kwargs.get("model") or "")
            base_url = str(kwargs.get("base_url") or "")
            # Only act on models we manage (known aliases); leave deliberate ids alone.
            current_tier = cx.alias_to_tier(current)
            if current_tier is None:
                return None
            session_id = str(kwargs.get("session_id") or "")
            rid = str(kwargs.get("api_request_id") or "")
            coding = _is_coding_family(request, current, base_url)
            family = "coding" if coding else "gpt"
            available = (self.settings.available_coding_models if coding
                         else self.settings.available_gpt_models)

            task = ti.extract_current_user_task(request)
            prev = self._tasks.get(session_id)
            decision = ti.classify(task, prev)

            # Highest-precedence explicit control: a leading standalone #opus /
            # #fable line forces the Claude coding model regardless of task shape,
            # overriding complexity and Astra for THIS request only (no task
            # state or authorization is stored). The request-middleware contract
            # rewrites the request body but cannot switch provider/base_url, so a
            # forced control is honored only on a coding-family seam; on the GPT
            # seam it is recorded for the execution seam to refuse — never a GPT
            # alias substituted for the coding model.
            if decision.forced_coding:
                return self._route_forced_coding(
                    request, rid, session_id, coding, decision.forced_coding, current, task)

            if self.settings.astra_policy == "always":
                authorized = True
            elif self.settings.astra_policy == "never":
                authorized = False
            else:
                # An expired (retired) task hash cannot re-authorize itself —
                # the agent loop re-presents the same latest user text every
                # iteration, and that must not renew Astra past the absolute cap.
                authorized = (decision.authorized_max
                              and not self._tasks.retired(session_id, task.task_hash))
            tier = decision.tier
            if tier == "max" and not authorized:
                tier = "high"  # automatic ceiling: Sol

            # Bounded task state: a continuation refreshes the running task, a
            # new substantive request replaces it (authorization never carries
            # over), and a status question leaves it untouched. The store keeps
            # the original `created` stamp for a repeated identical task, so
            # per-iteration re-puts cannot restart the absolute lifetime.
            if decision.continuation and prev is not None:
                self._tasks.touch(session_id)
            elif decision.substantive and task.found:
                self._tasks.put(session_id, TaskState(
                    task.task_hash, tier, decision.effort, authorized,
                ))

            chosen = cx.resolve_model(tier, family=family, available=available or None,
                                      allow_max=authorized)
            sid8, task8 = _hash8(session_id), (task.task_hash[:8] if task.task_hash else "-")

            if not chosen:
                # No allowed model available. An unauthorized max-tier request
                # must not go out unchanged — the execution seam will refuse it.
                outcome = "refuse-scheduled" if current_tier == "max" and not authorized else "no-candidate"
                self._record_decision(rid, {"authorized": authorized, "task8": task8})
                logger.info(
                    "model_router: route rid=%s sid=%s task=%s family=%s tier=%s auth=%d "
                    "model=%s->%s effort=- reasons=%s outcome=%s",
                    rid or "-", sid8, task8, family, tier, int(authorized),
                    current, current, decision.label, outcome,
                )
                return None

            new_request = dict(request)
            model_changed = chosen.strip().lower() != current.strip().lower()
            if model_changed:
                new_request["model"] = chosen

            # Effort is selected independently of the model. GPT family: only
            # by rewriting fields the request already carries, clamped so the
            # router never raises effort above what was configured. Coding
            # family: the task-based Claude effort (medium/high/xhigh, max only
            # when authorized), clamped to coding_effort_ceiling; claude-max-
            # proxy maps the label to the CLI thinking budget.
            effort_note = "-"
            effort_changed = False
            if family == "coding" and self.settings.coding_effort_routing:
                target = cx.coding_effort(tier, decision.effort, decision.reasons,
                                          ceiling=self.settings.coding_effort_ceiling)
                effort_changed, effort_note = _route_coding_effort(new_request, request, target)
            elif family == "gpt" and self.settings.effort_routing:
                present = _present_efforts(request)
                if present:
                    ceiling = max((r for r in map(_effort_rank, present.values()) if r is not None),
                                  default=None)
                    target_rank = _effort_rank(decision.effort)
                    if ceiling is not None and target_rank is not None:
                        target = _EFFORT_LADDER[min(target_rank, ceiling)]
                        effort_changed = _apply_effort(new_request, request, target)
                        effort_note = f"{'/'.join(sorted(set(present.values())))}->{target}"

            self._record_decision(rid, {
                "authorized": authorized, "task8": task8,
                "selected": chosen if model_changed else current,
            })
            outcome = "rewritten" if (model_changed or effort_changed) else "unchanged"
            logger.info(
                "model_router: route rid=%s sid=%s task=%s family=%s tier=%s auth=%d "
                "model=%s->%s effort=%s reasons=%s outcome=%s",
                rid or "-", sid8, task8, family, tier, int(authorized),
                current, chosen if model_changed else current, effort_note,
                decision.label, outcome,
            )
            if not (model_changed or effort_changed):
                return None
            return {
                "request": new_request,
                "source": "model-router",
                "reason": f"task:{tier}",
            }
        except Exception:
            logger.warning("model_router: complexity routing skipped (error)")
            return None
