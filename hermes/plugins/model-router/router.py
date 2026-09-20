"""Router: task-scoped complexity rewrite (llm_request) + fail-closed privacy
and Astra budget enforcement (llm_execution).

The privacy handler is the load-bearing security boundary. Errors while
classifying or routing private traffic must return a local result or refusal,
never fall through to cloud. Once a provider call is permitted, its exceptions
propagate unchanged instead of being mislabeled as privacy refusals.

Routing decisions come from the CURRENT user task only (``task_intent``), with
bounded per-session state (``task_state``) so a "continue" retains the running
task's tier while a status question routes cheap. Every managed decision — even
an unchanged one — is logged with opaque hashes, tier/reason labels and
transport outcomes; never prompt content, secrets, or raw provider errors.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Set

from . import complexity as cx
from . import privacy as pv
from . import task_intent as ti
from .local_backend import (
    LocalBackend,
    LocalUnavailable,
    messages_from_request,
    refusal_response,
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
    def __init__(self, settings: RouterSettings, *, backend: Optional[LocalBackend] = None) -> None:
        self.settings = settings
        self._backend = backend if backend is not None else LocalBackend(
            settings.ollama_base_url, settings.ollama_model, timeout=settings.ollama_timeout
        )
        self._private_sessions: Set[str] = set()
        self._tasks = TaskStateStore(ttl=settings.task_ttl)
        # Request-stage decisions keyed by api_request_id so the execution seam
        # can enforce Astra authorization and correlate outcomes without
        # re-deriving (or logging) anything from the payload. Bounded.
        self._decisions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ---------------------------------------------------------------- privacy
    def is_private(self, request: Any, session_id: str) -> pv.PrivacyDecision:
        """Sticky, fail-closed privacy decision. Any internal error => private."""
        try:
            if session_id and session_id in self._private_sessions:
                return pv.PrivacyDecision(True, ("session-sticky",))
            decision = pv.scan_request(
                request,
                private_terms=self.settings.private_terms,
                private_paths=self.settings.private_paths,
            )
            if decision.private and session_id:
                self._private_sessions.add(session_id)  # keep the session local
            return decision
        except Exception:  # never let classification failure fall open
            logger.warning("model_router: privacy scan errored; treating as private")
            if session_id:
                self._private_sessions.add(session_id)
            return pv.PrivacyDecision(True, ("classifier-error",))

    def _local_or_refuse(self, request: Any, reason: str) -> Any:
        try:
            resp = self._backend.chat(messages_from_request(request))
            logger.info("model_router: private->local ok reason=%s", reason)
            return resp
        except LocalUnavailable:
            logger.warning("model_router: private->refuse (local unavailable) reason=%s", reason)
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
        return decision.authorized_max

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
                    return self._local_or_refuse(request, decision.label)
        except Exception:  # privacy failures must never fall open to cloud
            logger.warning("model_router: llm_execution guard tripped; refusing")
            return refusal_response(reason="router-guard")

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
        self._log_outcome(rid, session_id, kwargs, "completed", response=response)
        return response

    def _log_outcome(
        self, rid: str, session_id: str, kwargs: Dict[str, Any], status: str,
        *, response: Any = None, error: str = "",
    ) -> None:
        """Completion/outcome telemetry. ``served`` comes only from actual
        response evidence (the provider-reported ``model`` attribute); a
        selected/requested model is never presented as the model executed."""
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
                "model_router: outcome rid=%s sid=%s task=%s status=%s requested=%s served=%s usage=%s%s",
                rid or "-", _hash8(session_id), entry.get("task8", "-"), status, requested,
                served if isinstance(served, str) else "unknown",
                _usage_summary(response) if response is not None else "unavailable",
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

    def on_llm_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Rewrite model and (gpt family) reasoning effort for the current task."""
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
            # Bounded task state: a continuation refreshes the running task, a
            # new substantive request replaces it (authorization never carries
            # over), and a status question leaves it untouched.
            if decision.continuation and prev is not None:
                self._tasks.touch(session_id)
            elif decision.substantive and task.found:
                self._tasks.put(session_id, TaskState(
                    task.task_hash, decision.tier, decision.effort, decision.authorized_max,
                ))

            if self.settings.astra_policy == "always":
                authorized = True
            elif self.settings.astra_policy == "never":
                authorized = False
            else:
                authorized = decision.authorized_max
            tier = decision.tier
            if tier == "max" and not authorized:
                tier = "high"  # automatic ceiling: Sol

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

            # Effort is selected independently of the model, only for the gpt
            # family (claude-max-proxy owns coding-effort semantics), and only
            # by rewriting fields the request already carries — clamped so the
            # router never raises effort above what was configured.
            effort_note = "-"
            effort_changed = False
            if family == "gpt" and self.settings.effort_routing:
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
