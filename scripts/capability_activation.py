#!/usr/bin/env python3
"""Capability activation proposal: exact selected-file manifest, inert bundle, and a
REHEARSAL-ONLY apply/rollback (Milestone E).

  manifest            print the manifest computed from this source tree (read-only)
  check               compare it with hermes/capabilities/activation/manifest.json
  bundle --out DIR    write the bundle (manifest.json + files/) into a NEW directory
  preflight           read-only: bundle integrity, target checks, collisions, jsonschema
  apply               rehearsal only: install the bundle into a verified disposable home
  rollback            undo one apply journal (refuses changed files)
  verify              read-only: installed files equal the manifest

There is no default target and no live mode. ``apply``/``rollback`` accept only a
directory that proves it is a disposable rehearsal home (private 0700 directory owned by
the caller, a rehearsal marker whose nonce the caller repeats, no Hermes state files) and
that is neither the account's real ``~/.hermes`` nor ``$HERMES_HOME`` nor inside/around
them. Activating a live home is a separate, reviewed and approved step
(docs/capability-activation.md); nothing here restarts anything or installs packages.

Every bundled file is copied byte-for-byte with mode 0644 (no executable bit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
MANIFEST_PATH = REPO / "hermes" / "capabilities" / "activation" / "manifest.json"
MARKER = ".capability-rehearsal-home"
SKILLS_DEST = "skills/agent-ops-kit"
ROOT_DEST = "agent-ops-kit/capability-root"
FILE_MODE = 0o644
DIR_MODE = 0o755
MAX_FILE = 2 * 1024 * 1024
MAX_JOURNAL = 1024 * 1024
MAX_DEST = 200
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Files whose presence marks a real Hermes home; a rehearsal target must have none.
LIVE_STATE = ("config.yaml", ".env", "auth.json", "state.db", "hermes-agent", "sessions", "memories",
              "profiles", "kanban", "SOUL.md")
ROOT_SCRIPTS = ("capability_registry.py", "capability-plan.py", "capability_workflow.py", "capability_design.py",
                "capability_methods.py", "capability_research.py", "capability_selection.py", "capability-select.py",
                "config-get.py", "requirements-capabilities.txt")
ROOT_DATA = ("hermes/capabilities/registry.json", "hermes/capabilities/upstreams.lock.json",
             "hermes/capabilities/routing-recipes.json", "hermes/capabilities/routing-catalog.json",
             "hermes/companies/schema.json", "hermes/projects/schema.json",
             "LICENSE")
ROOT_GLOBS = ("hermes/capabilities/schemas/*.json", "hermes/companies/examples/*.json", "hermes/projects/examples/*.json")
JSONSCHEMA_RANGE = (">=4.10.3", "<5")
# Per-interpreter observations on ONE host; not a claim about any other host or interpreter.
# preflight/apply check the actual --python on the actual host every time.
JSONSCHEMA_RESOLVED = [
    {"interpreter": "/usr/bin/python3", "python": "3.12.3", "jsonschema": "4.10.3",
     "source": "host polymath-dev only: apt python3-jsonschema 4.10.3-2ubuntu1 (observed 2026-11-05 UTC, read-only)"},
    {"interpreter": "$HERMES_HOME/hermes-agent/venv/bin/python", "python": "3.12.3", "jsonschema": "4.26.0",
     "source": "host polymath-dev only: Hermes agent venv (observed 2026-11-05 UTC, read-only)"},
]


class ActivationError(Exception):
    """Refusal or invalid input; nothing was written past the reported point."""


def _registry():
    sys.path.insert(0, str(SCRIPTS))
    import capability_registry as cr  # noqa: E402
    return cr


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _regular(p: Path) -> os.stat_result:
    st = os.lstat(p)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_FILE:
        raise ActivationError(f"{p}: not a single-link regular file of bounded size")
    return st


# ---------------------------------------------------------------- manifest

def selected_sources(repo: Path = REPO) -> List[Tuple[str, str, str, List[str]]]:
    """``(source, destination, role, capability_ids)`` for every selected file, sorted by destination.
    Skills: the registry's source-implemented capability skills (whole skill directory).
    Capability root: the scripts, registry, lock, recipes, schemas, company/project contracts,
    licence, and the implementation files every non-planned capability's digest covers."""
    reg = json.loads((repo / "hermes/capabilities/registry.json").read_text())
    rows: Dict[str, Tuple[str, str, str, List[str]]] = {}

    def add(src: str, dest: str, role: str, cid: Optional[str] = None) -> None:
        row = rows.setdefault(dest, (src, dest, role, []))
        if cid and cid not in row[3]:
            row[3].append(cid)

    for c in reg["capabilities"]:
        if c["lifecycle"] == "source-implemented" and c["exposed_skill_name"]:
            sdir = repo / "hermes/skills" / c["exposed_skill_name"]
            for f in sorted(p for p in sdir.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
                rel = f.relative_to(repo).as_posix()
                add(rel, f"{SKILLS_DEST}/{f.relative_to(repo / 'hermes/skills').as_posix()}", "skill", c["id"])
        if c["lifecycle"] != "planned":
            for rel in c["implementation"]["paths"]:
                add(rel, f"{ROOT_DEST}/{rel}", "capability-root", c["id"])
    for name in ROOT_SCRIPTS:
        add(f"scripts/{name}", f"{ROOT_DEST}/scripts/{name}", "capability-root")
    for rel in ROOT_DATA:
        add(rel, f"{ROOT_DEST}/{rel}", "capability-root")
    for pattern in ROOT_GLOBS:
        for f in sorted(repo.glob(pattern)):
            rel = f.relative_to(repo).as_posix()
            add(rel, f"{ROOT_DEST}/{rel}", "capability-root")
    for src, dest, role, cids in list(rows.values()):
        if role == "skill":  # capability-root keeps a byte copy of each skill for implementation digests
            add(src, f"{ROOT_DEST}/{src}", "capability-root")
            for cid in cids:
                add(src, f"{ROOT_DEST}/{src}", "capability-root", cid)
    return [rows[d] for d in sorted(rows)]


def build_manifest(repo: Path = REPO) -> Dict[str, Any]:
    files = []
    for src, dest, role, cids in selected_sources(repo):
        p = repo / src
        _regular(p)
        files.append({"source": src, "destination": dest, "role": role, "sha256": sha256_file(p),
                      "size": p.stat().st_size, "mode": "0644", "capability_ids": sorted(cids)})
    reg = json.loads((repo / "hermes/capabilities/registry.json").read_text())
    skills = sorted({f["destination"].split("/")[2] for f in files if f["role"] == "skill"})
    body = {
        "schema_version": 1,
        "kind": "capability-activation-manifest",
        "status": "proposed (not applied to any live home)",
        "target": "HERMES_HOME of the default profile, relative destinations only",
        "skills": skills,
        "capability_ids": sorted(c["id"] for c in reg["capabilities"] if c["lifecycle"] == "source-implemented"),
        "source_root": {"destination": ROOT_DEST, "contract": "skills call $KIT/scripts/...; $KIT is this directory "
                        "(or a reviewed checkout); scripts resolve the registry and schemas relative to themselves"},
        "prerequisites": {"python": ">=3.10", "jsonschema": {"range": ",".join(JSONSCHEMA_RANGE),
                                                            "resolved": JSONSCHEMA_RESOLVED,
                                                            "install": "none: preflight refuses when absent or out of range"}},
        "excluded": ["hermes/capabilities/evals (fixtures; tests only)", "scripts/tests",
                     "hermes/plugins/model-router (installs through its existing make specialist-install path)",
                     "raw upstream files of gstack, PM Skills, SoloSquad; any RO-PNA text",
                     "gstack bin/, hooks, setup, preamble, update, telemetry, browser and ship programs",
                     "executable bits, symlinks, startup hooks, config.yaml or plugin enablement changes"],
        "config_impact": "none: no config.yaml, plugin, cron or service change; Hermes lists the new skills in sessions "
                         "started after the copy; no restart is performed or required by this bundle",
        "files": files,
    }
    body["bundle_digest"] = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return body


def manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


# ---------------------------------------------------------------- bundle

def write_bundle(out: Path, repo: Path = REPO) -> Dict[str, Any]:
    out = Path(os.path.abspath(out))
    if os.path.lexists(out):
        raise ActivationError(f"{out}: already exists; the bundle needs a new directory")
    if not out.parent.is_dir():
        raise ActivationError(f"{out.parent}: parent directory does not exist")
    manifest = build_manifest(repo)
    os.mkdir(out, 0o700)
    for f in manifest["files"]:
        dest = out / "files" / f["destination"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = (repo / f["source"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ActivationError(f"{f['source']}: changed while bundling")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(dest, FILE_MODE)
    (out / "manifest.json").write_bytes(manifest_bytes(manifest))
    return manifest


def load_bundle(bundle: Path) -> Tuple[Dict[str, Any], str]:
    """(manifest, manifest sha256) after checking every bundled file and that nothing else is there."""
    bundle = Path(os.path.abspath(bundle))
    raw = (bundle / "manifest.json").read_bytes()
    _regular(bundle / "manifest.json")
    manifest = _registry().parse_json(raw.decode("utf-8"))
    errs = _registry().schema_errors(manifest, "activation-manifest.schema.json", "manifest")
    if errs:
        raise ActivationError("manifest: " + "; ".join(errs))
    expected = set()
    for f in manifest["files"]:
        rel = _destination(f["destination"], "manifest destination")  # the rule rollback applies
        p = bundle / "files" / rel
        _regular(p)
        if os.lstat(p).st_mode & 0o7777 != FILE_MODE or f["mode"] != "0644":
            raise ActivationError(f"{rel}: bundled mode is not 0644")
        if sha256_file(p) != f["sha256"]:
            raise ActivationError(f"{rel}: bundled bytes differ from the manifest")
        expected.add(p)
    for p in (bundle / "files").rglob("*"):
        if p.is_symlink() or (p.is_file() and p not in expected):
            raise ActivationError(f"{p}: not in the manifest")
    if hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest() != manifest["bundle_digest"]:
        raise ActivationError("manifest bundle_digest does not match its file list")
    # The same-name collision check uses manifest["skills"]: it must be exactly the bundled skill dirs.
    dirs = {f["destination"].split("/")[2] for f in manifest["files"] if f["destination"].startswith(SKILLS_DEST + "/")}
    if sorted(dirs) != sorted(manifest["skills"]):
        raise ActivationError(f"manifest skills {sorted(manifest['skills'])} differ from the bundled skill "
                              f"directories {sorted(dirs)}")
    return manifest, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- target checks

def _real_homes() -> List[Path]:
    homes = [Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes", Path.home() / ".hermes"]
    if os.environ.get("HERMES_HOME"):
        homes.append(Path(os.environ["HERMES_HOME"]).expanduser())
    return [Path(os.path.realpath(h)) for h in homes]


def verify_rehearsal_home(target: Path, nonce: str) -> Path:
    """The target as a verified disposable rehearsal home, or ActivationError."""
    t = Path(os.path.abspath(target))
    if not t.is_absolute() or os.path.islink(t) or not t.is_dir() or os.path.realpath(t) != str(t):
        raise ActivationError(f"{t}: target must be an existing directory with no symlink in its path")
    real = t
    for live in _real_homes():
        # Only the live home's own scratch area may hold a disposable home; anything else at,
        # around or inside a live home (profiles, skills, plugins, ...) is refused.
        inside = live in real.parents and (live / "cache" / "scratch") not in real.parents
        if real == live or real in live.parents or inside:
            raise ActivationError(f"{t}: refused: this is, contains or is inside a real Hermes home ({live})")
    st = os.lstat(t)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ActivationError(f"{t}: target must be owned by you with mode 0700 (private)")
    try:
        mst = _regular(t / MARKER)
        marker = json.loads((t / MARKER).read_text())
    except (OSError, ActivationError, ValueError):
        raise ActivationError(f"{t}: no valid rehearsal marker {MARKER}") from None
    if (mst.st_uid != os.getuid() or not isinstance(marker, dict) or marker.get("purpose") != "capability-activation-rehearsal"
            or not re.fullmatch(r"[0-9a-f]{32}", str(marker.get("nonce", ""))) or marker["nonce"] != nonce):
        raise ActivationError(f"{t}: rehearsal marker does not match the confirmed nonce")
    present = [n for n in LIVE_STATE if os.path.lexists(t / n)]
    if present:
        raise ActivationError(f"{t}: looks like a real Hermes home (has {present}); refused")
    return t


def make_rehearsal_home(path: Path) -> str:
    """Create a NEW private rehearsal home with its marker; returns the nonce (tests and the
    documented rehearsal use this; it never touches an existing directory)."""
    p = Path(os.path.abspath(path))
    os.mkdir(p, 0o700)
    os.chmod(p, 0o700)
    nonce = os.urandom(16).hex()
    fd = os.open(p / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"purpose": "capability-activation-rehearsal", "nonce": nonce}, fh)
    return nonce


def _safe_parents(target: Path, rel: str) -> None:
    cur = target
    for part in rel.split("/")[:-1]:
        cur = cur / part
        if os.path.lexists(cur) and (os.path.islink(cur) or not cur.is_dir()):
            raise ActivationError(f"{cur}: path component is a symlink or not a directory")


_RELEASE = re.compile(r"(0|[1-9][0-9]{0,3})(?:\.(0|[1-9][0-9]{0,3})){0,2}")
_SPEC = re.compile(r"(>=|<=|==|>|<)(.+)")


def _release(ver: str) -> Optional[Tuple[int, int, int]]:
    """A final-release version X[.Y[.Z]] as a 3-tuple; None for anything else (rc, dev, local, ...)."""
    if not _RELEASE.fullmatch(ver):
        return None
    nums = [int(x) for x in ver.split(".")]
    return tuple(nums + [0] * (3 - len(nums)))  # type: ignore[return-value]


def _satisfies(ver: str, spec: str) -> bool:
    """``ver`` is a final release meeting every comma-separated clause of ``spec`` (>=, <=, ==, >, <).
    Unparsable versions or clauses are refused, never approximated."""
    v = _release(ver)
    if v is None:
        return False
    ops = {">=": v.__ge__, "<=": v.__le__, "==": v.__eq__, ">": v.__gt__, "<": v.__lt__}
    for clause in spec.split(","):
        m = _SPEC.fullmatch(clause.strip())
        bound = _release(m.group(2)) if m else None
        if bound is None or not ops[m.group(1)](bound):
            return False
    return True


def check_prerequisites(python: str, python_spec: str = ">=3.10") -> Dict[str, Any]:
    """The Python version and jsonschema of ``python`` (run with -I, nothing installed) against the
    manifest's ``python`` prerequisite and ``JSONSCHEMA_RANGE``; pre-releases are refused."""
    try:
        r = subprocess.run([python, "-I", "-c", "import sys, importlib.metadata as m; "
                            "print(sys.version.split()[0]); print(m.version('jsonschema'))"],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "python": python, "reason": "interpreter probe timed out after 60s (no install performed)"}
    if r.returncode != 0:
        return {"ok": False, "python": python, "reason": "jsonschema is not importable (no install performed)"}
    out = r.stdout.split()
    if len(out) != 2:
        return {"ok": False, "python": python, "reason": "unexpected interpreter probe output"}
    pyver, ver = out
    reasons = []
    if not _satisfies(pyver, python_spec):
        reasons.append(f"python {pyver} is not a final release satisfying {python_spec}")
    if not _satisfies(ver, ",".join(JSONSCHEMA_RANGE)):
        reasons.append(f"jsonschema {ver} is not a final release within {','.join(JSONSCHEMA_RANGE)}")
    return {"ok": not reasons, "python": python, "python_version": pyver, "jsonschema": ver,
            "reason": "; ".join(reasons)}


def preflight(target: Path, bundle: Path, nonce: str, python: str) -> Dict[str, Any]:
    """Read-only plan of an apply: per-file preimage (absent | identical), collisions, prerequisites."""
    manifest, msha = load_bundle(bundle)
    t = verify_rehearsal_home(target, nonce)
    entries, collisions = [], []
    for f in manifest["files"]:
        rel = f["destination"]
        _safe_parents(t, rel)
        dest = t / rel
        if not os.path.lexists(dest):
            entries.append({"destination": rel, "preimage": "absent"})
            continue
        try:
            st = _regular(dest)
        except ActivationError as exc:
            collisions.append({"destination": rel, "reason": str(exc)})
            continue
        if sha256_file(dest) == f["sha256"] and stat.S_IMODE(st.st_mode) == FILE_MODE:
            entries.append({"destination": rel, "preimage": "identical", "mode": "0644"})
        else:
            collisions.append({"destination": rel, "reason": "existing file differs (bytes or mode)"})
    sys.path.insert(0, str(SCRIPTS))
    import capability_compat as cc  # noqa: E402
    ours = {t / SKILLS_DEST / s for s in manifest["skills"]}
    installed = cc.installed_skill_names([t / "skills"], exclude=ours)
    for s in manifest["skills"]:
        for path in installed.get(s, []):
            collisions.append({"destination": path, "reason": f"another installed skill already uses the name {s}"})
    prereq = check_prerequisites(python, manifest["prerequisites"]["python"])
    return {"manifest_sha256": msha, "target": str(t), "entries": entries, "collisions": collisions,
            "prerequisites": prereq, "would_write": sum(1 for e in entries if e["preimage"] == "absent"),
            "ok": not collisions and prereq["ok"]}


# ---------------------------------------------------------------- apply / rollback

def _write_new(dest: Path, data: bytes, state: Optional[Dict[str, Any]] = None) -> None:
    """Write without clobbering: temp file in the same directory, then link() (fails if dest exists).
    ``state`` records progress for the caller's revert: ``tmp``, ``phase`` (writing, linking,
    link-failed, linked) and ``tmp_removed``. An interrupt inside link() leaves ``linking`` (the
    destination may or may not be ours); a temp that cannot be removed after a successful link
    raises, since the destination then has a second link."""
    st: Dict[str, Any] = state if state is not None else {}
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    st.update(tmp=str(tmp), phase="writing", tmp_removed=False)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    linked = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, FILE_MODE)
        st["phase"] = "linking"
        try:
            os.link(tmp, dest)
        except OSError:
            st["phase"] = "link-failed"
            raise
        st["phase"] = linked = "linked"
    finally:
        try:
            os.unlink(tmp)
            st["tmp_removed"] = True
        except OSError:
            if linked:
                raise  # the destination is ours but still has the temp as a second link


def _outside_path(path: Path, t: Path, what: str) -> Path:
    """An absolute path outside the target whose parent is a real directory (no symlink anywhere)."""
    p = Path(os.path.abspath(path))
    if not p.parent.is_dir() or os.path.realpath(p.parent) != str(p.parent):
        raise ActivationError(f"{p}: the {what} directory must exist with no symlink in its path")
    if p == t or t in p.parents:
        raise ActivationError(f"{p}: the {what} must live outside the target")
    return p


def _json_bytes(doc: Dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


def _create_exclusive(path: Path) -> int:
    """fd of a NEW 0600 file; refuses an existing path or symlink."""
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    except FileExistsError:
        raise ActivationError(f"{path}: already exists; a new file is required") from None


def _write_journal(path: Path, journal: Dict[str, Any], *, new: bool) -> None:
    data = _json_bytes(journal)
    if new:
        with os.fdopen(_create_exclusive(path), "wb") as fh:
            fh.write(data)
        return
    # Fresh, unpredictable temp name created O_EXCL|O_NOFOLLOW, then an atomic rename over the journal.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    fd = _create_exclusive(tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def apply(target: Path, bundle: Path, nonce: str, manifest_sha256: str, journal_path: Path, python: str,
          *, _fail_after: Optional[int] = None) -> Dict[str, Any]:
    """Rehearsal apply. Refuses on any collision or prerequisite failure before writing; writes a
    journal first; on any exception mid-way (KeyboardInterrupt included) removes what it created,
    records it, and re-raises. Idempotent: identical files
    are left. The journal is complete only at the end: a hard kill or power loss leaves it
    ``in-progress`` without the files already written (docs/capability-activation.md, recovery)."""
    plan = preflight(target, bundle, nonce, python)
    if plan["manifest_sha256"] != manifest_sha256:
        raise ActivationError("the bundle manifest is not the approved one (sha256 differs)")
    if not plan["ok"]:
        raise ActivationError("preflight refused: " + json.dumps({"collisions": plan["collisions"],
                                                                  "prerequisites": plan["prerequisites"]}))
    t = Path(plan["target"])
    journal_path = _outside_path(journal_path, t, "journal")
    manifest, msha = load_bundle(bundle)  # re-read: it must still be the approved manifest
    if msha != manifest_sha256:
        raise ActivationError("the bundle manifest changed after preflight; it is not the approved one")
    by_dest = {f["destination"]: f for f in manifest["files"]}
    if sorted(by_dest) != sorted(e["destination"] for e in plan["entries"]):
        raise ActivationError("the bundle file list changed after preflight")
    journal = {"schema_version": 1, "kind": "capability-activation-journal", "manifest_sha256": plan["manifest_sha256"],
               "target": str(t), "status": "in-progress", "entries": [], "created_dirs": []}
    _write_journal(journal_path, journal, new=True)
    written = 0
    mk: Dict[str, Any] = {}  # an in-flight mkdir (cleared once recorded)
    cur: Optional[Dict[str, Any]] = None  # the in-flight file write (None once its entry is recorded)
    try:
        for e in plan["entries"]:
            rel = e["destination"]
            if e["preimage"] == "identical":
                journal["entries"].append({"destination": rel, "preimage": "identical", "action": "unchanged"})
                continue
            _make_parents(t, rel, journal["created_dirs"], mk)
            if _fail_after is not None and written >= _fail_after:
                raise OSError("injected failure (test)")
            _safe_parents(t, rel)
            cur = {"destination": rel}
            _write_new(t / rel, _approved_bytes(Path(bundle), by_dest[rel]), cur)
            written += 1
            journal["entries"].append(_created_entry(rel, by_dest[rel]["sha256"]))
            cur = None
        journal["status"] = "applied"
        _write_journal(journal_path, journal, new=False)
        return journal
    except BaseException as exc:  # KeyboardInterrupt too: revert, record, then re-raise it
        journal["status"] = "failed"
        journal["error"] = str(exc) or type(exc).__name__
        unknown = _in_flight(t, journal, cur, mk, by_dest)
        try:
            undo = _undo(t, journal)
        except ActivationError as uexc:  # raised only by the check pass, before any removal
            undo = {"removed": [], "refused": [str(uexc)], "dirs_removed": []}
        if unknown:
            undo["cleanup_unknown"] = unknown
        clean = not (undo["refused"] or undo.get("errors") or unknown)
        journal["status"] = "failed-reverted" if clean else "failed-partial"
        journal["revert"] = undo
        _write_journal(journal_path, journal, new=False)
        if not isinstance(exc, (OSError, ActivationError)):
            raise
        raise ActivationError(f"apply failed ({journal['status']}): {exc}") from None


def _created_entry(rel: str, sha256: str) -> Dict[str, Any]:
    return {"destination": rel, "preimage": "absent", "action": "created", "sha256": sha256, "mode": "0644"}


def _in_flight(t: Path, journal: Dict[str, Any], cur: Optional[Dict[str, Any]], mk: Dict[str, Any],
               by_dest: Dict[str, Dict[str, Any]]) -> List[str]:
    """Settle the write or mkdir an exception interrupted. A destination known to be ours (linked,
    temp removed) is recorded so the revert removes it; anything whose ownership is not known (an
    interrupt inside link() or mkdir(), or a temp left beside its linked destination) is returned
    as ``cleanup_unknown`` and left in place: in-process state only, no transaction."""
    unknown: List[str] = []
    if mk.get("dir") and mk["dir"] not in journal["created_dirs"] and os.path.lexists(t / mk["dir"]):
        unknown.append(mk["dir"])
    if cur and cur.get("phase"):
        rel = cur["destination"]
        recorded = any(e["destination"] == rel for e in journal["entries"])
        if not cur.get("tmp_removed") and os.path.lexists(cur["tmp"]):
            unknown.append(os.path.relpath(cur["tmp"], t))
            if cur["phase"] in ("linking", "linked"):
                unknown.append(rel)  # second link to the temp: not a single-link file the undo may remove
        elif cur["phase"] == "linking" and os.path.lexists(t / rel):
            unknown.append(rel)
        elif cur["phase"] == "linked" and not recorded:
            journal["entries"].append(_created_entry(rel, by_dest[rel]["sha256"]))
    return unknown


def _make_parents(t: Path, rel: str, created: List[str], mk: Dict[str, Any]) -> None:
    """Create the missing parent directories of ``rel`` one component at a time from the target:
    each existing component is opened with O_NOFOLLOW (a symlink or non-directory refuses) BEFORE
    anything is created beneath it, and each new one is made with mkdir relative to its checked
    parent's fd. Each created directory (relative) is appended to ``created`` as soon as mkdir
    returns, so a later failure still reverts it; ``mk['dir']`` names an mkdir in flight."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parts = rel.split("/")[:-1]
    fd = os.open(t, flags)
    try:
        for i, part in enumerate(parts):
            sub = "/".join(parts[:i + 1])
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                mk["dir"] = sub
                try:
                    os.mkdir(part, DIR_MODE, dir_fd=fd)
                except OSError:
                    mk.clear()  # not made by us
                    raise
                created.append(sub)
                mk.clear()
                try:
                    nxt = os.open(part, flags, dir_fd=fd)
                except OSError:
                    raise ActivationError(f"{t}/{sub}: created directory cannot be opened") from None
            except OSError:
                raise ActivationError(f"{t}/{sub}: path component is a symlink or not a directory") from None
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)


def _approved_bytes(bundle: Path, f: Dict[str, Any]) -> bytes:
    """The bundled bytes of manifest entry ``f``, read at write time and checked against its digest."""
    fd = os.open(bundle / "files" / f["destination"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_FILE:
            raise ActivationError(f"{f['destination']}: bundled file is not a single-link regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(MAX_FILE + 1)
    finally:
        os.close(fd)
    if hashlib.sha256(data).hexdigest() != f["sha256"]:
        raise ActivationError(f"{f['destination']}: bundled bytes changed after approval")
    return data


# ---------------------------------------------------------------- journal input

def _relpath(value: Any, what: str) -> str:
    """A normalized relative path: bounded, every segment matching ``_SEGMENT``."""
    if not isinstance(value, str) or not value or len(value) > MAX_DEST:
        raise ActivationError(f"{what}: not a bounded string")
    parts = value.split("/")
    if not all(_SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts):
        raise ActivationError(f"{what} {value!r}: not a normalized relative path")
    return value


def _destination(value: Any, what: str) -> str:
    """The one destination rule for bundles (apply) and journals (rollback): a normalized
    relative path strictly under one of the two allowed roots."""
    rel = _relpath(value, what)
    if not rel.startswith((SKILLS_DEST + "/", ROOT_DEST + "/")):
        raise ActivationError(f"{what} {rel!r} is outside the allowed roots")
    return rel


def _exact(obj: Any, keys: Dict[str, type], what: str) -> Dict[str, Any]:
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ActivationError(f"journal {what}: keys must be exactly {sorted(keys)}")
    for k, typ in keys.items():
        if type(obj[k]) is not typ:  # noqa: E721  (bool is not int here)
            raise ActivationError(f"journal {what}.{k}: must be {typ.__name__}")
    return obj


_JOURNAL_KEYS = {"schema_version": int, "kind": str, "manifest_sha256": str, "target": str, "status": str,
                 "entries": list, "created_dirs": list}
_CREATED_KEYS = {"destination": str, "preimage": str, "action": str, "sha256": str, "mode": str}
_UNCHANGED_KEYS = {"destination": str, "preimage": str, "action": str}
_ROOT_DIRS = {"skills", SKILLS_DEST, "agent-ops-kit", ROOT_DEST}


def read_journal(path: Path, t: Path) -> Dict[str, Any]:
    """An applied journal for target ``t``, validated strictly before anything acts on it."""
    p = _outside_path(path, t, "journal")
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ActivationError(f"{p}: cannot open the journal ({exc.strerror}); symlinks are refused") from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
                or st.st_size > MAX_JOURNAL):
            raise ActivationError(f"{p}: journal must be your own single-link regular file of at most {MAX_JOURNAL} bytes")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_JOURNAL + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_JOURNAL:
        raise ActivationError(f"{p}: journal exceeds {MAX_JOURNAL} bytes")
    try:
        j = _registry().parse_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, _registry().RegistryError) as exc:  # duplicate keys, NaN, depth
        raise ActivationError(f"{p}: journal is not strict JSON ({exc})") from None
    _exact(j, _JOURNAL_KEYS, "")
    if (j["schema_version"] != 1 or j["kind"] != "capability-activation-journal" or j["target"] != str(t)
            or not _SHA256.fullmatch(j["manifest_sha256"])):
        raise ActivationError("journal is not an activation journal for this target")
    if j["status"] != "applied":
        raise ActivationError(f"journal status is {j['status']!r}, not 'applied'")
    seen = set()
    created_parents = set()
    for i, e in enumerate(j["entries"]):
        created = isinstance(e, dict) and e.get("action") == "created"
        _exact(e, _CREATED_KEYS if created else _UNCHANGED_KEYS, f"entries[{i}]")
        rel = _destination(e["destination"], f"journal entries[{i}].destination")
        if rel in seen:
            raise ActivationError(f"journal destination {rel!r} is listed twice")
        seen.add(rel)
        if created:
            if e["preimage"] != "absent" or not _SHA256.fullmatch(e["sha256"]) or e["mode"] != "0644":
                raise ActivationError(f"journal entries[{i}]: created entry needs preimage absent, sha256, mode 0644")
            parts = rel.split("/")
            created_parents.update("/".join(parts[:k]) for k in range(1, len(parts)))
        elif (e["preimage"], e["action"]) != ("identical", "unchanged"):
            raise ActivationError(f"journal entries[{i}]: unknown action")
    dirs = j["created_dirs"]
    for d in dirs:
        _relpath(d, "journal created_dirs")
        if d not in created_parents or not (d in _ROOT_DIRS or d.startswith((SKILLS_DEST + "/", ROOT_DEST + "/"))):
            raise ActivationError(f"journal created_dirs {d!r} is not a parent of a file this apply created")
    if len(set(dirs)) != len(dirs):
        raise ActivationError("journal created_dirs lists a directory twice")
    return j


# ---------------------------------------------------------------- undo (no symlink is ever followed)

def _open_dir_chain(t: Path, parts: Sequence[str]) -> Optional[int]:
    """fd of ``t/parts...`` opened component by component with O_NOFOLLOW; None if a component is
    absent; ActivationError if any component is a symlink or not a directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(t, flags)
    except OSError:
        raise ActivationError(f"{t}: target is not a real directory") from None
    for part in parts:
        try:
            nxt = os.open(part, flags, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None
        except OSError:
            os.close(fd)
            raise ActivationError(f"{t}/{'/'.join(parts)}: a path component is a symlink or not a directory") from None
        os.close(fd)
        fd = nxt
    return fd


def _undo(t: Path, journal: Dict[str, Any]) -> Dict[str, Any]:
    """Two passes over the journal's own created allowlist: check everything, then remove. Every
    path is resolved with O_NOFOLLOW from the target, so no symlinked component is followed."""
    created = [e for e in journal["entries"] if e["action"] == "created"]
    refused, present = [], []
    for e in created:
        *parents, name = e["destination"].split("/")
        pfd = _open_dir_chain(t, parents)
        if pfd is None:
            continue
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=pfd)
            except FileNotFoundError:
                continue
            except OSError:
                refused.append(e["destination"])
                continue
            try:
                st = os.fstat(fd)
                same = (stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= MAX_FILE
                        and stat.S_IMODE(st.st_mode) == FILE_MODE)
                if same:
                    with os.fdopen(fd, "rb", closefd=False) as fh:
                        same = hashlib.sha256(fh.read(MAX_FILE + 1)).hexdigest() == e["sha256"]
            finally:
                os.close(fd)
        finally:
            os.close(pfd)
        (present if same else refused).append(e["destination"])
    for d in journal["created_dirs"]:
        fd = _open_dir_chain(t, d.split("/"))  # raises on a symlinked or non-directory component
        if fd is not None:
            os.close(fd)
    if refused:
        return {"removed": [], "refused": refused, "dirs_removed": []}
    removed, errors = [], []
    for rel in present:
        *parents, name = rel.split("/")
        try:
            pfd = _open_dir_chain(t, parents)
        except ActivationError as exc:  # changed after the check pass: keep going, report it
            errors.append(f"{rel}: {exc}")
            continue
        if pfd is None:
            continue
        try:
            os.unlink(name, dir_fd=pfd)
            removed.append(rel)
        except OSError as exc:
            errors.append(f"{rel}: {exc.strerror or exc}")
        finally:
            os.close(pfd)
    dirs = []
    for d in sorted(journal["created_dirs"], key=lambda x: -x.count("/")):
        *parents, name = d.split("/")
        try:
            pfd = _open_dir_chain(t, parents)
        except ActivationError as exc:  # changed after the file removals: keep what was removed
            errors.append(f"{d}: {exc}")
            continue
        if pfd is None:
            continue
        try:
            os.rmdir(name, dir_fd=pfd)
            dirs.append(d)
        except OSError:
            pass  # not empty (something else lives there now) or gone; keep it
        finally:
            os.close(pfd)
    out = {"removed": removed, "refused": [], "dirs_removed": dirs}
    if errors:
        out["errors"] = errors
    return out


def rollback(target: Path, nonce: str, journal_path: Path, out: Path, bundle: Path) -> Dict[str, Any]:
    """Undo one applied journal: removes only files this apply created and that still have the
    applied bytes and mode; refuses (removing nothing) when any of them changed. The journal must
    name the approved bundle's manifest and only its files, and is consumed (status
    ``rolled-back`` or ``rollback-partial``) so it cannot be replayed against a later apply. A
    same-UID writer can still forge a journal naming approved files; there is no trusted receipt."""
    t = verify_rehearsal_home(target, nonce)
    journal = read_journal(journal_path, t)
    manifest, msha = load_bundle(bundle)
    if msha != journal["manifest_sha256"]:
        raise ActivationError("journal was not written for this bundle's manifest")
    approved = {f["destination"]: f["sha256"] for f in manifest["files"]}
    for e in journal["entries"]:
        if e["destination"] not in approved or (e["action"] == "created" and e["sha256"] != approved[e["destination"]]):
            raise ActivationError(f"journal entry {e['destination']!r} is not a file of the approved bundle")
    out = _outside_path(out, t, "rollback report")
    if out == Path(os.path.abspath(journal_path)):
        raise ActivationError("the rollback report must be a new file, not the journal")
    report = {"kind": "capability-activation-rollback", "journal": str(os.path.abspath(journal_path)), "target": str(t)}
    with os.fdopen(_create_exclusive(out), "wb") as fh:  # reserved before anything is removed
        try:
            result = _undo(t, journal)
        except ActivationError as exc:
            fh.write(_json_bytes({**report, "status": "refused", "error": str(exc), "removed": [], "refused": [],
                                  "dirs_removed": []}))
            raise
        status = "refused" if result["refused"] else "partial" if result.get("errors") else "rolled-back"
        report.update(result, status=status)
        fh.write(_json_bytes(report))
    if status != "refused":  # something was removed: the journal is spent
        _write_journal(Path(os.path.abspath(journal_path)),
                       {**journal, "status": "rolled-back" if status == "rolled-back" else "rollback-partial"}, new=False)
    if result["refused"]:
        raise ActivationError(f"rollback refused: changed after apply: {result['refused']}")
    if status == "partial":
        raise ActivationError(f"rollback partial: removed {len(result['removed'])}, failed: {result['errors']}")
    return report


def verify_installed(target: Path, bundle: Path) -> Dict[str, Any]:
    """Read-only: each manifest file at the target has the manifest bytes and mode and is reached
    without a symlinked parent component (else it is reported mismatched)."""
    manifest, msha = load_bundle(bundle)
    t = Path(os.path.abspath(target))
    if os.path.realpath(t) != str(t) or not t.is_dir():
        raise ActivationError(f"{t}: target must be an existing directory with no symlink in its path")
    bad = []
    for f in manifest["files"]:
        p = t / f["destination"]
        try:
            _safe_parents(t, f["destination"])  # the apply parent check: no symlinked component
            st = _regular(p)
            if sha256_file(p) != f["sha256"] or stat.S_IMODE(st.st_mode) != FILE_MODE:
                bad.append(f["destination"])
        except (OSError, ActivationError):
            bad.append(f["destination"])
    return {"manifest_sha256": msha, "files": len(manifest["files"]), "mismatched": bad, "ok": not bad}


# ---------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Capability activation proposal (manifest, bundle, rehearsal only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("manifest", help="print the computed manifest")
    sub.add_parser("check", help="compare the computed manifest with the committed one")
    b = sub.add_parser("bundle")
    b.add_argument("--out", required=True)
    for name in ("preflight", "apply", "rollback", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--target", required=True, help="a disposable rehearsal home (no default)")
        p.add_argument("--bundle", required=True)
        if name != "verify":
            p.add_argument("--confirm-rehearsal", required=True, help="the rehearsal marker nonce")
        if name in ("preflight", "apply"):
            p.add_argument("--python", default=sys.executable, help="interpreter the skills will use (jsonschema check)")
        if name == "apply":
            p.add_argument("--manifest-sha256", required=True, help="approved manifest digest")
            p.add_argument("--journal", required=True, help="new journal file outside the target")
        if name == "rollback":
            p.add_argument("--journal", required=True)
            p.add_argument("--out", required=True, help="new rollback report file")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "manifest":
            sys.stdout.write(manifest_bytes(build_manifest()).decode())
            return 0
        if args.cmd == "check":
            want = manifest_bytes(build_manifest())
            have = MANIFEST_PATH.read_bytes() if MANIFEST_PATH.is_file() else b""
            print(json.dumps({"matches": want == have, "manifest": str(MANIFEST_PATH),
                              "computed_sha256": hashlib.sha256(want).hexdigest()}, indent=2))
            return 0 if want == have else 1
        if args.cmd == "bundle":
            m = write_bundle(Path(args.out))
            print(json.dumps({"bundle": str(Path(args.out).resolve()), "files": len(m["files"]),
                              "manifest_sha256": hashlib.sha256(manifest_bytes(m)).hexdigest()}, indent=2))
            return 0
        if args.cmd == "preflight":
            rep = preflight(Path(args.target), Path(args.bundle), args.confirm_rehearsal, args.python)
            print(json.dumps(rep, indent=2))
            return 0 if rep["ok"] else 3
        if args.cmd == "apply":
            rep = apply(Path(args.target), Path(args.bundle), args.confirm_rehearsal, args.manifest_sha256,
                        Path(args.journal), args.python)
        elif args.cmd == "rollback":
            rep = rollback(Path(args.target), args.confirm_rehearsal, Path(args.journal), Path(args.out), Path(args.bundle))
        else:
            rep = verify_installed(Path(args.target), Path(args.bundle))
            print(json.dumps(rep, indent=2))
            return 0 if rep["ok"] else 1
        print(json.dumps(rep, indent=2))
        return 0
    except ActivationError as exc:
        print(f"capability_activation: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"capability_activation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
