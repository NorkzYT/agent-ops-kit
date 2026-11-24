#!/usr/bin/env python3
"""GPT model floor shared by the kit scripts (installer, stack init, knowledge
tools, route smoke, migration).

Every kit-managed GPT call uses version 6.1 or newer, family Terra or higher.
The model-router plugin carries its own copy of this rule
(``hermes/plugins/model-router/complexity.py``: it is installed on its own, so
it cannot import from here); ``scripts/tests/test_model_floor.py`` keeps the
two in lockstep.

CLI (stdlib only, prints key names and model ids, never other values):

    model_floor.py check KEY=VALUE ...   exit 1 and name every KEY whose value
                                         is a below-floor GPT id
    model_floor.py eligible MODEL        exit 0 when MODEL is an eligible id
"""

from __future__ import annotations

import re
import sys
from typing import Iterable, List, Optional, Tuple

MIN_VERSION = (6, 1)
ELIGIBLE_FAMILIES = ("terra", "sol", "astra")

# Canonical defaults. Routine, ordinary and default/named-profile work starts
# on Terra; the router alone moves a task to the newest eligible Sol (hard) or
# Astra (explicit #max/#astra only). Nothing here upgrades to Astra.
DEFAULT_MODEL = "gpt-6.1-terra"
STATIC_SOL = "gpt-6.2-sol"
STATIC_ASTRA = "gpt-6.2-astra"

_ID_RE = re.compile(r"gpt-(0|[1-9][0-9]{0,2})(?:\.(0|[1-9][0-9]{0,2}))?-(luna|terra|sol|astra)")
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


def parse(model_id: object) -> Optional[Tuple[str, Tuple[int, int]]]:
    """``(family, (major, minor))`` for a strict GPT family id, else ``None``."""
    if not isinstance(model_id, str):
        return None
    m = _ID_RE.fullmatch(model_id)
    if m is None:
        return None
    return m.group(3), (int(m.group(1)), int(m.group(2) or 0))


def eligible(model_id: object) -> bool:
    """A canonical (unprefixed) family id at or above the floor: the ids the
    kit itself writes and the router selects."""
    parsed = parse(model_id)
    return bool(parsed) and parsed[0] in ELIGIBLE_FAMILIES and parsed[1] >= MIN_VERSION


def provider_split(model_id: str) -> Tuple[str, str]:
    """``(wrapper, id)`` split at the last ``/`` or ``:`` (``openai/x`` ->
    ``("openai", "x")``, ``openrouter/openai/x`` -> ``("openrouter/openai",
    "x")``), else ``("", model_id)``."""
    parts = _SEGMENT_SPLIT_RE.split(model_id)
    if len(parts) == 1:
        return "", model_id
    return model_id[: len(model_id) - len(parts[-1]) - 1], parts[-1]


def floor_status(model_id: object) -> str:
    """Classify a configured model id without changing it.

    ``eligible``  a GPT family id at or above the floor, bare or behind plain
                  provider wrappers (``openai/gpt-6.1-terra``,
                  ``openrouter/openai/gpt-6.2-sol``, ``azure:gpt-6.1-terra``);
                  says nothing about whether that provider serves it;
    ``below``     a strictly parsed GPT id older than 6.1 or Luna, bare or
                  wrapped (or the bare ``luna`` alias, any case);
    ``unknown``   names a GPT model but is not a strict family id behind plain
                  wrappers: unclear, so never rewritten (case variants,
                  suffixes, padding; refused: ``gpt-4o``, ``gpt-6-1-terra``;
                  refused: ``azure-gpt-4o``, ``gpt-latest``, ``gpt-6.1-terra:latest``);
    ``other``     not an OpenAI GPT id (Claude, local Ollama such as
                  ``gpt4all``/``gpt-oss:20b``/``ollama/luna``, empty): outside the GPT floor.
    """
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
    parsed = parse(bare)
    if parsed is None or not all(_WRAPPER_RE.fullmatch(w) and not _GPT_LIKE_RE.search(w) for w in wrappers):
        return "unknown"
    return "eligible" if parsed[0] in ELIGIBLE_FAMILIES and parsed[1] >= MIN_VERSION else "below"


def below_floor(model_id: object) -> bool:
    """True for a value the floor refuses: ``below`` or ``unknown``. Claude
    aliases, local ids and empty values are never below the GPT floor."""
    return floor_status(model_id) in ("below", "unknown")


def split_list(raw: str) -> List[str]:
    return [p.strip() for p in re.split(r"[,\s]+", raw or "") if p.strip()]


def check_pairs(pairs: Iterable[str]) -> List[str]:
    """``KEY=VALUE`` items whose value (or any comma-separated item) is below
    the floor, as ``KEY=<id>`` strings."""
    bad = []
    for pair in pairs:
        key, _, value = pair.partition("=")
        for item in split_list(value) or [value]:
            if below_floor(item):
                bad.append(f"{key}={item.strip()}")
    return bad


def main(argv: List[str]) -> int:
    if len(argv) >= 1 and argv[0] == "check":
        bad = check_pairs(argv[1:])
        for item in bad:
            print(f"below the GPT model floor (6.1+, Terra or higher) or not a recognised GPT id: {item}", file=sys.stderr)
        return 1 if bad else 0
    if len(argv) == 2 and argv[0] == "eligible":
        return 0 if eligible(argv[1]) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
