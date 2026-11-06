#!/usr/bin/env python3
"""Capability registry: strict loading, schema + semantic validation (pure library, read-only).

The registry (hermes/capabilities/registry.json) is a catalog of reusable
methods and their contracts. It is data: nothing here schedules, dispatches,
installs, executes, opens a network connection or grants a tool or permission.
Readiness is computed from evidence (see ``readiness``), never stored.

Schema validation uses the ``jsonschema`` package (Draft 2020-12, declared in
scripts/requirements-capabilities.txt); without it every validation fails
closed instead of falling back to a partial checker.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
CAPABILITIES_DIR = REPO / "hermes" / "capabilities"
SCHEMA_DIR = CAPABILITIES_DIR / "schemas"
COMPANY_SCHEMA = REPO / "hermes" / "companies" / "schema.json"
PROJECT_SCHEMA = REPO / "hermes" / "projects" / "schema.json"

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_INT_DIGITS = 18
MAX_JSON_DEPTH = 64


class RegistryError(ValueError):
    """An input file or record failed strict loading or validation."""


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise RegistryError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise RegistryError(f"non-finite number {text!r}")
    return value


def _bounded_int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise RegistryError(f"integer literal longer than {MAX_INT_DIGITS} digits")
    return int(text)


def _reject_constant(text: str) -> Any:
    raise RegistryError(f"non-finite number {text!r}")


def _check_depth(text: str, limit: int = MAX_JSON_DEPTH) -> None:
    """Reject array/object nesting deeper than ``limit`` before the recursive decoder runs.
    Brackets inside strings (including escaped quotes) do not count."""
    depth = 0
    in_str = escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                raise RegistryError(f"JSON nesting deeper than {limit} levels")
        elif ch in "]}":
            depth -= 1


def parse_json(text: str) -> Any:
    """Parse JSON text strictly: no duplicate keys, NaN/Infinity, huge integers or deep nesting."""
    _check_depth(text)
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_finite_float,
                          parse_int=_bounded_int, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from None
    except RecursionError:  # defensive: _check_depth bounds nesting first
        raise RegistryError("JSON nesting too deep") from None


def load_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Read one regular, single-link, non-symlinked UTF-8 JSON file of bounded size."""
    path = Path(os.path.abspath(path))
    if os.path.realpath(path.parent) != str(path.parent):
        raise RegistryError(f"{path}: a parent directory is a symlink")
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer before fstat refuses it.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if os.path.islink(path):
            raise RegistryError(f"{path}: refusing a symlink") from None
        raise RegistryError(f"{path}: cannot open ({exc.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RegistryError(f"{path}: not a regular file")
        if st.st_nlink != 1:
            raise RegistryError(f"{path}: refusing a hard link ({st.st_nlink} links)")
        if st.st_size > max_bytes:
            raise RegistryError(f"{path}: {st.st_size} bytes exceeds the {max_bytes}-byte bound")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RegistryError(f"{path}: exceeds the {max_bytes}-byte bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RegistryError(f"{path}: not valid UTF-8") from None
    try:
        return parse_json(text)
    except RegistryError as exc:
        raise RegistryError(f"{path}: {exc}") from None




MAX_SOURCE_BYTES = 8 * 1024 * 1024

# Exact upstream revisions approved for this integration (research 2026-11-04).
# A lock that names any other revision is invalid: pins move only by review.
PINNED_REVISIONS = {
    "gstack": "622e8b1f76d293b647eca7b33d9dd060ab618b21",
    "pm-skills": "2a12295a7717569baa7be181b4cfebe3ca38a6bb",
    "solosquad": "197ddcd1032fdaf2dd58ee37d075da232bdd1db3",
    "pna-builders": "e4237f11ad1359f3e8da722f56e37962a69c0ce4",
}
NAME_PREFIX = {"pm-skills": "pm-", "gstack": "gstack-"}
ID_NAMESPACE = {"kit": ("original",), "pm": ("adapted",), "gstack": ("adapted",), "existing": ("existing-kit",)}
ID_UPSTREAM = {"pm": "pm-skills", "gstack": "gstack"}
MOVED_STUB_RE = re.compile(r"^pm-ai-shipping/(skills|commands)/")
SOURCE_CHANGING = {"source-modification", "commit-push"}
WRITE_APPROVALS = {"source-modification", "commit-push"}
# A tool need that can spend money or write outside the task must be matched by the declared side effect.
TOOL_SIDE_EFFECT = {"image.generate-paid": "spend", "github.approved-write": "external-write"}


# ---------------------------------------------------------------- schemas

def _jsonschema():
    try:
        import jsonschema  # type: ignore
    except ImportError:
        raise RegistryError("the jsonschema package (>=4.10, Draft 2020-12) is required: "
                            "apt install python3-jsonschema, or pip install -r scripts/requirements-capabilities.txt") from None
    if not hasattr(jsonschema, "Draft202012Validator"):
        raise RegistryError("jsonschema is too old: Draft 2020-12 support (>=4.10) is required")
    return jsonschema


def check_schema(schema: Any) -> None:
    js = _jsonschema()
    try:
        js.Draft202012Validator.check_schema(schema)
    except js.exceptions.SchemaError as exc:
        raise RegistryError(f"invalid schema: {exc.message}") from None


_SCHEMAS: Dict[str, Any] = {}


def load_schema(name: str) -> Any:
    """A schema by file name under hermes/capabilities/schemas, or by absolute path."""
    if name not in _SCHEMAS:
        schema = load_json(Path(name) if os.path.isabs(name) else SCHEMA_DIR / name)
        check_schema(schema)
        _SCHEMAS[name] = schema
    return _SCHEMAS[name]


def schema_errors(instance: Any, schema_name: str, label: str) -> List[str]:
    """All Draft 2020-12 violations as stable one-line strings (empty = valid)."""
    js = _jsonschema()
    validator = js.Draft202012Validator(load_schema(schema_name))
    out = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path]):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{label}: schema: {where}: {err.message[:300]}")
    return out


# ---------------------------------------------------------------- digests and safe repo reads

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def files_digest(pairs: Iterable[Tuple[str, str]]) -> str:
    """Order-independent digest over (label, sha256) pairs: sha256 of sorted 'label<TAB>sha\n' lines."""
    return sha256_bytes("".join(f"{label}\t{sha}\n" for label, sha in sorted(pairs)).encode("utf-8"))


