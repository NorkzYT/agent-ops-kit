"""Router: complexity rewrite (llm_request) + fail-closed privacy (llm_execution).

The privacy handler is the load-bearing security boundary. Hermes treats a
middleware that *raises* as a failure and falls open to the base cloud path, so
this handler must never raise: every path returns either a local result or a
refusal, and cloud (`next_call`) is invoked only for traffic proven public.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Set

from . import complexity as cx
from . import privacy as pv
from .local_backend import (
    LocalBackend,
    LocalUnavailable,
    messages_from_request,
    refusal_response,
)
from .router_config import RouterSettings

logger = logging.getLogger("hermes.plugin.model_router")


def _is_coding_family(request: Any, model: str, base_url: str) -> bool:
    """Delegation/coding calls go to claude-max-proxy; orchestrator to CLIProxy."""
    hay = f"{base_url or ''}".lower()
    if ":3456" in hay or "claude-max" in hay or "claude_max" in hay:
        return True
    m = (model or "").strip().lower()
    return m in ("opus", "fable", "sonnet", "haiku", "default", "best")


class Router:
    def __init__(self, settings: RouterSettings, *, backend: Optional[LocalBackend] = None) -> None:
        self.settings = settings
        self._backend = backend if backend is not None else LocalBackend(
            settings.ollama_base_url, settings.ollama_model, timeout=settings.ollama_timeout
        )
        self._private_sessions: Set[str] = set()

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

    def on_llm_execution(self, **kwargs: Any) -> Any:
        """Wrap the provider call. Fail-closed: never raises, never leaks private."""
        request = kwargs.get("request")
        next_call: Callable[[Any], Any] = kwargs.get("next_call")  # type: ignore[assignment]
        session_id = str(kwargs.get("session_id") or "")
        try:
            if not self.settings.privacy_routing:
                return next_call(request)
            decision = self.is_private(request, session_id)
            if not decision.private:
                return next_call(request)  # public -> cloud provider
            logger.info("model_router: private detected reasons=%s (no cloud egress)", decision.label)
            return self._local_or_refuse(request, decision.label)
        except Exception:  # last-resort guard: refuse rather than fall open to cloud
            logger.warning("model_router: llm_execution guard tripped; refusing")
            return refusal_response(reason="router-guard")

    # ------------------------------------------------------------- complexity
    def on_llm_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Rewrite the model by complexity within the request's model family."""
        if not self.settings.complexity_routing:
            return None
        try:
            request = kwargs.get("request")
            if not isinstance(request, dict):
                return None
            current = str(request.get("model") or kwargs.get("model") or "")
            base_url = str(kwargs.get("base_url") or "")
            # Only act on models we manage (known aliases); leave deliberate ids alone.
            if cx.alias_to_tier(current) is None:
                return None
            coding = _is_coding_family(request, current, base_url)
            family = "coding" if coding else "gpt"
            available = (self.settings.available_coding_models if coding
                         else self.settings.available_gpt_models)
            result = cx.score_request(request)
            chosen = cx.resolve_model(result.tier, family=family, available=available or None)
            if not chosen or chosen.strip().lower() == current.strip().lower():
                return None
            new_request = dict(request)
            new_request["model"] = chosen
            logger.info("model_router: complexity=%s score=%d family=%s %s->%s labels=%s",
                        result.tier, result.score, family, current, chosen, result.label)
            return {
                "request": new_request,
                "source": "model-router",
                "reason": f"complexity:{result.tier}",
            }
        except Exception:
            logger.warning("model_router: complexity routing skipped (error)")
            return None
