#!/usr/bin/env python3
"""knowledge_plan — the reviewed plan, canonical references and assignment
metadata shared by knowledge-classify.py, knowledge-apply.py, knowledge-learn.py
and knowledge-stage.sh.

Plan (``plan-<report>.json`` next to the report, written by classify, edited by the operator,
applied by ``make knowledge-apply PLAN=<plan> APPROVE=1``):

  {"plan_version": 1, "generated": ..., "library": ..., "report": ...,
   "items": [{"source": "<collection>/<path>", "source_id": "<16 hex>",
              "source_sha256": "<64 hex>", "status": "proposed|needs-review|already-imported|...",
              "proposed_profile": "...", "confidence": 0.9, "rationale": "...",
              "review_reasons": [...], "decision": "assign|skip|existing|review",
              "profile": "<target profile>", "purpose": "", "chapters": [], "tags": [],
              "secondary_reason": "", "note": ""}],
   "new_profiles": [{"name": "...", "approved": false, "purpose": "...", "in_scope": [...],
                     "out_of_scope": [...], "parent": "...", "overlap": [...],
                     "boundaries": [...], "sources": [...]}]}

Decisions: ``assign`` (write the assignment and learn it), ``skip`` (leave the
book out of this run), ``existing`` (already owned; nothing to do) are terminal.
``review`` is the default for anything the guardrails flagged and must be
changed by hand before the plan can be applied.

No review reason is a risk label. Plans written before the ``high-risk-*`` and
``model-risk:*`` labels were removed, or while instruction-like book text was
flagged ``suspicious-text``, are not re-interpreted: a ``review`` item that
still carries a retired reason is ``pending-stale`` (never approved, skipped
or re-labelled here) until the operator regenerates the plan with
``make knowledge-classify`` or sets its decision by hand.

Source identity is content, never a file name: sha256 over the sorted book
files (name, size, bytes) of the library path. Two library copies of the same
bytes share one ``source_id`` and thus one canonical copy.

Reference files (``<profile>/knowledge/refs/<name>.ref.md``) are tiny Markdown
files with a front matter that points at the shared canonical copy
(``$KNOWLEDGE_CANONICAL/sources/<source_id>/<file>``) and its sha256; readers
validate the path (inside the canonical root, regular file) and the hash before
using the text. No symlinks.

CLI (for shell scripts):
  knowledge_plan.py canonical-entries <knowledge-assignments.json>
      prints the manifest entries that are shared canonically, one per line.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
PLAN_VERSION = 1
ASSIGNMENTS_VERSION = 1
TERMINAL = ("assign", "skip", "existing")
DECISIONS = TERMINAL + ("review",)
KNOWN_ACTIONS = ("assign", "canonicalize", "reference", "install", "learn")
# Item states a dry-run reports. ready/skip/existing are executable; pending-*
# items are not (and are never guessed); invalid plans raise PlanError.
READY, PENDING_REVIEW, PENDING_PROFILE, PENDING_ERROR = ("ready", "pending-review", "pending-profile-approval",
                                                          "pending-error")
PENDING_STALE = "pending-stale"
STATES = (READY, "skip", "existing", PENDING_REVIEW, PENDING_PROFILE, PENDING_ERROR, PENDING_STALE)
# Review reasons older classifiers wrote; the current one writes no risk label at all.
LEGACY_RISK_PREFIXES = ("high-risk-content:", "high-risk-profile:", "model-risk:")
# Retired exact reasons: instruction-like text in a curated book is data, not a review gate.
RETIRED_REASONS = ("suspicious-text",)
NEW_PROFILE_REASON = "new-profile-proposal:"
BOOK_EXTS = (".epub", ".pdf", ".md", ".txt")
TEXT_EXTS = (".md", ".txt")
REF_SUFFIX = ".ref.md"
REF_DIR = "refs"
ASSIGNMENTS_FILE = "knowledge-assignments.json"
MANIFEST_FILE = "knowledge-sources.txt"
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SOURCE_ID_LEN = 16
SPEC_LIST_FIELDS = ("in_scope", "out_of_scope", "boundaries")
SPEC_TEXT_FIELDS = ("purpose", "parent")
PLACEHOLDER_RE = re.compile(r"^\s*<.*>\s*$|^\s*(tbd|todo|fill ?in)\s*$", re.I)
MIN_SECONDARY_REASON = 20


class PlanError(Exception):
    """The plan cannot be applied as written; every reason is in the message."""


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def canonical_root(default_repo: Path = REPO) -> Path:
    return Path(os.environ.get("KNOWLEDGE_CANONICAL") or default_repo / "data" / "knowledge-canonical")


def slugify(text: str, width: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:width].rstrip("-")) or "source"


# --------------------------------------------------------------------------- #
# Library sources and identity
# --------------------------------------------------------------------------- #
def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_source(books: Path, rel: str) -> Path:
    """Validate a library-relative path and return its real location."""
    if not isinstance(rel, str) or not rel or any(c in rel for c in "\0\n\r") or rel.startswith(("/", "-")):
        raise PlanError(f"unsafe source path: {rel!r}")
    parts = rel.split("/")
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
        raise PlanError(f"source path must be <collection>/<path> without '.', '..' or '//': {rel!r}")
    root = books.resolve()
    cur = root
    for p in parts:
        cur = cur / p
        if cur.is_symlink():
            raise PlanError(f"source path goes through a symlink: {rel!r}")
    if not cur.exists():
        raise PlanError(f"source not found in the library: {rel!r}")
    real = cur.resolve()
    if not is_within(real, root):
        raise PlanError(f"source resolves outside the library: {rel!r}")
    return real


def book_files(src: Path, exts: Sequence[str] = BOOK_EXTS) -> List[Path]:
    """Regular, non-hidden book files below src (symlinks are never followed)."""
    if src.is_file():
        return [src] if src.suffix.lower() in exts else []
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if not f.startswith(".") and p.suffix.lower() in exts and not p.is_symlink() and p.is_file():
                out.append(p)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SourceIdentity:
    source: str
    source_id: str
    sha256: str
    files: List[str] = field(default_factory=list)     # relative to the source path (file name for a file)


def source_identity(books: Path, rel: str) -> SourceIdentity:
    """Content identity of a library path: sha256 over (name, size, bytes) of its book files."""
    src = safe_source(books, rel)
    files = book_files(src)
    if not files:
        raise PlanError(f"no book files (.epub/.pdf/.md/.txt) under {rel!r}")
    base = src.parent if src.is_file() else src
    h = hashlib.sha256()
    rels = []
    for f in files:
        r = f.relative_to(base).as_posix()
        rels.append(r)
        h.update(f"{r}\0{f.stat().st_size}\0".encode("utf-8"))
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    digest = h.hexdigest()
    return SourceIdentity(rel, digest[:SOURCE_ID_LEN], digest, rels)


# --------------------------------------------------------------------------- #
# Plan building (classify side)
# --------------------------------------------------------------------------- #
def default_decision(status: str, needs_review: bool, action: str = "none") -> str:
    if status == "already-imported":
        return "existing"
    if action in ("assigned", "already-listed"):
        return "existing"
    if status == "proposed" and not needs_review and action in ("none", "would-assign"):
        return "assign"
    if status == "approved" and action in ("would-assign",):
        return "assign"
    return "review"


def plan_item(source: str, identity: Optional[SourceIdentity], status: str, proposed_profile: str,
              profile: str, decision: str, confidence=None, rationale: str = "",
              review_reasons=(), owners=(), note: str = "", new_profile: Optional[Dict[str, str]] = None,
              secondary_reason: str = "", tags: Sequence[str] = ()) -> Dict[str, object]:
    return {
        "source": source,
        "source_id": identity.source_id if identity else "",
        "source_sha256": identity.sha256 if identity else "",
        "status": status,
        "proposed_profile": proposed_profile,
        "confidence": confidence,
        "rationale": rationale,
        "review_reasons": list(review_reasons),
        "owners": list(owners),
        "new_profile_suggestion": dict(new_profile or {}),
        "decision": decision,
        "profile": profile,
        "purpose": "",
        "chapters": [],
        "tags": list(tags),
        "secondary_reason": secondary_reason,
        "note": note,
    }


def new_profile_proposal(name: str, purpose: str, sources: Iterable[str],
                         spec: Optional[Dict[str, object]] = None,
                         consolidated: Sequence[str] = ()) -> Dict[str, object]:
    """A proposal only (``approved: false``). ``spec`` carries the classifier's complete
    fields when it produced them; ``spec_status`` says whether the spec is complete
    ("complete") or which fields are still missing ("incomplete:<fields>"). ``tags`` are the
    subcategories the family owns and ``aliases`` the narrower names the taxonomy layer folded
    into it (``consolidated``: the ones folded in this run)."""
    spec = spec or {}
    aliases = sorted(set(spec.get("aliases") or []) | set(consolidated))
    out = {
        "name": name,
        "approved": False,
        "purpose": str(spec.get("purpose") or purpose),
        "in_scope": list(spec.get("in_scope") or []),
        "out_of_scope": list(spec.get("out_of_scope") or []),
        "parent": str(spec.get("parent") or ""),
        "overlap": list(spec.get("overlap") or []),
        "boundaries": list(spec.get("boundaries") or []),
        "tags": list(spec.get("tags") or []),
        "aliases": aliases,
        "sources": sorted(set(sources)),
    }
    missing = spec_missing(out)
    out["spec_status"] = "complete" if not missing else "incomplete:" + ",".join(missing)
    out["note"] = (("" if not consolidated else
                    f"taxonomy consolidation applied: the narrower proposals {', '.join(consolidated)} were folded "
                    "into this broad family (their subjects are tags); approve at most this one. ")
                   + "proposed by the classifier, not created. Review the spec (edit any field), then either "
                   "set approved to true and each item's profile to this name with decision assign, or accept "
                   "every complete proposal at once: make knowledge-apply PLAN=<plan> APPROVE=1 APPROVE_ALL=1. "
                   "knowledge-apply creates it only then."
                   + ("" if not missing else f" Incomplete ({', '.join(missing)}): fill it in by hand or re-run "
                      "make knowledge-classify; APPROVE_ALL leaves it pending."))
    return out


def spec_missing(spec: Dict[str, object]) -> List[str]:
    """Fields a new-profile spec still needs before it can be approved (``approved`` itself excluded)."""
    missing = []
    for key in SPEC_TEXT_FIELDS:
        v = spec.get(key)
        if not isinstance(v, str) or not v.strip() or _placeholder(v):
            missing.append(key)
    for key in SPEC_LIST_FIELDS:
        v = spec.get(key)
        if not _is_list_of_str(v) or not v or any(_placeholder(x) for x in v):
            missing.append(key)
    return missing


def spec_complete(spec: object) -> bool:
    return isinstance(spec, dict) and isinstance(spec.get("name"), str) and not spec_missing(spec)


def plan_path_for(report: Path) -> Path:
    """``plan-<report name>``: never matches a ``classify-*.json`` latest-report glob."""
    return report.with_name("plan-" + report.name)


def write_plan(path: Path, items: List[Dict[str, object]], new_profiles: List[Dict[str, object]],
               meta: Dict[str, object]) -> Path:
    data = {"plan_version": PLAN_VERSION, "generated": now_iso()}
    data.update(meta)
    data["decisions"] = {"terminal": list(TERMINAL), "default_for_review": "review",
                         "help": "set every decision to assign (with profile), skip or existing; "
                                 "review blocks apply"}
    data["items"] = items
    data["new_profiles"] = new_profiles
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Plan loading and validation (apply side; fail closed)
# --------------------------------------------------------------------------- #
def load_plan(path: Path) -> Dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read plan {path}: {exc}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanError(f"plan {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise PlanError(f"plan {path} is not a JSON object")
    if data.get("plan_version") != PLAN_VERSION:
        raise PlanError(f"plan {path}: plan_version {data.get('plan_version')!r} is not {PLAN_VERSION}")
    for key in ("items", "new_profiles"):
        if not isinstance(data.get(key), list):
            raise PlanError(f"plan {path}: {key!r} must be a list")
    data["_plan_id"] = hashlib.sha256(raw).hexdigest()[:SOURCE_ID_LEN]
    data["_path"] = str(path)
    return data


def existing_profiles(profiles_dir: Path) -> Dict[str, Path]:
    """Book-backed profile seeds: <profiles_dir>/<name>/knowledge-sources.txt (no _template, no symlinks)."""
    out: Dict[str, Path] = {}
    if not profiles_dir.is_dir():
        return out
    for d in sorted(profiles_dir.iterdir()):
        if d.name.startswith("_") or d.is_symlink() or not d.is_dir():
            continue
        if (d / MANIFEST_FILE).is_file() or (d / "SOUL.md").is_file():
            out[d.name] = d
    return out


def _is_list_of_str(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def _placeholder(v: str) -> bool:
    return bool(PLACEHOLDER_RE.match(v or ""))


def validate_new_profile(spec: object, profiles: Dict[str, Path], created_by_plan: Sequence[str]) -> List[str]:
    errs: List[str] = []
    if not isinstance(spec, dict):
        return ["new_profiles: every entry must be an object"]
    name = spec.get("name")
    where = f"new profile {name!r}"
    if not isinstance(name, str) or not PROFILE_RE.match(name) or ".." in name:
        return [f"{where}: name must match {PROFILE_RE.pattern} (lowercase letters, digits, dashes)"]
    if name in profiles and name not in created_by_plan:
        errs.append(f"{where}: a profile with that name already exists; assign to it as an existing "
                    "profile and remove it from new_profiles")
    if spec.get("approved") is not True:
        errs.append(f"{where}: approved must be exactly true (it is a proposal until you approve it)")
    for key in SPEC_TEXT_FIELDS:
        v = spec.get(key)
        if not isinstance(v, str) or not v.strip() or _placeholder(v):
            errs.append(f"{where}: {key} must be filled in (parent may be 'none')")
    for key in SPEC_LIST_FIELDS:
        v = spec.get(key)
        if not _is_list_of_str(v) or not v or any(_placeholder(x) for x in v):
            errs.append(f"{where}: {key} must be a non-empty list of statements")
    if not _is_list_of_str(spec.get("overlap", [])):
        errs.append(f"{where}: overlap must be a list of profile names")
    parent = spec.get("parent")
    if isinstance(parent, str) and parent.strip() and parent != "none" and not PROFILE_RE.match(parent):
        errs.append(f"{where}: parent must be a profile name or 'none'")
    return errs


@dataclass
class Validated:
    plan: Dict[str, object]
    assign: List[Dict[str, object]]                 # items with decision assign
    skipped: List[Dict[str, object]]
    existing: List[Dict[str, object]]
    identities: Dict[str, SourceIdentity]           # source -> identity (recomputed now)
    new_profiles: Dict[str, Dict[str, object]]      # approved specs by name
    profiles: Dict[str, Path]                       # existing seeds
    affected: List[str]                             # profiles that receive assignments


def validate_plan(plan: Dict[str, object], books: Path, profiles_dir: Path,
                  created_by_plan: Sequence[str] = ()) -> Validated:
    """Every refusal is collected; nothing is written. Raises PlanError with all of them."""
    errs: List[str] = []
    profiles = existing_profiles(profiles_dir)
    specs: Dict[str, Dict[str, object]] = {}
    for spec in plan["new_profiles"]:
        e = validate_new_profile(spec, profiles, created_by_plan)
        errs += e
        if isinstance(spec, dict) and isinstance(spec.get("name"), str):
            if spec["name"] in specs:
                errs.append(f"new profile {spec['name']!r} is listed twice")
            specs[spec["name"]] = spec
    assign, skipped, existing = [], [], []
    identities: Dict[str, SourceIdentity] = {}
    seen: set = set()
    for n, it in enumerate(plan["items"], 1):
        if not isinstance(it, dict):
            errs.append(f"item {n}: not an object")
            continue
        src = it.get("source")
        where = f"item {n} ({src!r})"
        if not isinstance(src, str):
            errs.append(f"{where}: source must be a string")
            continue
        decision = it.get("decision")
        if decision not in DECISIONS:
            errs.append(f"{where}: decision {decision!r} is not one of {', '.join(DECISIONS)}")
            continue
        actions = it.get("actions", [])
        if actions and (not isinstance(actions, list) or any(a not in KNOWN_ACTIONS for a in actions)):
            errs.append(f"{where}: unknown action in {actions!r} (known: {', '.join(KNOWN_ACTIONS)})")
        if decision == "review":
            errs.append(f"{where}: decision is still review ({', '.join(it.get('review_reasons') or []) or 'needs a decision'}); "
                        "set it to assign (with a profile), skip or existing")
            continue
        if decision == "skip":
            skipped.append(it)
            continue
        if decision == "existing":
            existing.append(it)
            continue
        prof = it.get("profile")
        if not isinstance(prof, str) or not prof:
            errs.append(f"{where}: decision assign needs a profile")
            continue
        if not PROFILE_RE.match(prof) or ".." in prof or prof.startswith("_"):
            errs.append(f"{where}: unsafe profile name {prof!r} (must match {PROFILE_RE.pattern})")
            continue
        if prof not in profiles and prof not in specs:
            errs.append(f"{where}: profile {prof!r} does not exist and is not an approved new profile in this plan "
                        f"(existing: {', '.join(profiles) or 'none'})")
            continue
        key = (src, prof)
        if key in seen:
            errs.append(f"{where}: assigned to {prof} twice")
            continue
        seen.add(key)
        sr = it.get("secondary_reason", "")
        if sr and (not isinstance(sr, str) or len(" ".join(sr.split())) < MIN_SECONDARY_REASON):
            errs.append(f"{where}: secondary_reason must state the substantial, recurring, independent purpose "
                        f"(at least {MIN_SECONDARY_REASON} characters)")
        for lf in ("chapters", "tags"):
            if not isinstance(it.get(lf, []), list):
                errs.append(f"{where}: {lf} must be a list")
        if src not in identities:
            try:
                identities[src] = source_identity(books, src)
            except PlanError as exc:
                errs.append(f"{where}: {exc}")
                continue
        ident = identities[src]
        if it.get("source_sha256") != ident.sha256 or it.get("source_id") != ident.source_id:
            errs.append(f"{where}: stale source hash: the library content changed since the plan was written "
                        f"(plan {str(it.get('source_sha256'))[:12]}…, library {ident.sha256[:12]}…); "
                        "re-run make knowledge-classify and review a fresh plan")
            continue
        assign.append(it)
    # a source may be assigned to several profiles only when the plan says why
    by_source: Dict[str, List[Dict[str, object]]] = {}
    for it in assign:
        by_source.setdefault(it["source"], []).append(it)
    for src, its in by_source.items():
        if len(its) > 1 and sum(1 for x in its if not x.get("secondary_reason")) > 1:
            errs.append(f"{src!r} is assigned to {len(its)} profiles; all but the canonical one need a "
                        "secondary_reason (one canonical profile per book)")
    used_new = {it["profile"] for it in assign if it["profile"] in specs}
    for name in specs:
        if name not in used_new:
            errs.append(f"new profile {name!r} has no item assigned to it (give it at least one source, or remove it)")
    if errs:
        raise PlanError("plan refused:\n  - " + "\n  - ".join(errs))
    affected = sorted({it["profile"] for it in assign})
    return Validated(plan, assign, skipped, existing, identities, specs, profiles, affected)


# --------------------------------------------------------------------------- #
# Plan resolution: states, explicit bulk approval (APPROVE_ALL), digest
# --------------------------------------------------------------------------- #
@dataclass
class Resolution:
    """The effective plan after the operator's policy was applied to a copy of the loaded plan."""
    plan: Dict[str, object]                           # deep copy; the file is never edited
    items: List[Dict[str, object]]                    # per item: source, state, decision, profile, reasons, change
    specs: List[Dict[str, object]]                    # per new profile: name, state, missing, legacy, sources
    changes: List[Dict[str, object]]                  # decisions APPROVE_ALL / SKIP_ERRORS changed (source, from, to, profile)
    approved_profiles: List[str]                      # new profiles approved by this run (not by hand)
    policy: Dict[str, bool]
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def pending(self) -> List[Dict[str, object]]:
        return [i for i in self.items if i["state"].startswith("pending")]

    @property
    def decisions(self) -> List[Tuple[str, str, str]]:
        return sorted((str(i["source"]), str(i["decision"]), str(i["profile"] or "")) for i in self.items)

    @property
    def digest(self) -> str:
        return approval_digest(self.decisions, self.approved_profiles, self.policy)