def safe_repo_file(repo: Path, rel: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    """Read repo/rel refusing absolute/dot-dot paths, any symlinked component, hard links and non-regular files."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or any(p in ("..", ".") for p in parts):
        raise RegistryError(f"{rel}: path escapes the repository")
    cur = Path(os.path.abspath(repo))
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            raise RegistryError(f"{rel}: missing") from None
        if stat.S_ISLNK(st.st_mode):
            raise RegistryError(f"{rel}: refusing a symlink at {'/'.join(parts[:i + 1])}")
    if not stat.S_ISREG(st.st_mode):
        raise RegistryError(f"{rel}: not a regular file")
    if st.st_nlink != 1:
        raise RegistryError(f"{rel}: refusing a hard link")
    if st.st_size > max_bytes:
        raise RegistryError(f"{rel}: exceeds {max_bytes} bytes")
    fd = os.open(cur, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        fst = os.fstat(fh.fileno())
        if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1 or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise RegistryError(f"{rel}: changed or not a single-link regular file at open")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RegistryError(f"{rel}: exceeds {max_bytes} bytes (grew while being read)")
    return data


def implementation_digest(repo: Path, paths: Sequence[str]) -> str:
    return files_digest((p, sha256_bytes(safe_repo_file(repo, p))) for p in paths)


def upstream_digest(lock_index: Dict[Tuple[str, str], dict], upstream: str, paths: Sequence[str]) -> Optional[str]:
    try:
        return files_digest((f"{upstream}:{p}", lock_index[(upstream, p)]["sha256"]) for p in paths)
    except KeyError:
        return None


def kit_skill_names(repo: Path) -> Dict[str, str]:
    """Frontmatter ``name:`` of every kit skill under hermes/skills (name -> SKILL.md path)."""
    names: Dict[str, str] = {}
    root = repo / "hermes" / "skills"
    if not root.is_dir():
        return names
    for skill in sorted(root.iterdir()):
        rel = f"hermes/skills/{skill.name}/SKILL.md"
        try:
            head = safe_repo_file(repo, rel, max_bytes=256 * 1024).decode("utf-8", "replace")
        except RegistryError:
            continue
        m = re.search(r"^name:\s*([a-z0-9-]+)\s*$", head.split("\n---", 1)[0], re.M)
        if m:
            names[m.group(1)] = rel
    return names


# ---------------------------------------------------------------- validation

def lock_index(lock: dict) -> Dict[Tuple[str, str], dict]:
    return {(f["repo"], f["path"]): f for f in lock["files"]}


def _lock_semantics(lock: dict) -> List[str]:
    errs: List[str] = []
    for key, rev in PINNED_REVISIONS.items():
        if lock["sources"][key]["revision"] != rev:
            errs.append(f"lock: source {key} revision {lock['sources'][key]['revision']} is not the pinned {rev}")
    seen = set()
    for f in lock["files"]:
        k = (f["repo"], f["path"])
        if k in seen:
            errs.append(f"lock: duplicate lock entry {f['repo']}:{f['path']}")
        seen.add(k)
        if f["repo"] == "pm-skills" and MOVED_STUB_RE.match(f["path"]) and f["status"] != "excluded-moved-stub":
            errs.append(f"lock: {f['path']} is a PM Skills moved stub and must stay excluded-moved-stub")
        if f["status"] == "excluded-moved-stub" and not (f["repo"] == "pm-skills" and MOVED_STUB_RE.match(f["path"])):
            errs.append(f"lock: {f['repo']}:{f['path']} is marked a moved stub but is not one")
        reuse = lock["sources"][f["repo"]]["reuse"]
        if f["status"] == "selected" and reuse != "adapt-with-notice":
            errs.append(f"lock: {f['repo']} is {reuse} (reference-only); {f['path']} cannot be selected for adaptation")
    for key, src in lock["sources"].items():
        if src["reuse"] == "adapt-with-notice" and lock_index(lock).get((key, "LICENSE"), {}).get("status") != "license-notice":
            errs.append(f"lock: adapted source {key} needs its LICENSE recorded as a license-notice entry")
    return errs


def _cycles(caps: Dict[str, dict]) -> List[str]:
    edges = {cid: sorted({i["producer"] for i in c["inputs"] if i["producer"]}) for cid, c in caps.items()}
    errs, state = [], {}

    def visit(node: str, trail: List[str]) -> None:
        state[node] = 1
        for nxt in edges.get(node, ()):
            if nxt not in edges:
                continue
            if state.get(nxt) == 1:
                errs.append("registry: producer cycle " + " -> ".join(trail[trail.index(nxt):] + [nxt]) if nxt in trail
                            else f"registry: producer cycle through {nxt}")
            elif not state.get(nxt):
                visit(nxt, trail + [nxt])
        state[node] = 2

    for cid in edges:
        if not state.get(cid):
            visit(cid, [cid])
    return errs


def _capability_semantics(c: dict, caps: Dict[str, dict], lock: dict, lidx: Dict[Tuple[str, str], dict],
                          reserved: set, kit_names: Dict[str, str], repo: Path) -> List[str]:
    cid = c["id"]
    errs: List[str] = []
    e = lambda msg: errs.append(f"{cid}: {msg}")  # noqa: E731
    src, ex, impl = c["source"], c["execution"], c["implementation"]

    ns = cid.split(".", 1)[0]
    if src["kind"] not in ID_NAMESPACE[ns]:
        e(f"id namespace {ns}. requires source kind {ID_NAMESPACE[ns][0]}, not {src['kind']}")
    if ns in ID_UPSTREAM and src["upstream"] != ID_UPSTREAM[ns]:
        e(f"id namespace {ns}. requires upstream {ID_UPSTREAM[ns]}")

    name = c["exposed_skill_name"]
    if name is not None:
        if name in reserved:
            e(f"exposed skill name {name!r} is reserved (would shadow an existing or upstream bare name)")
        if name in kit_names and kit_names[name] not in impl["paths"]:
            e(f"exposed skill name {name!r} collides with existing kit skill {kit_names[name]}")
        want = NAME_PREFIX.get(src["upstream"] or "") if src["kind"] == "adapted" else None
        if want and not name.startswith(want):
            e(f"adapted {src['upstream']} skill name {name!r} must use the {want!r} prefix")
        if not want and name.startswith(tuple(NAME_PREFIX.values())):
            e(f"kit skill name {name!r} must not use a third-party prefix")

    if ex["default_mode"] not in ex["modes"]:
        e(f"default_mode {ex['default_mode']!r} is not one of modes {ex['modes']}")
    both = set(c["tools"]["required"]) & set(c["tools"]["optional"])
    if both:
        e(f"tool needs {sorted(both)} are both required and optional")

    side = set(c["side_effects"])
    for tool in sorted((set(c["tools"]["required"]) | set(c["tools"]["optional"])) & set(TOOL_SIDE_EFFECT)):
        if TOOL_SIDE_EFFECT[tool] not in side:
            e(f"tool need {tool} requires side effect {TOOL_SIDE_EFFECT[tool]!r} (side effects must not understate the tools)")
    approvals = {a["action"] for a in c["approvals"]}
    if "none" in side and len(side) > 1:
        e("side effect 'none' cannot be combined with other side effects")
    if ex["writer"] == "none" and side & SOURCE_CHANGING:
        e(f"read-only capability (writer none) cannot declare {sorted(side & SOURCE_CHANGING)}")
    if side & SOURCE_CHANGING:
        if ex["writer"] != "existing-claude-route":
            e("source changes require writer existing-claude-route")
        if not approvals & WRITE_APPROVALS:
            e("source changes require a source-modification or commit-push approval")
    if "network-egress-public" in side and "company-private" in c["scope"]["data_classes"] \
            and "external-research-if-private-context" not in approvals:
        e("public egress with company-private data requires approval external-research-if-private-context")

    if src["kind"] == "adapted":
        up = src["upstream"]
        lsrc = lock["sources"].get(up or "")
        if lsrc is None:
            e("adapted capability needs an upstream source")
        else:
            if lsrc["reuse"] != "adapt-with-notice":
                e(f"upstream {up} is {lsrc['reuse']} (reference-only); it cannot be adapted")
            if src["revision"] != lsrc["revision"]:
                e(f"source revision {src['revision']} does not match the lock revision {lsrc['revision']}")
            if lsrc["license_spdx"] and src["license"] != lsrc["license_spdx"]:
                e(f"license {src['license']} does not match the lock license {lsrc['license_spdx']}")
            if not src["paths"]:
                e("adapted capability needs at least one locked upstream path")
            for p in src["paths"]:
                f = lidx.get((up, p))
                if f is None:
                    e(f"source path {up}:{p} is not in the lock")
                elif f["status"] == "excluded-moved-stub":
                    e(f"source path {up}:{p} is a moved stub and cannot be used")
                elif f["status"] != "selected":
                    e(f"source path {up}:{p} is {f['status']}, not selected")
            if upstream_digest(lidx, up, src["paths"]) != src["content_digest"]:
                e("content_digest does not match the locked upstream file hashes")
    else:
        if src["upstream"] or src["revision"] or src["paths"] or src["content_digest"]:
            e(f"{src['kind']} capability must not name upstream paths, revision or content_digest")
    for ref in src["inspiration_refs"]:
        f = lidx.get((ref["upstream"], ref["path"]))
        if f is None or f["status"] not in ("reference-only", "selected"):
            e(f"inspiration ref {ref['upstream']}:{ref['path']} is not in the lock as reference-only")

    if c["lifecycle"] == "planned":
        if impl["paths"] or impl["digest"]:
            e("planned capability must not claim implementation files or a digest")
    else:
        if not impl["paths"] or not impl["digest"]:
            e(f"{c['lifecycle']} capability needs implementation paths and digest")
        else:
            try:
                if implementation_digest(repo, impl["paths"]) != impl["digest"]:
                    e("implementation digest mismatch (files changed since the record was written)")
            except RegistryError as exc:
                e(f"implementation file: {exc}")

    for i in c["inputs"]:
        prod = i["producer"]
        if prod is None:
            continue
        if prod not in caps:
            e(f"input {i['name']}: unknown producer {prod}")
            continue
        if not {o["kind"] for o in caps[prod]["outputs"]} & set(i["kinds"]):
            e(f"input {i['name']}: producer {prod} does not output any of {i['kinds']}")
        mode = i.get("producer_mode")
        if mode and mode not in caps[prod]["execution"]["modes"]:
            e(f"input {i['name']}: producer_mode {mode!r} is not a mode of {prod}")
    for h in c.get("handoff_to", []):
        if h not in caps:
            e(f"unknown handoff target {h}")
    return errs


def validate_registry(registry: Any, lock: Any, *, repo: Path = REPO) -> List[str]:
    """Every schema and semantic error of a registry + lock pair (empty list = valid)."""
    errs = schema_errors(registry, "registry.schema.json", "registry") + schema_errors(lock, "upstreams-lock.schema.json", "lock")
    if errs:
        return errs
    errs += _lock_semantics(lock)
    caps: Dict[str, dict] = {}
    names: Dict[str, str] = {}
    for c in registry["capabilities"]:
        if c["id"] in caps:
            errs.append(f"registry: duplicate capability id {c['id']}")
        caps[c["id"]] = c
        n = c["exposed_skill_name"]
        if n is not None:
            if n in names:
                errs.append(f"registry: duplicate exposed skill name {n} ({names[n]}, {c['id']})")
            names[n] = c["id"]
    lidx = lock_index(lock)
    reserved = set(registry["reserved_skill_names"])
    kit_names = kit_skill_names(repo)
    for c in registry["capabilities"]:
        errs += _capability_semantics(c, caps, lock, lidx, reserved, kit_names, repo)
    errs += _cycles(caps)
    return errs


def compare_research_lock(lock: dict, research_raw: bytes) -> List[str]:
    """Check that every kit lock entry is copied verbatim from the recorded research lock."""
    errs: List[str] = []
    rec = lock["research_lock"]
    if sha256_bytes(research_raw) != rec["sha256"]:
        errs.append(f"research lock sha256 {sha256_bytes(research_raw)} is not the recorded {rec['sha256']}")
    try:
        research = parse_json(research_raw.decode("utf-8"))
    except (RegistryError, UnicodeDecodeError) as exc:
        return errs + [f"research lock unreadable: {exc}"]
    rfiles = {(f["repo"], f["path"]): f for f in research.get("files", [])}
    if len(research.get("files", [])) != rec["files_recorded"]:
        errs.append(f"research lock has {len(research.get('files', []))} files, lock records {rec['files_recorded']}")
    if research.get("research_date") != rec["research_date"]:
        errs.append("research_date differs from the research lock")
    for key, src in lock["sources"].items():
        r = research.get("sources", {}).get(key, {})
        if r.get("revision") != src["revision"] or r.get("repository") != src["repository"]:
            errs.append(f"source {key}: repository/revision differs from the research lock")
    for f in lock["files"]:
        r = rfiles.get((f["repo"], f["path"]))
        if r is None:
            errs.append(f"{f['repo']}:{f['path']} is not in the research lock")
        elif r.get("sha256") != f["sha256"] or r.get("bytes") != f["bytes"]:
            errs.append(f"{f['repo']}:{f['path']} hash/size differs from the research lock")
    return errs


def load_registry(caps_dir: Path = CAPABILITIES_DIR) -> Tuple[dict, dict]:
    return load_json(caps_dir / "registry.json"), load_json(caps_dir / "upstreams.lock.json")




# ---------------------------------------------------------------- evidence verdicts

PRIMARY_SOURCE_KINDS = {"primary-web", "document", "dataset"}


def evaluate_check(e: dict) -> Tuple[str, str]:
    """(pass|fail|inconclusive, reason) for one Evidence v1 record. Only an observed,
    clean, non-empty run passes: skips, zero counts, missing or nonzero exit status,
    and fixture/reported/simulated records never do."""
    if e["classification"] != "observed":
        return "inconclusive", f"{e['classification']} evidence is not an observed check"
    if e["status"] in ("skipped", "not-run"):
        return "inconclusive", f"check {e['status']}"
    if e["status"] in ("failed", "error"):
        return "fail", f"check {e['status']}"
    if e["exit_code"] not in (0, None):
        return "fail", f"nonzero exit {e['exit_code']}"
    if e["method"] == "command" and e["exit_code"] is None:
        return "inconclusive", "command evidence without an exit status"
    n = e["counts"]
    if n["failed"] or n["errors"]:
        return "fail", f"{n['failed']} failed, {n['errors']} errors"
    if n["skipped"]:
        return "inconclusive", f"{n['skipped']} skipped (skipped tests are not passes)"
    if not n["passed"]:
        return "inconclusive", "zero checks ran"
    return "pass", f"{n['passed']} passed"


# ---------------------------------------------------------------- readiness

DIMENSIONS = ("defined", "installed", "learned_current", "tools_available", "authorized",
              "runtime_accepted", "executed", "outcome_verified")
PROTECTED_DIMENSIONS = frozenset({"authorized", "runtime_accepted"})
PREREQUISITES = {
    "installed": ("defined",), "learned_current": ("installed",), "tools_available": ("installed",),
    "authorized": ("defined",), "runtime_accepted": ("installed", "authorized"),
    "executed": ("runtime_accepted", "tools_available"), "outcome_verified": ("executed",),
}
EXECUTION_PREREQUISITES = ("defined", "installed", "learned_current", "tools_available", "authorized", "runtime_accepted")

# Observer classes whose authorized/runtime_accepted observations count. Code, never
# input: empty until an accepted protected runtime (the secure-execution repair) exists.
TRUSTED_PROTECTED_OBSERVERS: frozenset = frozenset()


@dataclass(frozen=True)
class Observation:
    """One injected observer's reading of one dimension (state 'yes' or 'no')."""
    state: str
    basis: str
    evidence_class: str  # observed | fixture
    evidence_ids: Tuple[str, ...] = ()


def _dim(state: str, basis: str, evidence: Sequence[str] = ()) -> Dict[str, Any]:
    return {"state": state, "basis": basis, "evidence": list(evidence)}


def evaluate_readiness(c: dict, *, repo: Path = REPO, observer: Any = None,
                       reported: Sequence[dict] = ()) -> Dict[str, Any]:
    """Eight separate readiness dimensions for one capability record.

    ``defined`` is computed from the record (planned = no; otherwise the
    implementation digest must match the files). Every other dimension is
    'unknown' unless an injected observer reads it. Fixture observations stay
    'fixture'; evidence records supplied as input stay 'reported'; protected
    dimensions count only from a class in TRUSTED_PROTECTED_OBSERVERS. A 'yes'
    whose prerequisite is not 'yes' becomes 'inconsistent'. Nothing is inferred
    from a file merely existing.
    """
    dims: Dict[str, Dict[str, Any]] = {}
    if c["lifecycle"] == "planned":
        dims["defined"] = _dim("no", f"planned: adapter not implemented yet (milestone {c['milestone']})")
    else:
        try:
            ok = implementation_digest(repo, c["implementation"]["paths"]) == c["implementation"]["digest"]
            dims["defined"] = _dim("yes", "registry record + matching implementation digest") if ok else \
                _dim("no", "implementation digest mismatch")
        except RegistryError as exc:
            dims["defined"] = _dim("no", f"implementation unreadable: {exc}")
    applicable_learned = c["scope"]["requires_learned_knowledge"]
    by_dim: Dict[str, List[str]] = {}
    for e in reported:
        if e.get("subject", {}).get("capability_id") == c["id"] and e.get("dimension"):
            by_dim.setdefault(e["dimension"], []).append(e["evidence_id"])
    trusted = observer is not None and type(observer) in TRUSTED_PROTECTED_OBSERVERS
    for d in DIMENSIONS[1:]:
        if d == "learned_current" and not applicable_learned:
            dims[d] = _dim("not_applicable", "capability needs no learned knowledge")
            continue
        obs = observer.observe(c["id"], d) if observer is not None else None
        if obs is not None and d in PROTECTED_DIMENSIONS and not trusted:
            dims[d] = _dim("unknown", f"protected dimension: {getattr(observer, 'name', 'observer')} is not an accepted protected-runtime observer")
        elif obs is not None and obs.evidence_class != "observed":
            dims[d] = _dim("fixture", f"{obs.basis} ({obs.evidence_class} evidence is not install/runtime proof)", obs.evidence_ids)
        elif obs is not None and obs.state in ("yes", "no"):
            dims[d] = _dim(obs.state, obs.basis, obs.evidence_ids)
        elif d in by_dim:
            dims[d] = _dim("reported", "evidence supplied as input: reported, not observed by the planner", by_dim[d])
        else:
            dims[d] = _dim("unknown", "no observation")
    for d in DIMENSIONS[1:]:
        if dims[d]["state"] != "yes":
            continue
        for pre in PREREQUISITES[d]:
            pstate = dims[pre]["state"]
            if pstate not in ("yes", "not_applicable"):
                dims[d] = _dim("inconsistent", f"claimed yes but prerequisite {pre} is {pstate}", dims[d]["evidence"])
                break
    executable = all(dims[d]["state"] in ("yes", "not_applicable") for d in EXECUTION_PREREQUISITES)
    return {"capability_id": c["id"], "dimensions": dims, "executable_now": executable,
            "outcome_verified": dims["outcome_verified"]["state"] == "yes"}


# ---------------------------------------------------------------- capability results

def validate_result(r: Any, registry: dict) -> List[str]:
    """Schema + semantic errors of one CapabilityResult v1 (empty = valid)."""
    errs = schema_errors(r, "capability-result.schema.json", "result")
    if errs:
        return errs
    caps = {c["id"]: c for c in registry["capabilities"]}
    c = caps.get(r["capability_id"])
    if c is None:
        return [f"result: unknown capability {r['capability_id']}"]
    if r["capability_version"] != c["version"]:
        errs.append(f"result: capability_version {r['capability_version']} is not the registry {c['version']}")
    if r["source_digest"] != c["source"]["content_digest"]:
        errs.append("result: source_digest does not match the registry record")
    if r["implementation_digest"] != c["implementation"]["digest"]:
        errs.append("result: implementation_digest does not match the registry record")
    if r["mode"] not in c["execution"]["modes"]:
        errs.append(f"result: mode {r['mode']!r} is not a mode of {c['id']}")
    sources: Dict[str, dict] = {}
    for src in r["sources"]:
        if src["source_id"] in sources:
            errs.append(f"result: duplicate source {src['source_id']}")
        sources[src["source_id"]] = src
        if src["kind"] == "primary-web" and "retrieved_at" not in src:
            errs.append(f"result: primary-web source {src['source_id']} needs retrieved_at")
    for cl in r["claims"]:
        cited = []
        for sid in cl["source_ids"]:
            if sid not in sources:
                errs.append(f"result: claim {cl['claim_id']} cites unknown source {sid}")
            else:
                cited.append(sources[sid])
        if cl["classification"] == "observed":
            if any(s["kind"] == "synthetic-fixture" for s in cited):
                errs.append(f"result: observed claim {cl['claim_id']} cites a synthetic fixture (synthetic data is never an observation)")
            if not any(s["kind"] in PRIMARY_SOURCE_KINDS for s in cited):
                errs.append(f"result: observed claim {cl['claim_id']} cites no primary source")
    for e in r["checks"]:
        sub = e["subject"].get("capability_id")
        if sub and sub != r["capability_id"]:
            errs.append(f"result: check {e['evidence_id']} is about {sub}, not {r['capability_id']}")
    if r["status"] == "completed":
        if not r["checks"]:
            errs.append("result: completed with no checks")
        for e in r["checks"]:
            verdict, why = evaluate_check(e)
            if verdict != "pass":
                errs.append(f"result: completed but check {e['evidence_id']} is not a pass ({why})")
        if r["approval_requests"]:
            errs.append("result: completed with a pending approval request")
    if r["status"] == "needs_input" and not (r["unknowns"] or r["approval_requests"]):
        errs.append("result: needs_input must name the missing input (unknowns or approval_requests)")
    return errs


# ---------------------------------------------------------------- company / project manifests

BOUNDARY_NAMES = ("repositories", "documents", "memory", "connected_apps", "browser_sessions", "credentials",
                  "outputs", "network", "model_inference", "cross_company_sharing")


def _strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _foreign_refs(owner: str, doc: dict, tenants: Iterable[str]) -> List[str]:
    others = [t for t in tenants if t != owner]
    hits = []
    for value in _strings(doc):
        for t in others:
            if re.search(rf"(?<![a-z0-9-]){re.escape(t)}(?![a-z0-9])", value):
                hits.append(f"{value!r} names {t}")
    return hits


def validate_manifests(companies: Sequence[Any], projects: Sequence[Any], registry: dict, *,
                       examples: bool = False) -> List[str]:
    """Schema + cross-reference errors for company/project manifests (empty = valid).

    Manifests state requirements and upper bounds only; validating one grants
    nothing and never marks a boundary enforced."""
    errs: List[str] = []
    for i, c in enumerate(companies):
        errs += schema_errors(c, str(COMPANY_SCHEMA), f"company[{i}]")
    for i, p in enumerate(projects):
        errs += schema_errors(p, str(PROJECT_SCHEMA), f"project[{i}]")
    if errs:
        return errs
    cap_ids = {c["id"] for c in registry["capabilities"]}
    by_company: Dict[str, dict] = {}
    for c in companies:
        cid = c["company_id"]
        if cid in by_company:
            errs.append(f"company {cid}: duplicate company_id")
        by_company[cid] = c
        for field in ("knowledge_scope", "memory_scope"):
            if c[field] != f"company/{cid}":
                errs.append(f"company {cid}: {field} {c[field]!r} must be company/{cid}")
        if examples and not c["synthetic"]:
            errs.append(f"company {cid}: example manifests must be synthetic (real manifests live outside Git)")
    tenants = list(by_company)
    seen_projects: Dict[str, dict] = {}
    for p in projects:
        pid, owner = p["project_id"], p["company_id"]
        if pid in seen_projects:
            errs.append(f"project {pid}: duplicate project_id")
        seen_projects[pid] = p
        if owner not in by_company:
            errs.append(f"project {pid}: unknown company {owner}")
        elif pid not in by_company[owner]["project_ids"]:
            errs.append(f"project {pid}: company {owner} does not list it")
        for cap_id in p["approved_capability_ids"]:
            if cap_id not in cap_ids:
                errs.append(f"project {pid}: unknown capability {cap_id}")
        if examples and not p["synthetic"]:
            errs.append(f"project {pid}: example manifests must be synthetic")
        errs += [f"project {pid}: cross-company reference {h}" for h in _foreign_refs(owner, p, tenants)]
    for cid, c in by_company.items():
        for pid in c["project_ids"]:
            if projects and pid not in seen_projects:
                errs.append(f"company {cid}: lists unknown project {pid}")
        errs += [f"company {cid}: cross-company reference {h}" for h in _foreign_refs(cid, c, tenants)]
    return errs


def company_boundary_report(company: dict) -> Dict[str, Dict[str, str]]:
    """Enforcement status of every required boundary. Always 'not-enforced' until an
    accepted protected runtime exists: a manifest cannot certify its own enforcement."""
    return {b: {"status": "not-enforced", "policy_ref": company["boundaries"][b]["policy_ref"],
                "requirement": company["boundaries"][b]["requirement"],
                "reason": "no accepted protected company execution runtime"} for b in BOUNDARY_NAMES}




# ---------------------------------------------------------------- planning

MAX_STAGES = 8
PERMISSION_EFFECT = "none: tool needs and approvals are requirements for the trusted policy layer, never grants"
SELECTOR_LABELS = {
    "model-router": "model-router current-task selection (closed IDs, reported provenance; validated here, not re-classified)",
    "explicit-offline": "explicit offline evaluation input (not model-router routing)",
}
STATUS_ORDER = ("invalid", "refused", "blocked", "needs_input", "planned")
# build_plan decides reuse from index-declared digests only; artifact bytes are checked (and the label
# raised to "verified") only by capability-plan.py --artifact-root via mark_contents_verified.
CONTENT_UNVERIFIED = "unverified"


def producer_record(c: dict, run_ref: Optional[str] = None) -> Dict[str, Any]:
    """Artifact ``produced_by`` for capability record ``c``: id, version and the registry's recorded
    implementation (and upstream source) digests. Recorded provenance, not proof of what ran."""
    rec: Dict[str, Any] = {"capability_id": c["id"], "capability_version": c["version"]}
    if run_ref:
        rec["run_ref"] = run_ref
    if c["implementation"]["digest"]:
        rec["implementation_digest"] = c["implementation"]["digest"]
    if c["source"]["content_digest"]:
        rec["source_digest"] = c["source"]["content_digest"]
    return rec


def _producer_verdict(prod: Optional[dict], registry_caps: Dict[str, dict]) -> Tuple[bool, str]:
    """Currency of an artifact's own producer binding. No producer (requester-supplied) has nothing
    to bind. Otherwise the producer must still exist, have the same version, and the recorded
    implementation/source digests must equal the registry's; a missing digest is legacy-unverified."""
    if prod is None:
        return True, "current"
    cid = prod["capability_id"]
    c = registry_caps.get(cid)
    if c is None:
        return False, f"stale: producer {cid} is not in the registry (removed or renamed)"
    if prod["capability_version"] != c["version"]:
        return False, f"stale: producer {cid} changed version ({prod['capability_version']} -> {c['version']})"
    want = (("implementation_digest", c["implementation"]["digest"], "implementation"),
            ("source_digest", c["source"]["content_digest"], "upstream source"))
    for field, registry_digest, label in want:
        if registry_digest is None and field not in prod:
            continue
        if field not in prod:
            return False, f"legacy-unverified: no recorded {label} digest for producer {cid}; not reusable"
        if prod[field] != registry_digest:
            return False, f"invalid: producer {cid} {label} changed without a version bump (recorded digest differs)"
    return True, "current"


def _artifact_currency(arts: Sequence[dict], registry_caps: Dict[str, dict]) -> Dict[str, Tuple[bool, str]]:
    """artifact_id -> (current, reason). Current = the producer binding holds (_producer_verdict) and
    every source ref resolves to an indexed, itself-current artifact with the same digest, the same
    company/project and a data class the referencing artifact may use (so the whole closure stays in scope)."""
    by_id = {a["artifact_id"]: a for a in arts}
    memo: Dict[str, Tuple[bool, str]] = {}

    def in_scope(src: dict, a: dict) -> bool:
        return ((src["company_id"], src["project_id"]) == (a["company_id"], a["project_id"])
                and _class_compatible(src["data_classification"], a["data_classification"]))

    def check(aid: str, trail: Tuple[str, ...]) -> Tuple[bool, str]:
        if aid in memo:
            return memo[aid]
        if aid in trail:
            return False, "source reference cycle"
        a = by_id[aid]
        verdict = _producer_verdict(a["produced_by"], registry_caps)
        for ref in a["source_refs"]:
            if not verdict[0]:
                break
            src = by_id.get(ref["artifact_id"])
            if src is None:
                verdict = (False, f"unverifiable: source {ref['artifact_id']} is not in the index")
            elif not in_scope(src, a):
                # Checked before anything else about the source, and reported without its ID, digest or
                # status: a chain may never reach into another tenant's or a stricter class's artifacts.
                verdict = (False, "stale: a source reference leaves this artifact's company/project/data-class scope")
            elif src["content_digest"] != ref["digest"]:
                verdict = (False, f"source {ref['artifact_id']} changed (digest differs)")
            else:
                ok, why = check(src["artifact_id"], trail + (aid,))
                if not ok:
                    verdict = (False, f"source {ref['artifact_id']} is stale ({why})")
        memo[aid] = verdict
        return verdict

    return {aid: check(aid, ()) for aid in by_id}


def _reusable(a: dict, envelope: dict, currency: Dict[str, Tuple[bool, str]]) -> bool:
    if a["review_status"] != "accepted" or not currency[a["artifact_id"]][0]:
        return False
    if a["company_id"] != envelope["company_id"] or a["project_id"] != envelope["project_id"]:
        return False
    return _class_compatible(a["data_classification"], envelope["data_classification"])


PRIVATE_RUNTIME_REFUSAL = ("company-private data requires an accepted protected company boundary below the prompt layer; "
                           "none is accepted (source-only milestone), so private execution is refused")


def _class_compatible(artifact_class: str, task_class: str) -> bool:
    if artifact_class == "synthetic" and task_class != "synthetic":
        return False  # a synthetic label never becomes a real observation
    if artifact_class == "company-private" and task_class != "company-private":
        return False
    return True


def _empty_plan(task_id: Optional[str], selection_id: Optional[str], registry: dict) -> Dict[str, Any]:
    return {"schema_version": 1, "plan_kind": "capability-plan", "status": "planned", "reasons": [], "errors": [],
            "task_id": task_id, "selection_id": selection_id, "registry_version": registry.get("registry_version", "?"),
            "selection_provenance": {"kind": "none", "outcome": "none", "label": "no selection"},
            "stages": [], "reused_artifacts": [], "stale_artifacts": [], "needs_input": [], "advisory_profiles": [],
            "company_boundaries": {}, "executable_now": False, "permission_effect": PERMISSION_EFFECT, "writes": []}


def build_plan(registry: dict, selection: Any, envelope: Any, artifacts: Any = None, *, repo: Path = REPO,
               observer: Any = None, reported: Sequence[Any] = (), project: Any = None,
               companies: Optional[Sequence[Any]] = None, specialist_routes: Any = None) -> Dict[str, Any]:
    """Describe the smallest capability closure for one task. Pure: reads only the
    registry's implementation files for digests; executes, installs and grants nothing.

    ``selection`` holds closed capability IDs from the existing model-router seam or an
    explicit offline evaluation; IDs outside the registry are rejected, nothing here
    classifies task text. Required inputs are satisfied, in order, by the task
    envelope, an accepted current artifact of this company/project, a stage already in
    the plan, or the input's declared producer; otherwise they are reported as
    needs_input. ``handoff_to`` targets are never added."""
    plan = _empty_plan(selection.get("task_id") if isinstance(selection, dict) else None,
                       selection.get("selection_id") if isinstance(selection, dict) else None, registry)
    errs = schema_errors(selection, "selection.schema.json", "selection") + schema_errors(envelope, "task-envelope.schema.json", "task")
    arts_doc = artifacts if artifacts is not None else {"schema_version": 1, "artifacts": []}
    errs += schema_errors(arts_doc, "artifact.schema.json", "artifacts")
    if reported:
        errs += schema_errors({"schema_version": 1, "evidence": list(reported)}, "evidence.schema.json", "evidence")
    if companies:
        errs += validate_manifests(companies, [], registry)
    if project is not None:
        errs += schema_errors(project, str(PROJECT_SCHEMA), "project")
    if errs:
        plan.update(status="invalid", errors=errs, reasons=["input failed schema validation"])
        return plan

    caps = {c["id"]: c for c in registry["capabilities"]}
    arts = arts_doc["artifacts"]
    by_id: Dict[str, dict] = {}
    for a in arts:
        if a["artifact_id"] in by_id:
            errs.append(f"artifacts: duplicate artifact_id {a['artifact_id']}")
        by_id[a["artifact_id"]] = a
    if selection["task_id"] != envelope["task_id"]:
        errs.append(f"selection is for task {selection['task_id']}, not {envelope['task_id']}")
    for cid in selection["capability_ids"]:
        if cid not in caps:
            errs.append(f"selection: unknown capability id {cid} (closed catalog)")
    for cid, mode in selection.get("modes", {}).items():
        if cid not in selection["capability_ids"]:
            errs.append(f"selection: mode given for unselected capability {cid}")
        elif cid in caps and mode not in caps[cid]["execution"]["modes"]:
            errs.append(f"selection: {mode!r} is not a mode of {cid}")
    for ref in envelope["input_artifacts"]:
        a = by_id.get(ref["artifact_id"])
        if a is None:
            errs.append(f"task: input artifact {ref['artifact_id']} is not in the artifact index")
        elif a["content_digest"] != ref["digest"]:
            errs.append(f"task: input artifact {ref['artifact_id']} digest differs from the index")
    if errs:
        plan.update(status="invalid", errors=errs, reasons=["selection/task/artifact cross-check failed"])
        return plan

    sel = selection["selector"]
    plan["selection_provenance"] = {"kind": sel["kind"], "outcome": sel["outcome"], "label": SELECTOR_LABELS[sel["kind"]]}
    if "decision_ref" in sel:
        plan["selection_provenance"]["decision_ref"] = sel["decision_ref"]
        if sel["decision_ref"].startswith("router-fixture:"):
            plan["selection_provenance"]["label"] += "; FIXTURE classifier answer (offline evaluation, not a live selection)"
    routes = specialist_routes if specialist_routes is not None else selection.get("specialist_routes", [])
    plan["advisory_profiles"] = [dict(r, readiness_note="model-router knowledge status reflects skill presence, not learned_current; advisory only")
                                 for r in routes]
    reasons: List[str] = []
    statuses = {"planned"}

    # Policy before planning: nothing below runs for a refused task.
    company_ids = set(envelope["companies_involved"]) | ({envelope["company_id"]} if envelope["company_id"] else set())
    if envelope["data_classification"] == "company-private":
        reasons.append(PRIVATE_RUNTIME_REFUSAL)
        if len(company_ids) > 1:
            reasons.append("cross-company private context is refused: no shared unrestricted context may combine companies")
        statuses.add("refused")
    if envelope["company_id"] and envelope["companies_involved"] and envelope["company_id"] not in envelope["companies_involved"]:
        reasons.append("task company_id is not among companies_involved")
        statuses.add("refused")
    if companies:
        by_company = {c["company_id"]: c for c in companies}
        comp = by_company.get(envelope["company_id"] or "")
        if envelope["company_id"] and comp is None:
            reasons.append(f"company {envelope['company_id']} has no manifest")
            statuses.add("refused")
        if comp is not None:
            if envelope["requester_ref"] not in comp["authorized_requesters"]:
                reasons.append("requester is not authorized for this company")
                statuses.add("refused")
            if envelope["destination_ref"] not in comp["authorized_destinations"]:
                reasons.append("destination is not authorized for this company")
                statuses.add("refused")
            if envelope["project_id"] and envelope["project_id"] not in comp["project_ids"]:
                reasons.append(f"project {envelope['project_id']} is not listed by company {comp['company_id']}")
                statuses.add("refused")
            plan["company_boundaries"] = company_boundary_report(comp)
    if project is not None:
        if project["company_id"] != envelope["company_id"] or project["project_id"] != envelope["project_id"]:
            reasons.append(f"project {project['project_id']} ({project['company_id']}) is not this task's project")
            statuses.add("refused")
        for cid in selection["capability_ids"]:
            if cid not in project["approved_capability_ids"]:
                reasons.append(f"{cid} is not in the project policy (approved_capability_ids narrows, never widens)")
                statuses.add("refused")
    for ref in envelope["input_artifacts"]:
        a = by_id[ref["artifact_id"]]
        if a["company_id"] != envelope["company_id"] or a["project_id"] != envelope["project_id"]:
            reasons.append(f"input artifact {a['artifact_id']} belongs to another company/project than this task")
            statuses.add("refused")
        elif not _class_compatible(a["data_classification"], envelope["data_classification"]):
            reasons.append(f"input artifact {a['artifact_id']} is {a['data_classification']} data; "
                           f"this task is {envelope['data_classification']}")
            statuses.add("refused")
    if sel["outcome"] != "ok":
        reasons.append(f"selector outcome {sel['outcome']}: no capability selected; nothing is substituted or fabricated")
        statuses.add("blocked")
    if "refused" in statuses or sel["outcome"] != "ok":
        plan.update(status=next(s for s in STATUS_ORDER if s in statuses), reasons=reasons)
        return plan

    currency = _artifact_currency(arts, caps)
    plan["stale_artifacts"] = [{"artifact_id": aid, "kind": by_id[aid]["kind"], "reason": why}
                               for aid, (ok, why) in currency.items() if not ok
                               and by_id[aid]["company_id"] == envelope["company_id"]
                               and by_id[aid]["project_id"] == envelope["project_id"]
                               and _class_compatible(by_id[aid]["data_classification"], envelope["data_classification"])]
    reusable = [a for a in arts if _reusable(a, envelope, currency)]
    referenced = {r["artifact_id"] for r in envelope["input_artifacts"]}

    def reuse_for_selected(c: dict, mode: Optional[str]) -> Optional[dict]:
        """A selected stage is skipped only for an output the task itself references (provenance:
        envelope.input_artifacts), from this capability (or no recorded producer), when no
        non-default mode was asked for (artifacts do not record a mode)."""
        if mode is not None and mode != c["execution"]["default_mode"]:
            return None
        cands = [a for a in reusable if a["artifact_id"] in referenced and a["kind"] == c["outputs"][0]["kind"]
                 and (a["produced_by"] is None or a["produced_by"]["capability_id"] == c["id"])]
        return max(cands, key=lambda a: (a["generation"], a["artifact_id"])) if cands else None

    def best_artifact(kinds: Sequence[str], only: Optional[set] = None) -> Optional[dict]:
        for k in kinds:
            cands = [a for a in reusable if a["kind"] == k and (only is None or a["artifact_id"] in only)]
            if cands:
                return max(cands, key=lambda a: (a["generation"], a["artifact_id"]))
        return None

    stages: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, set] = {}
    modes = selection.get("modes", {})

    def policy_allows(cid: str) -> bool:
        return project is None or cid in project["approved_capability_ids"]

    def reaches(src: str, dst: str) -> bool:
        seen, todo = set(), [src]
        while todo:
            n = todo.pop()
            if n == dst:
                return True
            if n not in seen:
                seen.add(n)
                todo.extend(edges.get(n, ()))
        return False

    def add_stage(cid: str, mode: Optional[str], because: str, required_by: Optional[str]) -> bool:
        if cid in stages:
            if required_by and required_by not in stages[cid]["required_by"]:
                stages[cid]["required_by"].append(required_by)
            return True
        if len(stages) >= MAX_STAGES:
            reasons.append(f"plan would exceed {MAX_STAGES} stages; narrow the selection")
            statuses.add("blocked")
            return False
        c = caps[cid]
        st = {"capability_id": cid, "version": c["version"], "mode": mode or c["execution"]["default_mode"], "action": "run",
              "added_because": because, "required_by": [required_by] if required_by else [], "after": [],
              "inputs": [{"name": i["name"], "kinds": list(i["kinds"]), "required": i["required"], "satisfied_by": None}
                         for i in c["inputs"]],
              "outputs": [o["kind"] for o in c["outputs"]]}
        reuse = reuse_for_selected(c, mode) if because == "selected" else None
        if reuse is not None:
            st["action"] = "reuse-existing"
            st["reused_output"] = {"artifact_id": reuse["artifact_id"], "digest": reuse["content_digest"], "kind": reuse["kind"],
                                   "content_check": CONTENT_UNVERIFIED}
            for i in st["inputs"]:
                i["note"] = "not needed: the accepted current output is reused"
        stages[cid] = st
        edges.setdefault(cid, set())
        return True

    def provider(kinds: Sequence[str], consumer: str) -> Optional[Tuple[str, str]]:
        for k in kinds:
            for sid, st in stages.items():
                if sid == consumer:
                    continue
                if st["action"] == "reuse-existing" and st["reused_output"]["kind"] == k:
                    return sid, k
                if st["action"] == "run" and k in st["outputs"]:
                    return sid, k
        return None

    def link(consumer: str, producer: str) -> bool:
        if reaches(consumer, producer):  # producer already depends on consumer
            return False
        edges[producer].add(consumer)
        if producer not in stages[consumer]["after"]:
            stages[consumer]["after"].append(producer)
        return True

    for cid in selection["capability_ids"]:
        add_stage(cid, modes.get(cid), "selected", None)

    needs: List[Dict[str, Any]] = []
    reused: Dict[str, Dict[str, Any]] = {}
    for st in stages.values():
        if st["action"] == "reuse-existing":
            reused[st["reused_output"]["artifact_id"]] = {**st["reused_output"], "used_by": st["capability_id"]}

    def resolve(required_pass: bool) -> None:
        progressed = True
        while progressed:
            progressed = False
            for cid in list(stages):
                st = stages[cid]
                if st["action"] != "run":
                    continue
                for spec, inp in zip(caps[cid]["inputs"], st["inputs"]):
                    if inp["satisfied_by"] is not None or spec["required"] != required_pass or inp.get("note"):
                        continue
                    if "task-envelope" in spec["kinds"]:
                        inp["satisfied_by"] = {"task_envelope": envelope["task_id"]}
                        continue
                    linked = cyclic = None

                    def use(art: dict, k: str) -> None:
                        inp["satisfied_by"] = {"artifact_id": art["artifact_id"], "digest": art["content_digest"], "kind": k,
                                               "content_check": CONTENT_UNVERIFIED}
                        reused.setdefault(art["artifact_id"], {"artifact_id": art["artifact_id"], "digest": art["content_digest"],
                                                               "kind": k, "used_by": cid, "content_check": CONTENT_UNVERIFIED})
                    # Preference: an artifact the task references, then a stage in this plan (fresh work
                    # wins over older index entries), then any other accepted current artifact.
                    for k in spec["kinds"]:
                        art = best_artifact([k], only=referenced)
                        if art is not None:
                            use(art, k)
                            linked = k
                            break
                        prov = provider([k], cid)
                        if prov is not None:
                            if link(cid, prov[0]):
                                inp["satisfied_by"] = {"stage": prov[0], "kind": k}
                                linked = k
                                break
                            cyclic = cyclic or prov[0]
                    if not linked:
                        for k in spec["kinds"]:
                            art = best_artifact([k])
                            if art is not None:
                                use(art, k)
                                linked = k
                                break
                    if linked:
                        continue
                    if cyclic and not required_pass:
                        inp["note"] = f"not linked to {cyclic}: it would create a cycle"
                        continue
                    if cyclic:
                        reasons.append(f"{cid}: required input {spec['name']} would create a stage cycle with {cyclic}")
                        statuses.add("blocked")
                        inp["note"] = "stage cycle"
                        continue
                    if required_pass and spec["producer"] and not policy_allows(spec["producer"]):
                        reasons.append(f"{cid}: producer {spec['producer']} for input {spec['name']} is not in the project policy "
                                       "(approved_capability_ids narrows automatically added producers too)")
                        needs.append({"capability_id": cid, "input": spec["name"], "kinds": list(spec["kinds"]),
                                      "reason": f"producer {spec['producer']} is not allowed by the project policy: "
                                                "supply an accepted current artifact"})
                        inp["note"] = "needs input"
                        continue
                    if required_pass and spec["producer"]:
                        if add_stage(spec["producer"], spec.get("producer_mode"), "dependency", cid):
                            progressed = True
                        continue
                    if required_pass:
                        needs.append({"capability_id": cid, "input": spec["name"], "kinds": list(spec["kinds"]),
                                      "reason": "required input has no accepted current artifact and no producer: the requester must supply it"})
                        inp["note"] = "needs input"
                    else:
                        inp["note"] = "optional input absent"

    resolve(True)
    resolve(False)

    # Order stages: dependencies first, ties in insertion (selection) order.
    order: List[str] = []
    pending = list(stages)
    while pending:
        ready = [c for c in pending if all(a in order for a in stages[c]["after"])]
        if not ready:  # defensive: link() never creates cycles
            order.extend(pending)
            break
        order.append(ready[0])
        pending.remove(ready[0])

    executable = True
    for cid in order:
        st, c = stages[cid], caps[cid]
        blockers: List[str] = []
        readiness = evaluate_readiness(c, repo=repo, observer=observer, reported=reported)
        if st["action"] == "run":
            dstate = readiness["dimensions"]["defined"]
            if dstate["state"] != "yes":
                blockers.append(f"not implemented / not defined: {dstate['basis']}")
            if envelope["data_classification"] not in c["scope"]["data_classes"]:
                blockers.append(f"data class {envelope['data_classification']} is not allowed by {cid}")
            if c["scope"]["company"] == "required" and not envelope["company_id"]:
                needs.append({"capability_id": cid, "input": "company_id", "kinds": [],
                              "reason": "company binding is required before this capability runs"})
            if c["scope"]["project"] == "required" and not envelope["project_id"]:
                needs.append({"capability_id": cid, "input": "project_id", "kinds": [],
                              "reason": "project binding is required before this capability runs"})
            if "network-egress-public" in c["side_effects"] and envelope["egress_class"] == "deny":
                blockers.append("task egress class is deny: public research cannot run; the evidence gap stays visible")
            executable = executable and readiness["executable_now"] and not blockers
        unavailable = [t for t in c["tools"]["optional"] if t == "web.public-read" and envelope["egress_class"] == "deny"]
        st.update({
            "tool_needs": {"required": list(c["tools"]["required"]), "optional": list(c["tools"]["optional"]),
                           "unavailable_optional": unavailable,
                           "note": "needs for the trusted policy intersection, not grants; availability is unverified here"},
            "side_effects": list(c["side_effects"]),
            "approvals_required": [dict(a) for a in c["approvals"]],
            "execution": {"method": c["execution"]["method"], "writer": c["execution"]["writer"]},
            "readiness": readiness, "blockers": blockers})
        if blockers:
            statuses.add("blocked")
    if needs:
        statuses.add("needs_input")
    plan.update(stages=[stages[c] for c in order], reused_artifacts=list(reused.values()), needs_input=needs,
                reasons=reasons, executable_now=any(stages[c]["action"] == "run" for c in order) and executable and len(statuses) == 1,
                status=next(s for s in STATUS_ORDER if s in statuses))
    return plan




