"""Tier tables and tier -> model resolution for the model-router.

Pure and side-effect-free. Classification of the current user task lives in
``task_intent``; this module owns the policy tables:

  * ``gpt`` (orchestrator, CLIProxyAPI): status/lookup -> luna (low effort),
    ordinary execution -> luna (medium), ambiguous/multi-constraint planning ->
    terra, genuinely hard work -> sol. ``gpt-6-astra`` is reachable ONLY with
    explicit per-task authorization (``allow_max=True``): the automatic budget
    ceiling is Sol regardless of what the provider advertises.
  * ``coding`` (delegation, claude-max-proxy): opus by default, fable only for
    genuinely difficult engineering. Reasoning-effort fields are never touched
    for this family — claude-max-proxy owns that mapping.

Model selection (which model) and reasoning effort (how hard it thinks) are
decided independently; ``TIER_EFFORT`` is the default effort per tier and the
router clamps it to whatever the request already carried.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# Ordered low -> high. The index is the degrade walk order.
TIERS = ("routine", "medium", "elevated", "high", "max")

# Per OpenAI's GPT-5.6/6 line: Luna = fastest/lowest-cost, Terra = balanced
# intelligence/cost, Sol = flagship complex work, Astra (GPT-6) = hardest
# end-to-end work (explicit opt-in only).
GPT_TIER_MODELS = {
    "routine": "gpt-5.6-luna",
    "medium": "gpt-5.6-luna",
    "elevated": "gpt-5.6-terra",
    "high": "gpt-5.6-sol",
    "max": "gpt-6-astra",
}

# Coding family: opus is the default, fable the hard-engineering escalation.
CODING_TIER_MODELS = {
    "routine": "opus",
    "medium": "opus",
    "elevated": "opus",
    "high": "fable",
    "max": "fable",
}

# Default reasoning effort per tier (gpt family only; the router never raises
# a request's effort above what it already carried).
TIER_EFFORT = {
    "routine": "low",
    "medium": "medium",
    "elevated": "medium",
    "high": "medium",   # classifier raises to high on substantive complexity
    "max": "high",
}

_ALIAS_TO_TIER = {
    # gpt aliases
    "luna": "routine", "gpt-5.6-luna": "routine",
    "terra": "elevated", "gpt-5.6-terra": "elevated",
    "sol": "high", "gpt-5.6-sol": "high",
    "astra": "max", "gpt-6-astra": "max",
    # coding aliases
    "opus": "medium", "fable": "high",
}


@dataclass(frozen=True)
class ComplexityResult:
    tier: str
    score: int
    label: str  # detector labels, never content
    effort: str = "medium"


def score_request(request: object) -> ComplexityResult:
    """Classify a provider request (or a raw string) by its CURRENT user task.

    Compatibility shim over :mod:`task_intent`: stateless, so continuation
    retention does not apply here. The router calls ``task_intent`` directly.
    """
    from . import task_intent as ti

    task = ti.task_from_text(request) if isinstance(request, str) else ti.extract_current_user_task(request)
    decision = ti.classify(task)
    return ComplexityResult(decision.tier, decision.score, decision.label, decision.effort)


def alias_to_tier(name: str) -> Optional[str]:
    """Map a model id or short alias to a tier, or None if unknown."""
    if not name:
        return None
    return _ALIAS_TO_TIER.get(name.strip().lower())


def _available_lower(available: Iterable[str]) -> set:
    return {a.strip().lower() for a in available if isinstance(a, str) and a.strip()}


def resolve_model(
    tier: str,
    *,
    family: str = "gpt",
    available: Optional[Sequence[str]] = None,
    allow_max: bool = False,
) -> Optional[str]:
    """Resolve a tier to a concrete model id, degrading to an available tier.

    ``available`` is the live model id set from a ``/v1/models`` probe. The walk
    steps *down* (max->...->routine) first; only if nothing at or below the tier
    exists does it step up — and the ``max`` tier is excluded from that upward
    walk unless ``allow_max`` is set, so a missing lower model can never silently
    upgrade a request to Astra. Without ``allow_max`` the start tier itself is
    also capped at ``high`` (the automatic Sol ceiling). Returns ``None`` when
    nothing allowed is available (caller decides: keep, degrade, or refuse).
    """
    table = CODING_TIER_MODELS if family == "coding" else GPT_TIER_MODELS
    if tier not in TIERS:
        tier = "routine"
    if not allow_max and tier == "max":
        tier = "high"
    avail = _available_lower(available) if available is not None else None
    start = TIERS.index(tier)
    for i in range(start, -1, -1):
        candidate = table[TIERS[i]]
        if avail is None or candidate.lower() in avail:
            return candidate
    # Nothing at or below the requested tier is available: step up, but never
    # into the max tier without explicit authorization.
    if avail is not None:
        for i in range(start + 1, len(TIERS)):
            if TIERS[i] == "max" and not allow_max:
                continue
            candidate = table[TIERS[i]]
            if candidate.lower() in avail:
                return candidate
    return None
