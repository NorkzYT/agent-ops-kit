"""model-router — complexity-based model selection + fail-closed privacy routing.

Registers two middleware callbacks with Hermes:

  * ``llm_request``   — deterministic complexity rewrite (luna/terra/sol/astra
    for the orchestrator; opus/fable for delegated coding).
  * ``llm_execution`` — fail-closed privacy guard. Requests carrying private
    data (SSN, PAN, ABA, IBAN, secrets, credentials, ``#private``/``#local``,
    or configured terms/paths) never reach a cloud provider: they are answered
    by a local Ollama model or refused. It never raises, so Hermes' fail-open
    middleware behavior can never route private data to the cloud.

See ``docs/model-routing.md`` in the kit for configuration and readiness.
"""

from __future__ import annotations

import logging

from .router import Router
from .router_config import load_settings

logger = logging.getLogger("hermes.plugin.model_router")


def register(ctx) -> None:
    settings = load_settings(getattr(ctx, "get_config", None))
    router = Router(settings)
    ctx.register_middleware("llm_request", router.on_llm_request)
    ctx.register_middleware("llm_execution", router.on_llm_execution)
    logger.info(
        "model_router registered (privacy=%s complexity=%s local=%s)",
        settings.privacy_routing,
        settings.complexity_routing,
        "configured" if (settings.ollama_base_url and settings.ollama_model) else "unset",
    )


__all__ = ["register", "Router", "load_settings"]