# ---------------------------------------------------------------- selection seam and artifact contents

def selection_from_picks(task_id: str, selection_id: str, picks: Optional[Sequence[Any]], *, registry: dict,
                         selector_kind: str, outcome: str, eligible: Optional[Sequence[str]] = None,
                         max_picks: int = 4, decision_ref: Optional[str] = None) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Turn the closed-ID picks a selector returned (the model-router seam at coordinator
    evaluation time, or an offline evaluation) into a selection record. Mirrors the
    router's AI selection: unknown, ineligible, duplicate and over-cap picks are dropped
    and reported; a non-ok outcome selects nothing. No task text is read here."""
    known = {c["id"] for c in registry["capabilities"]}
    pool = set(eligible) if eligible is not None else known
    chosen: List[str] = []
    dropped: List[Dict[str, str]] = []
    if outcome == "ok":
        for raw in picks or ():
            cid = str(raw).strip().lower()
            if cid in chosen:
                dropped.append({"id": cid, "reason": "duplicate"})
            elif cid not in known:
                dropped.append({"id": "?", "reason": "unknown"})
            elif cid not in pool:
                dropped.append({"id": cid, "reason": "not-eligible"})
            elif len(chosen) >= max_picks:
                dropped.append({"id": cid, "reason": "cap"})
            else:
                chosen.append(cid)
    selector: Dict[str, Any] = {"kind": selector_kind, "outcome": outcome}
    if decision_ref:
        selector["decision_ref"] = decision_ref
    return ({"schema_version": 1, "selection_id": selection_id, "task_id": task_id, "selector": selector,
             "capability_ids": chosen}, dropped)


def verify_artifact_contents(index: dict, root: Path) -> List[str]:
    """Check each indexed artifact's bytes under ``root`` against its content digest,
    refusing escapes, symlinks and hard links. Read-only."""
    errs = []
    for a in index["artifacts"]:
        try:
            digest = sha256_bytes(safe_repo_file(root, a["location"]))
        except RegistryError as exc:
            errs.append(f"artifact {a['artifact_id']}: {exc}")
            continue
        if digest != a["content_digest"]:
            errs.append(f"artifact {a['artifact_id']}: content digest differs from the index")
    return errs


def mark_contents_verified(plan: Dict[str, Any]) -> None:
    """After verify_artifact_contents found every indexed artifact's bytes equal to its digest,
    relabel the plan's artifact decisions from "unverified" to "verified"."""
    entries = list(plan["reused_artifacts"])
    for st in plan["stages"]:
        if "reused_output" in st:
            entries.append(st["reused_output"])
        entries += [i["satisfied_by"] for i in st["inputs"] if isinstance(i["satisfied_by"], dict) and "artifact_id" in i["satisfied_by"]]
    for e in entries:
        e["content_check"] = "verified"