def approval_digest(decisions: Iterable[Tuple[str, str, str]], approved_profiles: Iterable[str],
                    policy: Dict[str, bool]) -> str:
    """Stable 16-hex digest of exactly what an approval covers: every (source, decision, profile),
    the new profiles it approves and the policy flags. Written to the approval receipt."""
    body = {"decisions": sorted([list(d) for d in decisions]), "approved_profiles": sorted(approved_profiles),
            "policy": {k: bool(v) for k, v in sorted(policy.items())}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:SOURCE_ID_LEN]


def legacy_risk_reasons(reasons: Iterable[object]) -> List[str]:
    """The review reasons of an item that are risk labels from an older classifier."""
    return [str(r) for r in reasons if isinstance(r, str) and r.startswith(LEGACY_RISK_PREFIXES)]


def retired_reasons(reasons: Iterable[object]) -> List[str]:
    """The review reasons of an item that the current classifier no longer writes (legacy risk
    labels and ``suspicious-text``): a ``review`` item carrying one is stale, never re-decided."""
    return [str(r) for r in reasons if isinstance(r, str) and (r.startswith(LEGACY_RISK_PREFIXES)
                                                               or r in RETIRED_REASONS)]


def proposal_for(item: Dict[str, object], specs: Dict[str, Dict[str, object]]) -> str:
    """Name of the new-profile proposal an item is tied to, or ''."""
    prof = item.get("profile")
    if isinstance(prof, str) and prof in specs:
        return prof
    for r in item.get("review_reasons") or []:
        if isinstance(r, str) and r.startswith(NEW_PROFILE_REASON) and r[len(NEW_PROFILE_REASON):] in specs:
            return r[len(NEW_PROFILE_REASON):]
    sug = item.get("new_profile_suggestion")
    if isinstance(sug, dict) and sug.get("name") in specs:
        return str(sug["name"])
    return ""


