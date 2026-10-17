"""model-router — task-scoped model/effort selection + fail-closed privacy routing.

Registers two middleware callbacks with Hermes:

  * ``llm_request``   — deterministic rewrite of model and reasoning effort
    from the CURRENT user task only (luna/terra/sol for the orchestrator,
    gpt-6-astra only with explicit per-task authorization; opus/fable for
    delegated coding with task-based Claude effort medium/high/xhigh, max
    only when authorized).
  * ``llm_execution`` — fail-closed privacy guard plus the Astra budget seam.
    A request whose current user turn carries a sender-authored ``#privacy``
    marker never reaches a cloud provider: it is answered by a local Ollama
    model or refused. The marker is per request (no session stickiness, no
    content detection); earlier marked exchanges are withheld from later
    public requests. It never raises, so Hermes' fail-open middleware behavior
    can never route a marked request to the cloud. Unauthorized max-tier
    (Astra) requests are refused with a typed response instead of being sent.

Optionally (``specialist_routing: true``, off by default) a ``pre_llm_call``
hook adds a deterministic specialist-routing note to the orchestrator's
current turn, and ``/route-specialists <task>`` prints a dry-run trace. See
``docs/specialist-routing.md``.

At load it also extends Hermes' auxiliary pre-egress guard (``aux_guard``):
CLIProxyAPI / claude-max-proxy count as cloud even on loopback, and a
``#privacy`` / ``#private`` payload is never sent to them (compression,
title generation).

See ``docs/model-routing.md`` in the kit for configuration and readiness.
"""

from __future__ import annotations

import logging

from . import aux_guard
from .router import Router
from .router_config import load_settings
from .specialist_hook import SpecialistHook

logger = logging.getLogger("hermes.plugin.model_router")


def register(ctx) -> None:
    settings = load_settings(getattr(ctx, "get_config", None))
    router = Router(settings)
    ctx.register_middleware("llm_request", router.on_llm_request)
    ctx.register_middleware("llm_execution", router.on_llm_execution)
    aux_guard.install(aux_guard.proxy_urls(settings))
    profile = str(getattr(ctx, "profile_name", "") or "default")
    specialists = SpecialistHook(settings, profile_name=profile)
    if specialists.active:
        ctx.register_hook("pre_llm_call", specialists.on_pre_llm_call)
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command("route-specialists", specialists.dry_run,
                         description="Dry-run the specialist router for a task (nothing dispatched)",
                         args_hint="<task>")
    if settings.private_terms or settings.private_paths:
        logger.warning("model_router: private_terms/private_paths are ignored by request routing; "
                       "only a #privacy marker in the current turn keeps a request local")
    logger.info(
        "model_router registered (privacy=%s complexity=%s local=%s specialists=%s) "
        "local_models=routine:%s,fallback:%s,agent:%s classifier=%s coding_effort=%s",
        settings.privacy_routing,
        settings.complexity_routing,
        "configured" if (settings.ollama_base_url and settings.local_chain("routine")) else "unset",
        "on" if specialists.active else "off",
        settings.local_model("routine") or "-",
        settings.local_model("fallback") or "-",
        settings.local_model("agent") or "-",
        (f"cliproxy:{settings.specialist_classifier_model}/{settings.specialist_classifier_effort}"
         if settings.specialist_classifier == "cliproxy" else settings.specialist_classifier)
        if specialists.classifier_ready or settings.specialist_classifier == "none"
        else f"{settings.specialist_classifier}-unavailable",
        f"auto/{settings.coding_effort_ceiling}" if settings.coding_effort_routing else "off",
    )


__all__ = ["register", "Router", "load_settings"]