# ---------------------------------------------------------------- CLI (read-only)

def _cmd_validate(repo: Path) -> int:
    errs: List[str] = []
    try:
        reg, lock = load_registry(repo / "hermes" / "capabilities")
        for name in sorted(p.name for p in (repo / "hermes" / "capabilities" / "schemas").glob("*.json")):
            load_schema(name)
        companies = [load_json(p) for p in sorted((repo / "hermes/companies/examples").glob("*.json"))]
        projects = [load_json(p) for p in sorted((repo / "hermes/projects/examples").glob("*.json"))]
    except RegistryError as exc:
        print(json.dumps({"errors": [str(exc)]}, indent=2))
        return 1
    errs += validate_registry(reg, lock, repo=repo)
    if not errs:
        errs += validate_manifests(companies, projects, reg, examples=True)
    out = {"errors": errs, "capabilities": len(reg["capabilities"]), "lock_files": len(lock["files"]),
           "lifecycle": {k: sum(1 for c in reg["capabilities"] if c["lifecycle"] == k) for k in ("planned", "source-implemented", "existing")},
           "company_examples": [c["company_id"] for c in companies], "project_examples": [p["project_id"] for p in projects],
           "company_boundaries": {c["company_id"]: sorted({v["status"] for v in company_boundary_report(c).values()}) for c in companies}
           if not errs else {},
           "note": "source validation only: says nothing about installation, runtime acceptance or execution"}
    print(json.dumps(out, indent=2))
    return 1 if errs else 0


