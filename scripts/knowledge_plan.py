#!/usr/bin/env python3
"""knowledge_plan — the reviewed plan, canonical references and assignment
metadata shared by knowledge-classify.py, knowledge-apply.py, knowledge-learn.py
and knowledge-stage.sh.

Plan (``plan-<report>.json`` next to the report, written by classify, edited by the operator,
applied by ``make knowledge-apply PLAN=<plan> APPROVE=1``):

  {"plan_version": 1, "generated": ..., "library": ..., "report": ...,
   "items": [{"source": "<collection>/<path>", "source_id": "<16 hex>",
              "source_sha256": "<64 hex>", "status": "proposed|needs-review|already-imported|...",
              "proposed_profile": "...", "confidence": 0.9, "rationale": "...", "risk": [...],
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
              profile: str, decision: str, confidence=None, rationale: str = "", risk=(),
              review_reasons=(), owners=(), note: str = "", new_profile: Optional[Dict[str, str]] = None,
              secondary_reason: str = "") -> Dict[str, object]:
    return {
        "source": source,
        "source_id": identity.source_id if identity else "",
        "source_sha256": identity.sha256 if identity else "",
        "status": status,
        "proposed_profile": proposed_profile,
        "confidence": confidence,
        "rationale": rationale,
        "risk": list(risk),
        "review_reasons": list(review_reasons),
        "owners": list(owners),
        "new_profile_suggestion": dict(new_profile or {}),
        "decision": decision,
        "profile": profile,
        "purpose": "",
        "chapters": [],
        "tags": [],
        "secondary_reason": secondary_reason,
        "note": note,
    }


def new_profile_proposal(name: str, purpose: str, sources: Iterable[str]) -> Dict[str, object]:
    """A proposal only: every field must be reviewed and ``approved`` set by hand."""
    return {
        "name": name,
        "approved": False,
        "purpose": purpose,
        "in_scope": [],
        "out_of_scope": [],
        "parent": "",
        "overlap": [],
        "boundaries": [],
        "sources": sorted(set(sources)),
        "note": "proposed by the classifier, not created. Fill purpose, in_scope, out_of_scope, parent "
                "(or 'none'), overlap, boundaries; set approved to true; set each item's profile to this "
                "name and its decision to assign. knowledge-apply creates it only then.",
    }


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