def resolve_plan(plan: Dict[str, object], profiles: Dict[str, Path], approve_all: bool = False,
                 skip_errors: bool = False) -> Resolution:
    """Classify every item and new-profile spec; with ``approve_all`` accept the classifier's
    concrete proposals on a COPY of the plan. Nothing is guessed:

      * clean ``assign`` stays assign; ``skip``/``existing`` stay as written;
      * ``review`` with a concrete existing-profile proposal (``profile`` or ``proposed_profile``)
        becomes assign to it (APPROVE_ALL is the operator's explicit approval);
      * ``review`` that still carries a retired reason (a legacy risk label ``high-risk-content:``,
        ``high-risk-profile:``, ``model-risk:``, or ``suspicious-text``) is ``pending-stale``: the
        plan predates their removal and is regenerated or decided by hand, never re-interpreted here;
      * ``review`` tied to a COMPLETE new-profile proposal becomes assign and the proposal is
        approved; an incomplete proposal stays pending-profile-approval;
      * ``review`` with no target (classifier error, no profile, unhashable source) stays
        pending-error unless ``skip_errors``, which turns it into skip.

    Structural problems (bad decisions, names, stale hashes) are not judged here; the
    executable part of the result goes through :func:`validate_plan` afterwards."""
    eff = copy.deepcopy({k: v for k, v in plan.items() if not k.startswith("_")})
    policy = {"approve_all": bool(approve_all), "skip_errors": bool(skip_errors)}
    specs: Dict[str, Dict[str, object]] = {}
    for spec in eff.get("new_profiles", []):
        if isinstance(spec, dict) and isinstance(spec.get("name"), str):
            specs[spec["name"]] = spec
    items_out: List[Dict[str, object]] = []
    changes: List[Dict[str, object]] = []
    approved_now: List[str] = []
    used_specs: Dict[str, int] = {}
    for it in eff.get("items", []):
        if not isinstance(it, dict) or not isinstance(it.get("source"), str):
            continue   # validate_plan reports the structural error
        src = str(it["source"])
        decision = it.get("decision")
        prof = it.get("profile") if isinstance(it.get("profile"), str) else ""
        reasons = [str(r) for r in (it.get("review_reasons") or []) if isinstance(r, str)]
        row = {"source": src, "decision": decision, "profile": prof, "reasons": reasons, "state": "", "change": "",
               "status": it.get("status", ""), "spec": ""}
        if decision == "review" and retired_reasons(reasons):
            row["state"] = PENDING_STALE
        elif decision == "review":
            spec_name = proposal_for(it, specs)
            target = prof if prof in profiles else (it.get("proposed_profile") if it.get("proposed_profile") in profiles else "")
            unhashable = not it.get("source_sha256")
            if spec_name:
                row["spec"] = spec_name
                if approve_all and spec_complete(specs[spec_name]) and not unhashable:
                    it["decision"], it["profile"] = "assign", spec_name
                    row.update(decision="assign", profile=spec_name, state=READY, change="approved-proposal")
                    if spec_name not in approved_now:
                        approved_now.append(spec_name)
                    changes.append({"source": src, "from": "review", "to": "assign", "profile": spec_name,
                                    "why": "new-profile proposal approved by APPROVE_ALL"})
                else:
                    row["state"] = PENDING_PROFILE
            elif target and not unhashable:
                if approve_all:
                    it["decision"], it["profile"] = "assign", target
                    row.update(decision="assign", profile=target, state=READY, change="approved-proposal")
                    changes.append({"source": src, "from": "review", "to": "assign", "profile": target,
                                    "why": "existing-profile proposal approved by APPROVE_ALL"})
                else:
                    row.update(profile=target, state=PENDING_REVIEW)
            else:
                if skip_errors:
                    it["decision"], it["profile"] = "skip", ""
                    row.update(decision="skip", profile="", state="skip", change="skipped-error")
                    changes.append({"source": src, "from": "review", "to": "skip", "profile": "",
                                    "why": "no target (" + (", ".join(reasons) or "no proposal") + "); SKIP_ERRORS"})
                else:
                    row["state"] = PENDING_ERROR
        elif decision == "assign":
            if prof in specs:
                row["spec"] = prof
                row["state"] = READY if (specs[prof].get("approved") is True or prof in approved_now) else PENDING_PROFILE
            else:
                row["state"] = READY
        elif decision in ("skip", "existing"):
            row["state"] = decision
        else:
            row["state"] = READY      # unknown decision: validate_plan refuses it as invalid
        if row["state"] == READY and row["spec"]:
            used_specs[row["spec"]] = used_specs.get(row["spec"], 0) + 1
        items_out.append(row)
    for name in approved_now:
        specs[name]["approved"] = True
    # a spec approved by APPROVE_ALL whose items are still pending (e.g. unhashable) is not created
    for name in list(approved_now):
        if not used_specs.get(name):
            specs[name]["approved"] = False
            approved_now.remove(name)
    specs_out = []
    for name, spec in specs.items():
        missing = spec_missing(spec)
        state = ("approved" if spec.get("approved") is True and name not in approved_now
                 else "approved-by-approve-all" if name in approved_now
                 else "pending-incomplete" if missing else "pending-unapproved")
        specs_out.append({"name": name, "state": state, "missing": missing, "sources": list(spec.get("sources") or []),
                          "items": used_specs.get(name, 0), "legacy": bool(missing) and "spec_status" not in spec,
                          "purpose": str(spec.get("purpose") or "")})
    counts = {s: 0 for s in STATES}
    for row in items_out:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return Resolution(eff, items_out, specs_out, changes, sorted(approved_now), policy, counts)