def _cmd_list(repo: Path) -> int:
    reg, _ = load_registry(repo / "hermes" / "capabilities")
    print(f"{'id':<28} {'milestone':<9} {'lifecycle':<18} {'defined':<8} exposed")
    for c in reg["capabilities"]:
        d = evaluate_readiness(c, repo=repo)["dimensions"]["defined"]["state"]
        print(f"{c['id']:<28} {c['milestone']:<9} {c['lifecycle']:<18} {d:<8} {c['exposed_skill_name'] or '-'}")
    return 0


def _cmd_readiness(repo: Path, ids: Sequence[str]) -> int:
    reg, _ = load_registry(repo / "hermes" / "capabilities")
    caps = [c for c in reg["capabilities"] if not ids or c["id"] in ids]
    if ids and len(caps) != len(set(ids)):
        print(f"unknown capability id in {list(ids)}", file=sys.stderr)
        return 2
    print(json.dumps([evaluate_readiness(c, repo=repo) for c in caps], indent=2))
    return 0


def _cmd_verify_lock(repo: Path, research: Path) -> int:
    _, lock = load_registry(repo / "hermes" / "capabilities")
    p = Path(os.path.abspath(research))
    try:
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > 64 * 1024 * 1024:
            raise RegistryError(f"{p}: not a regular, non-symlinked file of bounded size")
        raw = p.read_bytes()
    except OSError as exc:
        raise RegistryError(f"{p}: {exc.strerror}") from None
    errs = compare_research_lock(lock, raw)
    print(json.dumps({"errors": errs, "files_checked": len(lock["files"])}, indent=2))
    return 1 if errs else 0


