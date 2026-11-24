"""Tier tables and tier -> model resolution for the model-router.

Pure and side-effect-free. Classification of the current user task lives in
``task_intent``; this module owns the policy tables:

  * ``gpt`` (orchestrator, CLIProxyAPI): every managed GPT call is version
    6.1 or newer and family Terra or higher (``GPT_MIN_VERSION`` /
    ``GPT_ELIGIBLE_FAMILIES``). Status/lookup and ordinary work -> Terra
    (``gpt-6.1-terra``; status keeps its low effort), ambiguous/multi-constraint
    planning -> Terra, genuinely hard work (or a leading ``#sol`` control) ->
    the newest eligible Sol. The newest eligible Astra is reachable ONLY with
    explicit per-task authorization (``allow_max=True``): the automatic budget
    ceiling is Sol regardless of what the provider advertises. There is no
    cross-tier or older-version fallback: a tier whose eligible model is not
    advertised resolves to ``None`` and the request is refused, never sent on
    a lower (or silently higher) model.
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

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

# Ordered low -> high. The index is the degrade walk order.
TIERS = ("routine", "medium", "elevated", "high", "max")

# Model floor for every kit-managed GPT call: version >= 6.1 and family Terra
# or higher. Luna, anything older than 6.1 (``gpt-5.6-*``, ``gpt-6-astra`` =
# 6.0) and any id that is not a strict family id are never selected.
GPT_MIN_VERSION = (6, 1)
GPT_ELIGIBLE_FAMILIES = ("terra", "sol", "astra")

# Terra = balanced intelligence/cost (routine and ordinary work), Sol =
# flagship complex work, Astra = hardest end-to-end work (explicit opt-in
# only). Used as-is when the live set is unknown; with a live set the
# ``high``/``max`` tiers pick the newest eligible Sol/Astra advertised.
GPT_TIER_MODELS = {
    "routine": "gpt-6.1-terra",
    "medium": "gpt-6.1-terra",
    "elevated": "gpt-6.1-terra",
    "high": "gpt-6.2-sol",
    "max": "gpt-6.2-astra",
}

# Strict GPT model id: ``gpt-<major>[.<minor>]-<family>``, lowercase ASCII, no
# leading zeros, at most three digits per component, nothing before or after.
# Anything else (case variants, whitespace, suffixes like ``-preview``,
# ``openai/`` prefixes, three-part versions, ``gpt-6-1-terra``) is not a family
# model.
_GPT_ID_RE = re.compile(r"gpt-(0|[1-9][0-9]{0,2})(?:\.(0|[1-9][0-9]{0,2}))?-(luna|terra|sol|astra)")
_FAMILY_TIER = {"terra": "elevated", "sol": "high", "astra": "max"}
# Names an OpenAI GPT model anywhere in one id segment, any case, so case
# variants, deployment wrappers and unknown aliases are caught, not ignored:
# ``gpt`` followed by a version digit or by a family/alias word. Refused:
# ``azure-gpt-4o``, ``my_gpt-5_deploy``, ``gpt5``, ``chatgpt-4o-latest`` (refused),
# ``gpt-terra``, ``gpt-latest`` (refused). Local names that
# merely start with "gpt" (``gpt4all``, ``gpt-oss:20b``, ``gpt-j``, ``gptq-*``)
# do not match.
_GPT_LIKE_RE = re.compile(
    r"gpt[-_. ]?[0-9](?!all)|gpt[-_](?:luna|terra|sol|astra|latest|turbo|default|mini|nano|pro)(?![a-z])",
    re.IGNORECASE)
# Routing wrappers in front of the id: one or more provider segments joined by
# ``/`` or ``:`` (``openai/``, ``openrouter/openai/``, ``azure:``). The id is
# kept as written; only the last segment is classified, and only when every
# wrapper segment is a plain name. Nothing is inferred about what the wrapped
# provider or proxy actually serves.
_SEGMENT_SPLIT_RE = re.compile(r"[/:]")
_WRAPPER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_BELOW_FLOOR_ALIASES = frozenset({"luna"})


# Tiers resolved to the NEWEST eligible strictly-parsed model of their family
# when the live set is known (``gpt-6.2-sol`` beats ``gpt-6.1-sol``;
# ``gpt-6.10-sol`` beats ``gpt-6.9-sol``; ``gpt-5.6-sol`` is never chosen). The tier itself — and the Astra authorization that
# gates ``max`` — is decided before this; the live set only picks the version.
NEWEST_FAMILY_TIERS = {"high": "sol", "max": "astra"}


def parse_family_model(model_id: object) -> Optional[Tuple[str, Tuple[int, int]]]:
    """``(family, (major, minor))`` for a strict GPT family id, else ``None``.

    A missing minor is ``0`` (``gpt-6-astra`` is version 6.0)."""
    if not isinstance(model_id, str):
        return None
    m = _GPT_ID_RE.fullmatch(model_id)
    if m is None:
        return None
    return m.group(3), (int(m.group(1)), int(m.group(2) or 0))


def gpt_model_eligible(model_id: object) -> bool:
    """True only for a strict GPT family id at or above the model floor:
    version >= ``GPT_MIN_VERSION`` and family in ``GPT_ELIGIBLE_FAMILIES``."""
    parsed = parse_family_model(model_id)
    return bool(parsed) and parsed[0] in GPT_ELIGIBLE_FAMILIES and parsed[1] >= GPT_MIN_VERSION


def gpt_floor_status(model_id: object) -> str:
    """``eligible`` | ``below`` | ``unknown`` | ``other`` — same rule as
    ``scripts/lib/model_floor.floor_status`` (kept in lockstep by
    scripts/tests/test_model_floor.py). An id behind plain provider wrappers
    (``openai/``, ``openrouter/openai/``, ``azure:``) is classified by its last
    segment and never rewritten; nothing is inferred about what that provider
    serves."""
    if not isinstance(model_id, str) or not model_id.strip():
        return "other"
    stripped = model_id.strip()
    if model_id != stripped:
        shaped = _GPT_LIKE_RE.search(stripped) or stripped.lower() in _BELOW_FLOOR_ALIASES
        return "unknown" if shaped else "other"
    if model_id.lower() in _BELOW_FLOOR_ALIASES:
        return "below"
    segments = _SEGMENT_SPLIT_RE.split(model_id)
    if not any(_GPT_LIKE_RE.search(s) for s in segments):
        return "other"
    *wrappers, bare = segments
    parsed = parse_family_model(bare)
    if parsed is None or not all(_WRAPPER_RE.fullmatch(w) and not _GPT_LIKE_RE.search(w) for w in wrappers):
        return "unknown"
    return "eligible" if parsed[0] in GPT_ELIGIBLE_FAMILIES and parsed[1] >= GPT_MIN_VERSION else "below"


def gpt_below_floor(model_id: object) -> bool:
    """True when ``model_id`` names a GPT model the floor refuses: older than
    6.1 or Luna (``below``), or not a strict family id (``unknown``: case
    variants and ids such as ``gpt-6-1-terra``/``gpt-4o`` are refused). Non-GPT ids (Claude aliases,
    local models such as ``gpt4all``/``gpt-oss:20b``) and eligible wrapped ids
    (``openai/gpt-6.1-terra``) are never below the floor."""
    return gpt_floor_status(model_id) in ("below", "unknown")


def newest_family_model(family: str, ids: Iterable[object]) -> Optional[str]:
    """The highest-version ELIGIBLE strict id of ``family`` in ``ids`` (numeric
    order), or ``None``. Equal versions break on the id string
    so the choice is deterministic."""
    best: Optional[Tuple[Tuple[int, int], str]] = None
    for mid in ids:
        parsed = parse_family_model(mid)
        if parsed is None or parsed[0] != family or not gpt_model_eligible(mid):
            continue
        key = (parsed[1], mid)  # type: ignore[assignment]
        if best is None or key[0] > best[0] or (key[0] == best[0] and key[1] < best[1]):
            best = key  # type: ignore[assignment]
    return best[1] if best else None


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
    # gpt aliases (eligible families only; full ids go through the floor check)
    "terra": "elevated", "sol": "high", "astra": "max",
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
    """Map a model id or short alias to a tier, or None if unknown.

    Besides the fixed aliases, any ELIGIBLE strict GPT family id maps to its
    family's tier (``gpt-7-astra`` -> ``max``), so a newer Astra is still gated
    as max-tier at the execution seam. Below-floor ids (``gpt-5.6-luna``,
    ``gpt-6-astra``) are not managed: the execution seam refuses them."""
    if not name or not isinstance(name, str):
        return None
    tier = _ALIAS_TO_TIER.get(name.strip().lower())
    if tier is not None:
        return tier
    if not gpt_model_eligible(name):
        return None
    return _FAMILY_TIER[parse_family_model(name)[0]]


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
    """Resolve a tier to a concrete model id.

    ``available`` is the live model id set from a ``/v1/models`` probe. Without
    ``allow_max`` the ``max`` tier is capped at ``high`` (the automatic Sol
    ceiling), so nothing here upgrades a request to Astra.

    GPT family: no fallback of any kind. The Terra tiers need their exact
    ``GPT_TIER_MODELS`` id; ``high``/``max`` take the newest ELIGIBLE Sol/Astra
    advertised (``NEWEST_FAMILY_TIERS``). When the live set lacks it the result
    is ``None`` (the router refuses), never an older version, Luna, another
    tier or a below-floor id.

    Coding family (Claude, unchanged): degrade down the tier walk first, then
    up — never into ``max`` without ``allow_max``. Returns ``None`` when nothing
    allowed is available.
    """
    coding = family == "coding"
    table = CODING_TIER_MODELS if coding else GPT_TIER_MODELS
    if tier not in TIERS:
        tier = "routine"
    if not allow_max and tier == "max":
        tier = "high"
    avail = _available_lower(available) if available is not None else None
    # Exact ids as advertised (strict parsing rejects case/whitespace variants).
    live = [a for a in available if isinstance(a, str)] if available is not None else []

    if not coding:
        if avail is None:
            return table[tier]
        if tier in NEWEST_FAMILY_TIERS:
            return newest_family_model(NEWEST_FAMILY_TIERS[tier], live)
        primary = table[tier]
        return primary if primary in live else None

    start = TIERS.index(tier)

    def pick(t: str) -> Optional[str]:
        if avail is None:
            return table[t]
        return table[t] if table[t].lower() in avail else None

    for i in range(start, -1, -1):
        candidate = pick(TIERS[i])
        if candidate:
            return candidate
    # Nothing at or below the requested tier is available: step up, but never
    # into the max tier without explicit authorization.
    if avail is not None:
        for i in range(start + 1, len(TIERS)):
            if TIERS[i] == "max" and not allow_max:
                continue
            candidate = pick(TIERS[i])
            if candidate:
                return candidate
    return None