def executable_plan(res: Resolution) -> Dict[str, object]:
    """The part of the effective plan that :func:`validate_plan` can judge now: pending items and
    unapproved (or unused) specs are left out, so pending is reported as pending, not as invalid."""
    pending = {i["source"] for i in res.pending}
    out = dict(res.plan)
    out["items"] = [it for it in res.plan.get("items", [])
                    if not (isinstance(it, dict) and it.get("source") in pending)]
    used = {it.get("profile") for it in out["items"] if isinstance(it, dict) and it.get("decision") == "assign"}
    out["new_profiles"] = [s for s in res.plan.get("new_profiles", [])
                           if isinstance(s, dict) and s.get("approved") is True and s.get("name") in used]
    return out


# --------------------------------------------------------------------------- #
# Assignments (per profile seed, tracked in git)
# --------------------------------------------------------------------------- #
def assignments_path(profile_dir: Path) -> Path:
    return profile_dir / ASSIGNMENTS_FILE


def load_assignments(profile_dir: Path) -> Dict[str, object]:
    p = assignments_path(profile_dir)
    if not p.is_file() or p.is_symlink():
        return {"version": ASSIGNMENTS_VERSION, "profile": profile_dir.name, "assignments": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PlanError(f"{p} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("assignments"), list):
        raise PlanError(f"{p}: expected an object with an 'assignments' list")
    data.setdefault("version", ASSIGNMENTS_VERSION)
    data.setdefault("profile", profile_dir.name)
    return data


def save_assignments(profile_dir: Path, data: Dict[str, object]) -> None:
    p = assignments_path(profile_dir)
    if p.is_symlink():
        raise PlanError(f"{p} is a symlink; refusing")
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def canonical_entries(assignments: Dict[str, object]) -> List[str]:
    """Manifest entries this profile shares through canonical references."""
    out = []
    for a in assignments.get("assignments", []):
        if isinstance(a, dict) and a.get("mode", "canonical") == "canonical" and isinstance(a.get("source"), str):
            out.append(a["source"])
    return out


# --------------------------------------------------------------------------- #
# Reference files
# --------------------------------------------------------------------------- #
def is_ref(path: Path) -> bool:
    return path.name.endswith(REF_SUFFIX) and path.parent.name == REF_DIR


def ref_name(source: str, file_rel: str, source_id: str) -> str:
    base = source.rstrip("/").rsplit("/", 1)[-1]
    stem = Path(file_rel).stem
    stem = Path(stem).stem if stem.lower().endswith((".epub", ".pdf")) else stem
    return f"{slugify(base)}-{slugify(stem, 24)}-{source_id[:8]}{REF_SUFFIX}"


def render_ref(fields: Dict[str, object]) -> str:
    order = ("canonical-ref", "source", "source_id", "canonical_root", "canonical_rel", "sha256", "profile",
             "purpose", "chapters", "tags", "secondary_reason", "plan_id", "written")
    lines = ["---"]
    for k in order:
        v = fields.get(k, "")
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        v = str(v).replace("\n", " ").strip()
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(f"# Reference: {fields.get('source')}")
    lines.append("")
    lines.append("This profile reads the shared canonical copy of this book; the text is not duplicated here.")
    lines.append(f"Canonical file: `{Path(str(fields.get('canonical_root'))) / str(fields.get('canonical_rel'))}`")
    lines.append(f"Verify before reading: sha256 `{fields.get('sha256')}` and the path must stay under the "
                 "canonical root. Do not copy the book into this folder; knowledge-apply manages it.")
    if fields.get("purpose"):
        lines.append(f"Purpose for this profile: {fields['purpose']}")
    return "\n".join(lines) + "\n"


def parse_ref(path: Path) -> Dict[str, str]:
    """Front matter of a reference file as a dict; raises ValueError when it is not one."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError as exc:
        raise ValueError(f"unreadable: {exc}") from None
    if not head.startswith("---\n"):
        raise ValueError("no front matter")
    end = head.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unterminated front matter")
    out: Dict[str, str] = {}
    for line in head[4:end].splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    if out.get("canonical-ref") != "1":
        raise ValueError("not a canonical reference")
    for k in ("source", "source_id", "canonical_rel", "sha256"):
        if not out.get(k):
            raise ValueError(f"reference lacks {k}")
    return out


def resolve_ref(ref: Dict[str, str], root: Optional[Path] = None) -> Tuple[Optional[Path], str]:
    """(canonical file, "") when the reference validates, else (None, problem)."""
    root = root or canonical_root()          # the configured store only; never a root named by the file
    named = ref.get("canonical_root", "")
    if named and Path(named).resolve() != root.resolve():
        return None, f"canonical_root {named} is not the configured canonical store {root}"
    rel = ref.get("canonical_rel", "")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in rel.split("/")):
        return None, f"unsafe canonical_rel {rel!r}"
    if not re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", "")):
        return None, "sha256 malformed"
    if not root.is_dir():
        return None, f"canonical root {root} not found"
    path = root / rel
    if path.is_symlink() or not path.is_file():
        return None, f"canonical file missing or not a regular file: {path}"
    if not is_within(path.resolve(), root.resolve()):
        return None, f"canonical file resolves outside {root}"
    if sha256_file(path) != ref["sha256"]:
        return None, f"canonical file changed since the reference was written: {path}"
    return path, ""


# --------------------------------------------------------------------------- #
# CLI for shell scripts
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "canonical-entries":
        p = Path(argv[1])
        if not p.is_file():
            return 0
        try:
            data = load_assignments(p.parent) if p.name == ASSIGNMENTS_FILE else json.loads(p.read_text("utf-8"))
        except (PlanError, ValueError) as exc:
            print(f"[knowledge_plan][error] {exc}", file=sys.stderr)
            return 1
        for e in canonical_entries(data):
            print(e)
        return 0
    print(__doc__.split("CLI (for shell scripts):", 1)[-1].strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