def _cmd_digests(repo: Path) -> int:
    reg, lock = load_registry(repo / "hermes" / "capabilities")
    lidx = lock_index(lock)
    out = {}
    for c in reg["capabilities"]:
        src, impl = c["source"], c["implementation"]
        computed_impl = None
        if impl["paths"]:
            try:
                computed_impl = implementation_digest(repo, impl["paths"])
            except RegistryError as exc:
                computed_impl = f"error: {exc}"
        out[c["id"]] = {"content_digest": upstream_digest(lidx, src["upstream"], src["paths"]) if src["upstream"] else None,
                        "implementation_digest": computed_impl}
    print(json.dumps(out, indent=2))
    return 0


def _cmd_stamp_provenance(repo: Path, index_path: Path) -> int:
    """Maintainer helper: print ``index_path`` with produced_by digests set from the registry record for
    every artifact whose producer id and version match the registry. A maintainer assertion that the
    artifact still reflects the current implementation, not a verification. Prints; writes nothing."""
    reg, _ = load_registry(repo / "hermes" / "capabilities")
    index = load_json(index_path)
    errs = schema_errors(index, "artifact.schema.json", "artifacts")
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 2
    caps = {c["id"]: c for c in reg["capabilities"]}
    for a in index["artifacts"]:
        prod = a["produced_by"]
        c = caps.get(prod["capability_id"]) if prod else None
        if c is not None and prod["capability_version"] == c["version"]:
            a["produced_by"] = producer_record(c, prod.get("run_ref"))
    print(json.dumps(index, indent=2, ensure_ascii=False))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Capability registry: validate and inspect (read-only; installs and executes nothing)")
    ap.add_argument("--repo", default=str(REPO), help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="schemas, registry, lock and synthetic company/project examples")
    sub.add_parser("list", help="capabilities with milestone, lifecycle and computed 'defined'")
    r = sub.add_parser("readiness", help="computed readiness dimensions (no observer: unknown unless computed)")
    r.add_argument("--id", action="append", default=[])
    v = sub.add_parser("verify-lock", help="compare the kit lock with the research lock it was copied from")
    v.add_argument("--research-lock", required=True)
    sub.add_parser("digests", help="recompute content/implementation digests for maintainers")
    sp = sub.add_parser("stamp-provenance", help="print an artifact index with producer digests from the registry (writes nothing)")
    sp.add_argument("--index", required=True)
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    try:
        if args.cmd == "validate":
            return _cmd_validate(repo)
        if args.cmd == "list":
            return _cmd_list(repo)
        if args.cmd == "readiness":
            return _cmd_readiness(repo, args.id)
        if args.cmd == "verify-lock":
            return _cmd_verify_lock(repo, Path(args.research_lock))
        if args.cmd == "stamp-provenance":
            return _cmd_stamp_provenance(repo, Path(args.index))
        return _cmd_digests(repo)
    except RegistryError as exc:
        print(f"capability_registry: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
