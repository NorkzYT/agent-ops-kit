"""Tier tables and tier -> model resolution for the model-router.

Pure and side-effect-free. Classification of the current user task lives in
``task_intent``; this module owns the policy tables:

  * ``gpt`` (orchestrator, CLIProxyAPI): status/lookup -> luna (low effort),
    ordinary execution -> luna (medium), ambiguous/multi-constraint planning ->
    terra, genuinely hard work -> sol. ``gpt-6-astra`` is reachable ONLY with
    explicit per-task authorization (``allow_max=True``): the automatic budget
    ceiling is Sol regardless of what the provider advertises.
  * ``coding`` (delegation, claude-max-proxy): opus by default, fable only for
    genuinely difficult engineering. Reasoning effort comes from
    ``coding_effort``: medium for routine/ordinary code, high for complex code,
    xhigh only for unusually hard debugging/architecture, max only for an
    explicitly authorized (``#max``/``#astra``) task. claude-max-proxy maps the
    label to the Claude CLI thinking budget.

Model selection (which model) and reasoning effort (how hard it thinks) are
decided independently; ``TIER_EFFORT`` is the default GPT effort per tier and
the router clamps it to whatever the request already carried. Coding effort is
clamped to its own ceiling (``coding_effort_ceiling``) instead, because the
orchestrator ceiling (``agent.reasoning_effort: high``) would rule out xhigh.
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
    "elevated": "gpt-6.1-terra",
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

# Coding-family effort per tier (claude-max-proxy: medium 10k, high 32k,
# xhigh 48k, max 64k thinking tokens). ``max`` is reachable only through an
# authorized max tier; xhigh is the automatic top, see ``coding_effort``.
CODING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODING_TIER_EFFORT = {
    "routine": "medium",
    "medium": "medium",
    "elevated": "medium",
    "high": "high",
    "max": "max",
}
# "Unusually hard debugging/architecture" gets xhigh: a hard-tier task with
# strong signals (classifier effort ``high``) that names one of these
# categories AND either a second hard category or an explicit #deep/#hard hint.
# One keyword plus length/code alone stays at high.
CODING_XHIGH_LABELS = frozenset({"kw:debug", "kw:arch", "kw:concurrency"})

_ALIAS_TO_TIER = {
    # gpt aliases
    "luna": "routine", "gpt-5.6-luna": "routine",
    "terra": "elevated", "gpt-6.1-terra": "elevated", "gpt-5.6-terra": "elevated",
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


def coding_effort(tier: str, effort: str, reasons: Iterable[str], *, ceiling: str = "max") -> str:
    """Reasoning effort for a coding-family request, clamped to ``ceiling``.

    ``tier`` is the effective (post-authorization) tier, ``effort`` and
    ``reasons`` the classifier's GPT effort and labels for the same task.
    """
    reasons = tuple(reasons)
    target = CODING_TIER_EFFORT.get(tier, "medium")
    categories = {r for r in reasons if r.startswith("kw:")}
    if (tier == "high" and effort == "high" and categories & CODING_XHIGH_LABELS
            and (len(categories) >= 2 or "tag:hard" in reasons)):
        target = "xhigh"
    if ceiling in CODING_EFFORTS and CODING_EFFORTS.index(target) > CODING_EFFORTS.index(ceiling):
        target = ceiling
    return target


def alias_to_tier(name: str) -> Optional[str]:
    """Map a model id or short alias to a tier, or None if unknown."""
    if not name:
        return None
    return _ALIAS_TO_TIER.get(name.strip().lower())


def _available_lower(available: Iterable[str]) -> set:
    return {a.strip().lower() for a in available if isinstance(a, str) and a.strip()}


# Coding models a leading #opus/#fable control may force.
FORCED_CODING_MODELS = frozenset(CODING_TIER_MODELS.values())


def resolve_forced_coding(model_alias: str, *, available: Optional[Sequence[str]] = None) -> Optional[str]:
    """Resolve a forced coding control (``#opus``/``#fable``) to a concrete model id.

    Unlike :func:`resolve_model` this never crosses families or silently degrades
    to a *different* model — the control names the model. Returns the alias when
    the live set is unknown (trust config) or contains it, and ``None`` when a
    live ``/v1/models`` probe proves it absent (the caller then keeps the request
    on its configured model and lets normal error/fallback handling apply). Only
    ``opus``/``fable`` are valid controls; anything else returns ``None``.
    """
    alias = (model_alias or "").strip().lower()
    if alias not in FORCED_CODING_MODELS:
        return None
    if available is None:
        return alias
    return alias if alias in _available_lower(available) else None


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
