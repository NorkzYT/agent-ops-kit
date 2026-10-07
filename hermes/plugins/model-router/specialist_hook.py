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
from typing import Any, Callable, List, Optional, Sequence

from . import specialists as sp
from .local_backend import LocalBackend, LocalUnavailable
from .router_config import RouterSettings

logger = logging.getLogger("hermes.plugin.model_router")

CLASSIFIER_TIMEOUT = 8.0


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
        private_terms=tuple(settings.private_terms),
        private_paths=tuple(settings.private_paths),
    )


def local_classifier(backend: LocalBackend) -> Optional[sp.Classifier]:
    """Ambiguity tie-breaker on the LOCAL Ollama model only (never cloud).

    ``route_text`` calls it only for non-private tasks with no deterministic
    match and filters the answer to the non-high-risk pool.
    """
    if not backend.configured:
        return None

    def classify(visible: str, pool: Sequence[str]) -> List[str]:
        prompt = (
            "Pick the specialist profiles whose domain this task needs. Answer with a "
            "comma-separated list of names from this list only, or 'none'.\n"
            f"Profiles: {', '.join(pool)}\n\nTask:\n{visible[:2000]}"
        )
        try:
            resp = backend.chat([{"role": "user", "content": prompt}])
        except LocalUnavailable:
            return []
        text = str(resp.choices[0].message.content or "").lower()
        return [t for t in re.split(r"[^a-z\-]+", text) if t in pool][:2]

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
                 session_private: Optional[Callable[[str], bool]] = None,
                 backend: Optional[LocalBackend] = None) -> None:
        self.settings = specialist_settings(settings)
        self.profile_name = profile_name or "default"
        self.orchestrators = tuple(settings.specialist_orchestrators or ("default",))
        self._session_private = session_private or (lambda _sid: False)
        self._classifier: Optional[sp.Classifier] = None
        if self.settings.classifier == "local":
            self._classifier = local_classifier(backend or LocalBackend(
                settings.ollama_base_url, settings.ollama_model, timeout=CLASSIFIER_TIMEOUT))

    @property
    def active(self) -> bool:
        return self.settings.enabled and self.profile_name in self.orchestrators

    def route(self, text: str, session_id: str = "") -> dict:
        return sp.route_text(text, self.settings,
                             session_private=self._session_private(session_id),
                             classifier=self._classifier)

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[dict]:
        if not self.active or kwargs.get("parent_session_id"):
            return None
        try:
            session_id = str(kwargs.get("session_id") or "")
            trace = self.route(_message_text(kwargs.get("user_message")), session_id)
            routes = trace.get("routes") or []
            logger.info(
                "model_router: specialists task=%s privacy=%s tier=%s source=%s routes=%s dropped=%s "
                "classifier=%s reason=%s",
                str(trace.get("task", "-"))[:8], trace.get("privacy"), trace.get("tier"),
                trace.get("task_source", "-"),
                ",".join(f"{r['profile']}:{r['execution']}:{r['knowledge']['status']}" for r in routes) or "-",
                ",".join(f"{d['profile']}:{d['reason']}" for d in trace.get("dropped") or []) or "-",
                trace.get("classifier"), trace.get("reason", "-"),
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
