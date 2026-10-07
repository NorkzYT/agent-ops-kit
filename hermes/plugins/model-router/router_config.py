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


def _int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _seconds(raw: Any, default: float, cap: float = 25.0) -> float:
    """A positive timeout, capped below Hermes' 30 s pre_llm_call hook limit."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(cap, value) if value > 0 else default


def _astra_policy(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in ("task", "always", "never") else "task"


@dataclass
class RouterSettings:
    # Privacy is always on and fail-closed; this only disables the complexity
    # rewrite, never the privacy guard.
    complexity_routing: bool = True
    privacy_routing: bool = True
    # Rewrite the reasoning-effort fields already present on managed GPT
    # requests (never invented, never raised above what the request carried).
    effort_routing: bool = True
    # Astra (max tier) policy: "task" = only a leading standalone #max/#astra
    # control line in the CURRENT user task authorizes it; "always" = legacy
    # behavior (any managed max-tier request allowed); "never" = hard off.
    astra_policy: str = "task"
    # Sliding TTL (seconds) for per-session task state (continuation retention
    # and per-task Astra authorization). Absolute cap is 4x this value.
    task_ttl: float = 1800.0
    # Local (Ollama) OpenAI-compatible endpoint used for private/aux work.
    ollama_base_url: str = ""
    ollama_model: str = ""          # legacy single model: the default for every unset role
    ollama_timeout: float = 60.0
    # Explicit local roles on the same endpoint (docs/model-routing.md, Local
    # model roles). routine: ordinary private turns + the specialist classifier;
    # agent: difficult private turns and private coding; fallback: tried once
    # when the role model fails on a model-level error. Empty = ollama_model
    # (routine/agent) or no fallback.
    ollama_routine_model: str = ""
    ollama_fallback_model: str = ""
    ollama_agent_model: str = ""
    # Operator-configured private signals.
    private_terms: List[str] = field(default_factory=list)
    private_paths: List[str] = field(default_factory=list)
    # Live-available model ids (kept in sync by the probe); empty = trust config.
    available_gpt_models: List[str] = field(default_factory=list)
    available_coding_models: List[str] = field(default_factory=list)
    # Specialist-router pilot (specialists.py). Off unless explicitly enabled;
    # when on it only ADDS a routing note to the orchestrator's current turn.
    specialist_routing: bool = False
    specialist_max_profiles: int = 4
    specialist_profiles: List[str] = field(default_factory=list)   # allowlist; empty = all
    specialist_require_ready: bool = False
    specialist_classifier: str = "none"                            # none | local (Ollama)
    specialist_classifier_timeout: float = 10.0                    # total seconds, both models
    specialist_orchestrators: List[str] = field(default_factory=lambda: ["default"])
    specialist_profiles_root: str = ""                             # '' = $HERMES_HOME/profiles


    def local_model(self, role: str) -> str:
        """The model for a local role (``routine`` | ``agent`` | ``fallback``)."""
        if role == "fallback":
            return self.ollama_fallback_model
        explicit = self.ollama_agent_model if role == "agent" else self.ollama_routine_model
        return explicit or self.ollama_model

    def local_chain(self, role: str) -> List[str]:
        """Bounded local chain for a role: the role model, then the fallback
        model, de-duplicated (at most two entries, same endpoint)."""
        return [m for m in dict.fromkeys((self.local_model(role), self.ollama_fallback_model)) if m]


def _classifier(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in ("none", "local") else "none"


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
        effort_routing=_truthy(val("effort_routing", "MODEL_ROUTER_EFFORT", None), True),
        astra_policy=_astra_policy(val("astra_policy", "MODEL_ROUTER_ASTRA", "task")),
        task_ttl=float(val("task_ttl_seconds", "MODEL_ROUTER_TASK_TTL", 1800.0) or 1800.0),
        ollama_base_url=str(val("ollama_base_url", "OLLAMA_BASE_URL", "") or "").rstrip("/"),
        ollama_model=str(val("ollama_model", "OLLAMA_CHAT_MODEL", "") or ""),
        ollama_timeout=float(val("ollama_timeout", "OLLAMA_TIMEOUT", 60.0) or 60.0),
        ollama_routine_model=str(val("ollama_routine_model", "OLLAMA_ROUTINE_MODEL", "") or "").strip(),
        ollama_fallback_model=str(val("ollama_fallback_model", "OLLAMA_FALLBACK_MODEL", "") or "").strip(),
        ollama_agent_model=str(val("ollama_agent_model", "OLLAMA_AGENT_MODEL", "") or "").strip(),
        private_terms=_split(val("private_terms", "MODEL_ROUTER_PRIVATE_TERMS", [])),
        private_paths=_split(val("private_paths", "MODEL_ROUTER_PRIVATE_PATHS", [])),
        available_gpt_models=_split(val("available_gpt_models", "MODEL_ROUTER_GPT_MODELS", [])),
        available_coding_models=_split(val("available_coding_models", "MODEL_ROUTER_CODING_MODELS", [])),
        specialist_routing=_truthy(val("specialist_routing", "MODEL_ROUTER_SPECIALISTS", None), False),
        specialist_max_profiles=_int(val("specialist_max_profiles", "MODEL_ROUTER_SPECIALIST_MAX", 4), 4),
        specialist_profiles=_split(val("specialist_profiles", "MODEL_ROUTER_SPECIALIST_PROFILES", [])),
        specialist_require_ready=_truthy(val("specialist_require_ready", "MODEL_ROUTER_SPECIALIST_REQUIRE_READY", None), False),
        specialist_classifier=_classifier(val("specialist_classifier", "MODEL_ROUTER_SPECIALIST_CLASSIFIER", "none")),
        specialist_classifier_timeout=_seconds(val("specialist_classifier_timeout",
                                                   "MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT", 10.0), 10.0),
        specialist_orchestrators=_split(val("specialist_orchestrators", "MODEL_ROUTER_SPECIALIST_ORCHESTRATORS", ["default"])) or ["default"],
        specialist_profiles_root=str(val("specialist_profiles_root", "MODEL_ROUTER_SPECIALIST_PROFILES_ROOT", "") or ""),
    )
