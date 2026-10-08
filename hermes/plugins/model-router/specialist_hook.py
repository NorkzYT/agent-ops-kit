"""Hermes glue for the specialist-router pilot (``specialists.py``).

* ``pre_llm_call`` hook — once per orchestrator turn, route the current user
  message and, when specialists match, return ``{"context": <note>}``. Hermes
  appends that note to the current user message (never the system prompt).
  The hook only suggests; the orchestrator dispatches through Kanban
  (``kanban_create`` with the profile as assignee, which runs under that
  profile's own HERMES_HOME) or a short bounded review. It never dispatches,
  never calls a cloud model, and returns nothing on any error.
* ``/route-specialists <task>`` — offline dry-run: prints the content-free
  routing trace (profiles, reasons, high-risk, knowledge readiness).

The hook runs only in the configured orchestrator profile(s) (``default``) and
never in a subagent turn, so a specialist never fans out to other specialists.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Sequence

from . import specialists as sp
from .local_backend import (
    BackendFactory,
    LocalBackend,
    LocalUnavailable,
    is_local_endpoint,
    run_local_chain,
)
from .router_config import RouterSettings

logger = logging.getLogger("hermes.plugin.model_router")

# Deterministic, short and without thinking: the answer is a few names.
CLASSIFIER_OPTIONS = {"temperature": 0, "max_tokens": 48, "reasoning_effort": "none"}
_THINK_RE = re.compile(r"(?s)<think>.*?</think>")


def specialist_settings(settings: RouterSettings) -> sp.SpecialistSettings:
    known, unknown = sp.split_allowed(settings.specialist_profiles)
    root = settings.specialist_profiles_root
    return sp.SpecialistSettings(
        enabled=settings.specialist_routing,
        max_profiles=sp.clamp_max(settings.specialist_max_profiles),
        allowed=known,
        unknown=unknown,
        require_ready=settings.specialist_require_ready,
        classifier=settings.specialist_classifier,
        profiles_root=Path(root).expanduser() if root else None,
    )


class Picks(list):
    """Classifier answer plus content-free metadata (model id, attempt, outcome)."""

    def __init__(self, names: Sequence[str] = (), meta: Optional[dict] = None) -> None:
        super().__init__(names)
        self.meta = meta or {}


def local_classifier(settings: RouterSettings, *,
                     factory: Optional[BackendFactory] = None) -> Optional[sp.Classifier]:
    """Ambiguity tie-breaker on the LOCAL Ollama endpoint only (never cloud).

    Models: the routine role (``ollama_routine_model``), then the fallback
    role once on a model-level failure, inside one total budget
    (``specialist_classifier_timeout``). ``route_text`` calls it only for
    public tasks with no or only weak keyword matches and keeps at most two
    non-high-risk answers. Any failure or timeout returns no names. Neither
    the prompt nor the answer is logged.
    """
    chain = settings.local_chain("routine")
    base_url = settings.ollama_base_url
    if not base_url or not chain:
        return None
    if not is_local_endpoint(base_url):
        logger.warning("model_router: specialist classifier disabled (endpoint not local)")
        return None
    budget = settings.specialist_classifier_timeout
    make = factory or (lambda model: LocalBackend(base_url, model, timeout=budget))

    def classify(visible: str, pool: Sequence[str]) -> List[str]:
        if not pool:
            return Picks([], {"model": "-", "attempt": 0, "outcome": "empty-pool"})
        prompt = (
            "Pick the specialist profiles whose domain this task needs. Answer with a "
            "comma-separated list of names from this list only, or 'none'.\n"
            f"Profiles: {', '.join(pool)}\n\nTask:\n{visible[:2000]}"
        )
        try:
            resp, model, attempt = run_local_chain(
                chain, [{"role": "user", "content": prompt}], make,
                budget=budget, options=CLASSIFIER_OPTIONS)
        except LocalUnavailable as exc:
            return Picks([], {"model": "-", "attempt": 0,
                              "outcome": f"unavailable:{getattr(exc, 'kind', 'error')}"})
        text = _THINK_RE.sub("", str(resp.choices[0].message.content or "")).lower()
        names = [t for t in dict.fromkeys(re.split(r"[^a-z\-]+", text)) if t in pool]
        return Picks(names[:sp.MAX_CLASSIFIER_ADDITIONS],
                     {"model": model, "attempt": attempt, "outcome": "ok"})

    return classify


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, (list, tuple)):
        parts = []
        for part in message:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                if part.get("type") in (None, "text", "input_text"):
                    parts.append(part["text"])
        return "\n".join(parts)
    return ""


class SpecialistHook:
    def __init__(self, settings: RouterSettings, *, profile_name: str = "default",
                 backend_factory: Optional[BackendFactory] = None) -> None:
        self.settings = specialist_settings(settings)
        self.profile_name = profile_name or "default"
        self.orchestrators = tuple(settings.specialist_orchestrators or ("default",))
        self._classifier: Optional[sp.Classifier] = None
        if self.settings.classifier == "local":
            self._classifier = local_classifier(settings, factory=backend_factory)

    @property
    def classifier_ready(self) -> bool:
        return self._classifier is not None

    @property
    def active(self) -> bool:
        return self.settings.enabled and self.profile_name in self.orchestrators

    def route(self, text: str) -> dict:
        return sp.route_text(text, self.settings, classifier=self._classifier)

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[dict]:
        if not self.active or kwargs.get("parent_session_id"):
            return None
        try:
            trace = self.route(_message_text(kwargs.get("user_message")))
            routes = trace.get("routes") or []
            logger.info(
                "model_router: specialists task=%s privacy=%s tier=%s source=%s routes=%s dropped=%s "
                "classifier=%s gate=%s classifier_model=%s classifier_outcome=%s reason=%s",
                str(trace.get("task", "-"))[:8], trace.get("privacy"), trace.get("tier"),
                trace.get("task_source", "-"),
                ",".join(f"{r['profile']}:{r['execution']}:{r['knowledge']['status']}" for r in routes) or "-",
                ",".join(f"{d['profile']}:{d['reason']}" for d in trace.get("dropped") or []) or "-",
                trace.get("classifier"), trace.get("classifier_gate", "-"),
                (trace.get("classifier_detail") or {}).get("model", "-"),
                (trace.get("classifier_detail") or {}).get("outcome", "-"),
                trace.get("reason", "-"),
            )
            plan = sp.render_plan(trace)
            return {"context": plan} if plan else None
        except Exception as exc:  # a suggestion is optional; never break the turn
            logger.warning("model_router: specialist routing skipped (%s)", type(exc).__name__)
            return None

    def dry_run(self, raw_args: str = "") -> str:
        """``/route-specialists <task>``: deterministic trace, nothing dispatched."""
        text = (raw_args or "").strip()
        if not text:
            return "usage: /route-specialists <task text>  (dry-run; nothing is dispatched)"
        trace = sp.route_text(text, self.settings, classifier=None)
        trace["active_in_this_profile"] = self.active
        trace["profile"] = self.profile_name
        return json.dumps(trace, indent=2, sort_keys=True)
