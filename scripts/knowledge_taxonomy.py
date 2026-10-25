#!/usr/bin/env python3
"""knowledge_taxonomy — deterministic consolidation of the classifier's new-profile
proposals into broad families, applied by knowledge-classify.py after parsing and
before the plan, report and review are written.

Policy (docs/knowledge-classify.md, "Taxonomy: broad parents first"):

* an existing broad profile that can own the book is preferred over a new one;
* a new profile is a broad parent-level family, never a narrow subcategory; the
  narrower subject becomes item ``tags`` and spec ``tags``/``aliases``;
* one proposal per family per run: overlapping names (``depth-psychology``,
  ``psychology-mental-health`` ...) fold into one canonical name and their
  sources are remapped to it;
* a canonical family that already exists as a profile is assigned to it (status
  stays ``needs-review``: the operator confirms a deterministic remap) instead of
  being proposed again; otherwise exactly one complete proposal is left for the
  operator. Nothing is created here.

The family map is explicit configuration: ``hermes/knowledge-taxonomy.json`` by
default, ``KNOWLEDGE_TAXONOMY=<path>`` to override (``KNOWLEDGE_TAXONOMY=off``
keeps only the structural rules). Resolution order for a proposed name:

  1. ``family-alias``     the name is listed as an alias of a family;
  2. ``existing-profile`` the name is ``<existing profile>-<child>``;
  3. ``family-prefix``    the name is ``<family>-<child>``;
  4. ``sibling-prefix``   the name is ``<another proposal in this run>-<child>``
                          (the shortest such root; roots resolve first);
  5. unchanged            an unknown family is preserved verbatim.

No review reason written here is a risk label; book text is never read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TAXONOMY_FILE = REPO / "hermes" / "knowledge-taxonomy.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CONSOLIDATED_REASON = "taxonomy-consolidated:"      # <proposed>-><canonical>  (still a proposal)
EXISTING_REASON = "taxonomy-existing-profile:"      # <proposed>-><existing profile>  (assigned for review)
NEW_PROFILE_REASON = "new-profile-proposal:"
MAX_TAGS = 12
MAX_TAG = 60
POLICY = ("prefer an existing broad profile; one broad parent-level family per run; narrower subjects are "
          "tags/aliases, never sibling profiles; nothing is created by consolidation")


class TaxonomyError(Exception):
    pass


@dataclass(frozen=True)
class Family:
    name: str
    aliases: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    parent: str = "none"


@dataclass
class FamilyResult:
    canonical: str
    existing: bool
    aliases: Tuple[str, ...] = ()
    sources: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    rules: Dict[str, str] = field(default_factory=dict)     # proposed name -> rule


@dataclass
class Consolidation:
    taxonomy_file: str = ""
    families: Dict[str, FamilyResult] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return bool(self.families)

    def summary(self) -> Dict[str, object]:
        return {"applied": self.applied, "taxonomy_file": self.taxonomy_file, "policy": POLICY,
                "families": {name: {"canonical": f.canonical, "existing": f.existing,
                                    "aliases": list(f.aliases), "sources": list(f.sources),
                                    "tags": list(f.tags), "rules": dict(f.rules)}
                             for name, f in sorted(self.families.items())}}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_taxonomy(path: Optional[Path] = None, missing_ok: bool = False) -> Dict[str, Family]:
    """The explicit family map; invalid files are an error, never a silent default."""
    path = Path(path or DEFAULT_TAXONOMY_FILE)
    if not path.is_file():
        if missing_ok:
            return {}
        raise TaxonomyError(f"taxonomy file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaxonomyError(f"{path}: not readable JSON: {exc}") from None
    fams_raw = data.get("families") if isinstance(data, dict) else None
    if not isinstance(fams_raw, dict):
        raise TaxonomyError(f"{path}: expected an object with a 'families' object")
    out: Dict[str, Family] = {}
    seen: Dict[str, str] = {}
    for name, spec in fams_raw.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise TaxonomyError(f"{path}: family name {name!r} must match {NAME_RE.pattern}")
        spec = spec if isinstance(spec, dict) else {}
        aliases = spec.get("aliases", [])
        tags = spec.get("tags", [])
        parent = spec.get("parent", "none")
        if not isinstance(aliases, list) or not all(isinstance(a, str) and NAME_RE.match(a) for a in aliases):
            raise TaxonomyError(f"{path}: family {name!r}: aliases must be a list of profile-style names")
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            raise TaxonomyError(f"{path}: family {name!r}: tags must be a list of non-empty strings")
        if not isinstance(parent, str) or not (parent == "none" or NAME_RE.match(parent)):
            raise TaxonomyError(f"{path}: family {name!r}: parent must be a profile name or 'none'")
        for a in aliases:
            if a == name:
                raise TaxonomyError(f"{path}: family {name!r} lists itself as an alias")
            if a in seen or a in fams_raw:
                raise TaxonomyError(f"{path}: alias {a!r} belongs to both {seen.get(a, a)!r} and {name!r}")
            seen[a] = name
        out[name] = Family(name, tuple(sorted(set(aliases))), tuple(_tag(t) for t in tags), parent or "none")
    return out


def _tag(text: str) -> str:
    return text.strip()[:MAX_TAG]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _child_of(name: str, roots: Iterable[str]) -> str:
    """The longest root ``r`` such that ``name`` starts with ``r-``; '' when none."""
    best = ""
    for r in roots:
        if r and not r.startswith("_") and name != r and name.startswith(r + "-") and len(r) > len(best):
            best = r
    return best


def canonical_for(name: str, families: Dict[str, Family], existing: Sequence[str],
                  proposed: Sequence[str]) -> Tuple[str, str]:
    """(canonical name, rule) for one proposed name; rule '' when it stays as it is."""
    for fam in families.values():
        if name in fam.aliases:
            return fam.name, "family-alias"
    root = _child_of(name, existing)
    if root:
        return root, "existing-profile"
    root = _child_of(name, families)
    if root:
        return root, "family-prefix"
    siblings = [p for p in proposed if p != name and name.startswith(p + "-") and not p.startswith("_")]
    if siblings:
        return min(siblings, key=len), "sibling-prefix"
    return name, ""


def subtopic(proposed: str, canonical: str) -> str:
    """The narrower subject a folded name carried: its tokens after the longest common dash-prefix
    with the canonical name (psychology-psychotherapy -> psychotherapy, blue-team-incident-response
    under blue-team-security -> incident-response), or the whole name (depth-psychology)."""
    a, b = proposed.split("-"), canonical.split("-")
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    rest = "-".join(a[n:])
    return rest or proposed


def _add_tags(current: List[str], new: Iterable[str]) -> List[str]:
    out = list(current)
    for t in new:
        t = _tag(str(t))
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


# --------------------------------------------------------------------------- #
# Consolidation of classifier items (knowledge-classify.Item instances)
# --------------------------------------------------------------------------- #
def consolidate(items: Sequence[object], existing: Sequence[str], families: Dict[str, Family],
                taxonomy_file: str = "") -> Consolidation:
    """Fold overlapping proposals in place. Existing profiles come first, families second,
    sibling roots proposed in the same run third; anything else is preserved verbatim."""
    existing_set = tuple(x for x in existing if isinstance(x, str) and not x.startswith("_"))
    proposed = sorted({str(i.new_profile["name"]) for i in items if getattr(i, "new_profile", None)})
    # Roots first: a name whose own canonical differs must not act as a sibling root.
    roots = tuple(p for p in proposed if canonical_for(p, families, existing_set, ())[1] == "")
    res = Consolidation(taxonomy_file=taxonomy_file)
    for it in items:
        if not getattr(it, "new_profile", None):
            continue
        name = str(it.new_profile["name"])
        canonical, rule = canonical_for(name, families, existing_set, roots)
        if canonical == name:
            continue
        fam = res.families.get(canonical)
        if fam is None:
            fam = res.families[canonical] = FamilyResult(canonical, canonical in existing_set,
                                                         tags=families[canonical].tags if canonical in families else ())
        fam.aliases = tuple(sorted(set(fam.aliases) | {name}))
        fam.sources = tuple(sorted(set(fam.sources) | {it.source}))
        fam.rules[name] = rule
        sub = subtopic(name, canonical)
        it.tags = _add_tags(getattr(it, "tags", []), [sub, *fam.tags])
        it.taxonomy = {"proposed": name, "canonical": canonical, "rule": rule}
        reasons = [r for r in it.review_reasons if r != NEW_PROFILE_REASON + name]
        if fam.existing:
            it.profile, it.new_profile = canonical, {}
            reasons = [r for r in reasons if r != "no-profile"]
            reasons.append(f"{EXISTING_REASON}{name}->{canonical}")
        else:
            spec = dict(it.new_profile)
            spec["name"] = canonical
            spec["aliases"] = sorted(set(spec.get("aliases") or []) | {name})
            spec["tags"] = _add_tags(list(spec.get("tags") or []), [sub, *fam.tags])
            it.new_profile = spec
            if "no-profile" in reasons and NEW_PROFILE_REASON + canonical not in reasons:
                reasons.insert(reasons.index("no-profile") + 1, NEW_PROFILE_REASON + canonical)
            reasons.append(f"{CONSOLIDATED_REASON}{name}->{canonical}")
        it.review_reasons = list(dict.fromkeys(reasons))
        it.needs_review = True
        it.status = "needs-review"
    # Members that already carried the canonical name are sources of the family too.
    for it in items:
        np = getattr(it, "new_profile", None)
        if np and np.get("name") in res.families and not res.families[np["name"]].existing:
            fam = res.families[np["name"]]
            fam.sources = tuple(sorted(set(fam.sources) | {it.source}))
    return res


def enrich_specs(specs: Dict[str, Dict[str, object]], res: Consolidation, existing: Sequence[str],
                 families: Dict[str, Family]) -> Dict[str, Dict[str, object]]:
    """One complete proposal per canonical family: aliases, tags and a parent default from the
    family map (only when the model gave none and the parent is an existing profile or 'none').
    Existing profiles are never specs."""
    out: Dict[str, Dict[str, object]] = {}
    for name, spec in specs.items():
        if name in existing:
            continue
        spec = dict(spec)
        fam = res.families.get(name)
        fdef = families.get(name)
        aliases = set(spec.get("aliases") or []) | (set(fam.aliases) if fam else set())
        tags = list(spec.get("tags") or [])
        tags = _add_tags(tags, [subtopic(a, name) for a in sorted(aliases)] + list(fdef.tags if fdef else ()))
        spec["aliases"] = sorted(aliases)
        spec["tags"] = tags
        if not spec.get("parent") and fdef:
            spec["parent"] = fdef.parent if fdef.parent in existing else "none"
        out[name] = spec
    return out
