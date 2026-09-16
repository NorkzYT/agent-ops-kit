"""Settings resolution for the model-router plugin.

Settings come from ``plugins.entries.model-router.settings`` (read via
``ctx.get_config``) with environment-variable fallbacks so the plugin also works
when driven from ``.env`` alone. Secrets are never stored here — only routing
knobs and the local endpoint URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


def _split(raw: Any) -> List[str]:
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    return []


def _truthy(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class RouterSettings:
    # Privacy is always on and fail-closed; this only disables the complexity
    # rewrite, never the privacy guard.
    complexity_routing: bool = True
    privacy_routing: bool = True
    # Local (Ollama) OpenAI-compatible endpoint used for private/aux work.
    ollama_base_url: str = ""
    ollama_model: str = ""
    ollama_timeout: float = 60.0
    # Operator-configured private signals.
    private_terms: List[str] = field(default_factory=list)
    private_paths: List[str] = field(default_factory=list)
    # Live-available model ids (kept in sync by the probe); empty = trust config.
    available_gpt_models: List[str] = field(default_factory=list)
    available_coding_models: List[str] = field(default_factory=list)


def load_settings(getter: Optional[Callable[[str, Any], Any]] = None) -> RouterSettings:
    """Build settings from a ``ctx.get_config``-style getter, with env fallback.

    ``getter(key, default)`` returns ``plugins.entries.model-router.settings.<key>``.
    A missing getter (unit tests, env-only installs) reads the environment.
    """
    def val(key: str, env: str, default: Any) -> Any:
        if getter is not None:
            got = getter(key, None)
            if got is not None:
                return got
        return os.environ.get(env, default)

    return RouterSettings(
        complexity_routing=_truthy(val("complexity_routing", "MODEL_ROUTER_COMPLEXITY", None), True),
        privacy_routing=_truthy(val("privacy_routing", "MODEL_ROUTER_PRIVACY", None), True),
        ollama_base_url=str(val("ollama_base_url", "OLLAMA_BASE_URL", "") or "").rstrip("/"),
        ollama_model=str(val("ollama_model", "OLLAMA_CHAT_MODEL", "") or ""),
        ollama_timeout=float(val("ollama_timeout", "OLLAMA_TIMEOUT", 60.0) or 60.0),
        private_terms=_split(val("private_terms", "MODEL_ROUTER_PRIVATE_TERMS", [])),
        private_paths=_split(val("private_paths", "MODEL_ROUTER_PRIVATE_PATHS", [])),
        available_gpt_models=_split(val("available_gpt_models", "MODEL_ROUTER_GPT_MODELS", [])),
        available_coding_models=_split(val("available_coding_models", "MODEL_ROUTER_CODING_MODELS", [])),
    )
