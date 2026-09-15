"""Deterministic complexity scoring and tier -> model resolution.

Pure and side-effect-free. Two model families are supported:

  * ``gpt`` (orchestrator, CLIProxyAPI): routine->luna, medium->terra,
    high->sol, max->astra.
  * ``coding`` (delegation, claude-max-proxy): opus by default, fable for
    high-complexity work.

Scoring never sees the network and never logs content — callers log only the
returned label and integer score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

# Ordered low -> high. The index is the degrade walk order.
TIERS = ("routine", "medium", "high", "max")

# Per OpenAI's GPT-5.6/6 line: Luna = fastest/lowest-cost (routine floor),
# Terra = balanced intelligence/cost (mid), Sol = flagship complex work (high),
# Astra (GPT-6) = hardest end-to-end work (max).
GPT_TIER_MODELS = {
    "routine": "gpt-5.6-luna",
    "medium": "gpt-5.6-terra",
    "high": "gpt-5.6-sol",
    "max": "gpt-6-astra",
}

# Coding family: opus is the floor, fable is the high/max escalation.
CODING_TIER_MODELS = {
    "routine": "opus",
    "medium": "opus",
    "high": "fable",
    "max": "fable",
}

_ALIAS_TO_TIER = {
    # gpt aliases
    "luna": "routine", "gpt-5.6-luna": "routine",
    "terra": "medium", "gpt-5.6-terra": "medium",
    "sol": "high", "gpt-5.6-sol": "high",
    "astra": "max", "gpt-6-astra": "max",
    # coding aliases
    "opus": "medium", "fable": "high",
}

_FORCE_MAX_RE = re.compile(r"(?<![A-Za-z0-9_])#(?:max|deep|hard)\b", re.IGNORECASE)
_FORCE_MIN_RE = re.compile(r"(?<![A-Za-z0-9_])#(?:quick|routine|cheap|simple)\b", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```")
_HIGH_KEYWORDS = re.compile(
    r"(?i)\b(architect(?:ure)?|refactor|migrat(?:e|ion)|concurren(?:t|cy)|distribut(?:ed|ion)|"
    r"threat model|security (?:review|audit)|race condition|deadlock|schema (?:design|migration)|"
    r"rewrite|redesign|end[- ]to[- ]end)\b"
)
_MED_KEYWORDS = re.compile(
    r"(?i)\b(multi[- ]file|across the (?:code)?base|several files|integration|debug|optimi[sz]e|"
    r"implement|design|test suite)\b"
)


@dataclass(frozen=True)
class ComplexityResult:
    tier: str
    score: int
    label: str  # detector labels, never content


def _latest_user_text(request: object) -> str:
    """Best-effort newest user turn; falls back to all text joined."""
    from .privacy import extract_texts  # local import keeps this module leaf-safe

    texts = extract_texts(request) if not isinstance(request, str) else [request]
    return texts[-1] if texts else ""


def score_request(request: object) -> ComplexityResult:
    """Score a provider request (or a raw string) into a tier."""
    from .privacy import extract_texts

    if isinstance(request, str):
        corpus = request
        msg_count = 1
    else:
        texts = extract_texts(request)
        corpus = "\n".join(texts)
        msgs = request.get("messages") if isinstance(request, dict) else None
        msg_count = len(msgs) if isinstance(msgs, (list, tuple)) else len(texts)

    if _FORCE_MAX_RE.search(corpus):
        return ComplexityResult("max", 99, "forced:max")
    if _FORCE_MIN_RE.search(corpus):
        return ComplexityResult("routine", 0, "forced:routine")

    labels: List[str] = []
    score = 0
    n = len(corpus)
    if n > 4000:
        score += 2; labels.append("len>4k")
    elif n > 1200:
        score += 1; labels.append("len>1k")
    if _CODE_FENCE_RE.search(corpus):
        score += 1; labels.append("code")
    if _HIGH_KEYWORDS.search(corpus):
        score += 3; labels.append("kw:high")
    if _MED_KEYWORDS.search(corpus):
        score += 1; labels.append("kw:med")
    if msg_count > 20:
        score += 1; labels.append("turns>20")

    if score >= 5:
        tier = "max"
    elif score >= 3:
        tier = "high"
    elif score >= 1:
        tier = "medium"
    else:
        tier = "routine"
    return ComplexityResult(tier, score, ",".join(labels) or "none")


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
) -> Optional[str]:
    """Resolve a tier to a concrete model id, degrading to an available tier.

    ``available`` is the live model id set from a ``/v1/models`` probe. The walk
    only ever steps *down* (max->high->medium->routine); it never upgrades. If
    nothing in the family is available it returns ``None`` (caller decides).
    """
    table = CODING_TIER_MODELS if family == "coding" else GPT_TIER_MODELS
    if tier not in TIERS:
        tier = "routine"
    avail = _available_lower(available) if available is not None else None
    start = TIERS.index(tier)
    for i in range(start, -1, -1):
        candidate = table[TIERS[i]]
        if avail is None or candidate.lower() in avail:
            return candidate
    # Nothing at or below the requested tier is available.
    if avail is not None:
        for i in range(start + 1, len(TIERS)):
            candidate = table[TIERS[i]]
            if candidate.lower() in avail:
                return candidate
    return None
