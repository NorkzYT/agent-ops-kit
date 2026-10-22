#!/usr/bin/env python3
"""knowledge-apply.py — apply one reviewed knowledge plan end to end.

    knowledge-apply.py --plan PLAN --dry-run          validate and show what would happen
    knowledge-apply.py --plan PLAN --approve          apply it (make knowledge-apply PLAN=... APPROVE=1)
    knowledge-apply.py --plan PLAN --legacy-report    read-only: profile-local copies of canonical books

One run, in this order, each step idempotent so the same plan can be re-run to
resume after a failure:

  1. validate   every item has a terminal decision (assign | skip | existing),
                every assigned profile exists or is an approved new-profile
                spec, every assigned source still hashes to what the plan saw,
                names are safe. Any refusal stops before a single write.
  2. profiles   create approved new profiles under hermes/profiles/<name>
                (SOUL.md from _template, book list, assignments file).
  3. assign     append the book-list line and record the assignment metadata
                (purpose, chapters, tags, secondary reason) in
                hermes/profiles/<p>/knowledge-assignments.json.
  4. canonical  copy (.md/.txt) or convert (EPUB/PDF) each unique source ONCE
                into $KNOWLEDGE_CANONICAL/sources/<source_id>/ (content id).
  5. reference  write a tiny refs/<name>.ref.md per canonical file into
                data/knowledge/<p>/ (never the book text).
  6. install    installed profiles: knowledge-sync.sh; new ones: hermes-profile.sh.
  7. learn      scripts/knowledge-learn.py for the affected profiles (resumable).
  8. report     $KNOWLEDGE_CANONICAL/apply/<plan_id>/report.json + checkpoint.json.

The library is only read. Nothing under a profile's existing knowledge is
deleted; legacy full copies are reported, never migrated. Exit 0 when complete,
1 when the plan is refused (nothing written), 3 when a step failed (the exact
resume command is printed).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_plan as kp  # noqa: E402

STEPS = ("validate", "profiles", "assignments", "canonical", "references", "install", "learn", "report")
MAX_TEXT_BYTES = 64 * 1024 * 1024
LEGACY_NOTE = ("legacy profile-local copies of canonical books: not migrated, not deleted. Remove a copy by "
               "hand only after its reference learned; knowledge-learn keeps learning the copy until then.")


class StepError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[knowledge-apply] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[knowledge-apply][error] {msg}", file=sys.stderr, flush=True)


class Settings:
    def __init__(self, args: argparse.Namespace):
        env = os.environ.get
        self.plan_path = Path(args.plan).resolve()
        self.books = Path(env("BOOKS_ROOT", "/mnt/books"))
        self.kroot = Path(env("KNOWLEDGE_ROOT", str(ROOT / "data" / "knowledge")))
        self.canon = kp.canonical_root()
        self.profiles_dir = Path(env("PROFILES_DIR", str(ROOT / "hermes" / "profiles")))
        self.hermes_home = Path(env("HERMES_HOME") or Path.home() / ".hermes")
        self.doc2md = Path(env("DOC2MD", str(ROOT / "hermes" / "skills" / "document-converter" / "scripts" / "doc2md.py")))
        self.convert = env("KNOWLEDGE_CONVERT", "1") == "1"
        self.install = not args.no_install
        self.learn = not args.no_learn and self.install
        self.learn_timeout = int(env("KNOWLEDGE_LEARN_TIMEOUT", "1800"))
        self.approve = args.approve
        self.dry_run = args.dry_run
        self.plan_id = ""

    def check(self) -> None:
        if not self.books.is_dir():
            raise kp.PlanError(f"book library not found at {self.books} (set BOOKS_ROOT)")
        books = self.books.resolve()
        for name, other in (("knowledge root", self.kroot), ("canonical store", self.canon)):
            o = other.resolve() if other.exists() else other.absolute()
            if kp.is_within(o, books) or kp.is_within(books, o):
                raise kp.PlanError(f"{name} {o} overlaps the book library {books}; refusing")
        if not self.profiles_dir.is_dir():
            raise kp.PlanError(f"profiles dir not found: {self.profiles_dir}")

    @property
    def run_dir(self) -> Path:
        return self.canon / "apply" / self.plan_id

    def resume_cmd(self) -> str:
        return f"make knowledge-apply PLAN={shq(str(self.plan_path))} APPROVE=1"


def shq(x: str) -> str:
    return "'" + x.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Checkpoint and report
# --------------------------------------------------------------------------- #
class Checkpoint:
    def __init__(self, s: Settings):
        self.path = s.run_dir / "checkpoint.json"
        self.data = {"plan_id": s.plan_id, "plan": str(s.plan_path), "created": kp.now_iso(),
                     "steps": {name: {"done": False} for name in STEPS}, "failed_step": "",
                     "profiles_created": [], "profiles": []}
        if self.path.is_file():
            try:
                old = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(old, dict) and old.get("plan_id") == s.plan_id:
                    self.data.update(old)
                    self.data["steps"] = {name: old.get("steps", {}).get(name, {"done": False}) for name in STEPS}
            except ValueError:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = kp.now_iso()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def done(self, step: str, **info) -> None:
        self.data["steps"][step] = dict(info, done=True, at=kp.now_iso())
        if self.data.get("failed_step") == step:
            self.data["failed_step"] = ""
        self.save()

    def failed(self, step: str, reason: str) -> None:
        self.data["steps"][step] = {"done": False, "at": kp.now_iso(), "error": reason[:400]}
        self.data["failed_step"] = step
        self.save()


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def create_profiles(s: Settings, v: kp.Validated, ck: Checkpoint) -> List[str]:
    created = []
    template = s.profiles_dir / "_template" / "SOUL.md"
    for name, spec in v.new_profiles.items():
        pdir = s.profiles_dir / name
        if name in ck.data["profiles_created"] and (pdir / "SOUL.md").is_file():
            log(f"profile {name}: created by this plan earlier; kept")
            continue
        if pdir.exists():
            raise StepError(f"profile folder {pdir} already exists")
        if not template.is_file():
            raise StepError(f"no profile template at {template}")
        pdir.mkdir(parents=True)
        (pdir / "SOUL.md").write_text(render_soul(template.read_text(encoding="utf-8"), spec), encoding="utf-8")
        (pdir / kp.MANIFEST_FILE).write_text(
            "# Books for this profile, one path per line, relative to BOOKS_ROOT (default /mnt/books).\n"
            "# Created by knowledge-apply from an approved plan; shared books live in the canonical store\n"
            "# and are referenced from knowledge/refs/ (see knowledge-assignments.json).\n", encoding="utf-8")
        data = kp.load_assignments(pdir)
        data["profile_spec"] = {k: spec.get(k) for k in ("purpose", "in_scope", "out_of_scope", "parent",
                                                          "overlap", "boundaries")}
        data["created_by_plan"] = s.plan_id
        kp.save_assignments(pdir, data)
        ck.data["profiles_created"].append(name)
        ck.save()
        created.append(name)
        log(f"profile {name}: created at {pdir} (SOUL.md from _template, empty book list)")
    return created


def render_soul(template: str, spec: Dict[str, object]) -> str:
    name = str(spec["name"])
    title = " ".join(w.capitalize() for w in name.split("-"))
    purpose = str(spec["purpose"]).strip()
    domain = purpose.split(":", 1)[0].split(".", 1)[0].strip() or name
    out = (template.replace("<profile name>", name).replace("<Name>", title).replace("<domain>", domain))
    out = out.replace("<One paragraph: what this agent is for and what \"good\" looks like.>", purpose)
    bullets = lambda key: "\n".join(f"- {x}" for x in spec.get(key, []))  # noqa: E731
    scope = ["## Scope", "", f"Purpose: {purpose}", "", "In scope:", bullets("in_scope"), "", "Out of scope:",
             bullets("out_of_scope"), "", f"Parent / overlap: {spec.get('parent') or 'none'}"
             + (f"; overlaps with {', '.join(spec.get('overlap', []))}" if spec.get("overlap") else ""),
             "", "Boundaries and safety:", bullets("boundaries"), ""]
    marker = "## Knowledge"
    if marker in out:
        out = out.replace(marker, "\n".join(scope) + "\n" + marker, 1)
    else:
        out += "\n" + "\n".join(scope)
    return out


def append_manifest(manifest: Path, rel: str) -> bool:
    if manifest.is_symlink() or not manifest.is_file():
        raise StepError(f"book list is not a regular file: {manifest}")
    lines = [ln.rstrip("\r") for ln in manifest.read_text(encoding="utf-8", errors="replace").splitlines()]
    if rel in (ln for ln in lines if ln.strip() and not ln.startswith("#")):
        return False
    data = manifest.read_bytes()
    with open(manifest, "ab") as fh:
        if data and not data.endswith(b"\n"):
            fh.write(b"\n")
        fh.write(rel.encode("utf-8") + b"\n")
    return True


def write_assignments(s: Settings, v: kp.Validated) -> List[Dict[str, object]]:
    out = []
    for it in v.assign:
        prof, src = it["profile"], it["source"]
        pdir = s.profiles_dir / prof
        ident = v.identities[src]
        added = append_manifest(pdir / kp.MANIFEST_FILE, src)
        data = kp.load_assignments(pdir)
        rec = {"source": src, "source_id": ident.source_id, "source_sha256": ident.sha256, "mode": "canonical",
               "purpose": str(it.get("purpose") or ""), "chapters": list(it.get("chapters") or []),
               "tags": list(it.get("tags") or []), "secondary_reason": str(it.get("secondary_reason") or ""),
               "plan_id": s.plan_id, "assigned": kp.now_iso()}
        existing = next((a for a in data["assignments"] if a.get("source") == src), None)
        if existing is None:
            data["assignments"].append(rec)
            state = "assigned"
        else:
            keep = existing.get("assigned", rec["assigned"])
            existing.update(rec)
            existing["assigned"] = keep
            state = "already-assigned"
        data["assignments"].sort(key=lambda a: a.get("source", ""))
        kp.save_assignments(pdir, data)
        out.append({"profile": prof, "source": src, "source_id": ident.source_id,
                    "manifest": "appended" if added else "listed", "assignment": state})
        log(f"assign {prof} <- {src} ({'book list appended' if added else 'already listed'}, {state})")
    return out


def canonical_dir(s: Settings, source_id: str) -> Path:
    return s.canon / "sources" / source_id


def canonicalize(s: Settings, ident: kp.SourceIdentity) -> Dict[str, object]:
    """Copy/convert one source into the canonical store exactly once; verify when present."""
    dest = canonical_dir(s, ident.source_id)
    meta_path = dest / "source.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
        if meta.get("complete") and meta.get("sha256") == ident.sha256 and \
                all((dest / f["rel"]).is_file() and kp.sha256_file(dest / f["rel"]) == f["sha256"] for f in meta.get("files", [])):
            if ident.source not in meta.get("sources", []):
                meta.setdefault("sources", []).append(ident.source)
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            log(f"canonical {ident.source_id}: present and verified ({len(meta.get('files', []))} file(s)) for {ident.source}")
            return meta
        log(f"canonical {ident.source_id}: incomplete or changed; rebuilding")
    src = kp.safe_source(s.books, ident.source)
    base = src.parent if src.is_file() else src
    collection, rest = ident.source.split("/", 1)
    dest.mkdir(parents=True, exist_ok=True)
    files: List[Dict[str, object]] = []
    problems: List[str] = []
    for f in kp.book_files(src, kp.TEXT_EXTS):
        rel = f.relative_to(base).as_posix()
        if f.stat().st_size > MAX_TEXT_BYTES:
            problems.append(f"too large: {rel}")
            continue
        out = dest / rel
        if out.is_symlink():
            raise StepError(f"canonical output is a symlink: {out}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if not (out.is_file() and filecmp.cmp(f, out, shallow=False)):
            shutil.copyfile(f, out)
        files.append({"rel": rel, "from": rel, "sha256": kp.sha256_file(out), "kind": "copied"})
    binaries = [f for f in kp.book_files(src, (".epub", ".pdf"))]
    if binaries:
        if not s.convert:
            problems.append(f"{len(binaries)} EPUB/PDF not converted (KNOWLEDGE_CONVERT=0)")
        elif not s.doc2md.is_file():
            raise StepError(f"converter not found at {s.doc2md} (set DOC2MD)")
        else:
            tmp = Path(tempfile.mkdtemp(prefix="knowledge-canonical.", dir=str(s.canon)))
            try:
                proc = subprocess.run([sys.executable, str(s.doc2md), "--source", str(s.books.resolve() / collection),
                                       "--dest", str(tmp), "--json", "--", rest],
                                      capture_output=True, text=True, check=False, timeout=7200)
                try:
                    results = json.loads(proc.stdout).get("results", [])
                except ValueError:
                    raise StepError(f"converter exit {proc.returncode} without a report: {proc.stderr.strip()[-300:]}")
                for r in results:
                    status, outp = r.get("status"), r.get("output")
                    if status in ("converted", "partial", "up-to-date") and outp:
                        op = Path(outp)
                        if not op.is_file() or op.is_symlink() or not kp.is_within(op.resolve(), tmp.resolve()):
                            problems.append(f"converter output outside the work folder: {outp}")
                            continue
                        # keep the path relative to the source (a folder) or the file name (a file)
                        rel = op.relative_to(tmp / rest).as_posix() if src.is_dir() else op.name
                        out = dest / rel
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(op, out)
                        files.append({"rel": rel, "from": r.get("source", ""), "sha256": kp.sha256_file(out),
                                      "kind": "converted" if status != "partial" else "converted-partial"})
                        if status == "partial":
                            problems.append(f"partial conversion: {r.get('source')}")
                    elif status not in ("unsupported", None):
                        problems.append(f"{status}: {r.get('source')}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    if not files:
        raise StepError(f"canonical {ident.source_id}: no text produced for {ident.source} ({'; '.join(problems) or 'no book files'})")
    complete = not any(p.startswith(("partial", "converter", "too large")) or "not converted" in p for p in problems)
    meta = {"source_id": ident.source_id, "sha256": ident.sha256, "sources": [ident.source], "library": str(s.books.resolve()),
            "files": sorted(files, key=lambda x: x["rel"]), "problems": problems, "complete": complete,
            "written": kp.now_iso()}
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not complete:
        raise StepError(f"canonical {ident.source_id} for {ident.source} is incomplete: {'; '.join(problems)}")
    log(f"canonical {ident.source_id}: {len(files)} file(s) for {ident.source}")
    return meta


def write_references(s: Settings, v: kp.Validated, canon_meta: Dict[str, Dict[str, object]]) -> List[Dict[str, object]]:
    out = []
    root = str(s.canon.resolve())
    for it in v.assign:
        prof, src = it["profile"], it["source"]
        ident = v.identities[src]
        meta = canon_meta[ident.source_id]
        rdir = s.kroot / prof / kp.REF_DIR
        rdir.mkdir(parents=True, exist_ok=True)
        for f in meta["files"]:
            name = kp.ref_name(src, str(f["rel"]), ident.source_id)
            path = rdir / name
            fields = {"canonical-ref": 1, "source": src, "source_id": ident.source_id, "canonical_root": root,
                      "canonical_rel": f"sources/{ident.source_id}/{f['rel']}", "sha256": f["sha256"], "profile": prof,
                      "purpose": it.get("purpose") or "", "chapters": it.get("chapters") or [],
                      "tags": it.get("tags") or [], "secondary_reason": it.get("secondary_reason") or "",
                      "plan_id": s.plan_id, "written": kp.now_iso()}
            text = kp.render_ref(fields)
            state = "current"
            if path.is_symlink():
                raise StepError(f"reference path is a symlink: {path}")
            if path.is_file():
                try:
                    old = kp.parse_ref(path)
                except ValueError:
                    old = {}
                same = all(old.get(k) == str(fields[k]) for k in ("source_id", "canonical_rel", "sha256", "profile"))
                if not same:
                    path.write_text(text, encoding="utf-8")
                    state = "updated"
            else:
                path.write_text(text, encoding="utf-8")
                state = "written"
            resolved, problem = kp.resolve_ref(kp.parse_ref(path), s.canon)
            if resolved is None:
                raise StepError(f"reference {path} does not validate: {problem}")
            out.append({"profile": prof, "source": src, "ref": f"{prof}/{kp.REF_DIR}/{name}", "state": state,
                        "canonical": fields["canonical_rel"]})
        log(f"references {prof}: {len(meta['files'])} for {src}")
    return out


def install_profiles(s: Settings, profiles: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in profiles:
        pdir = s.hermes_home / "profiles" / p
        env = dict(os.environ, KNOWLEDGE_ROOT=str(s.kroot), PROFILES_DIR=str(s.profiles_dir),
                   HERMES_HOME=str(s.hermes_home))
        if pdir.is_dir():
            cmd = ["bash", str(ROOT / "scripts" / "knowledge-sync.sh"), p, str(pdir / "knowledge")]
            what = "synced"
        else:
            cmd = ["bash", str(ROOT / "scripts" / "hermes-profile.sh"), p]
            what = "installed"
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
        if proc.returncode != 0:
            tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or [""]
            raise StepError(f"{cmd[1].rsplit('/', 1)[-1]} {p} exit {proc.returncode}: {tail[0][:300]}")
        if not (pdir / "knowledge").is_dir():
            raise StepError(f"{p}: {pdir / 'knowledge'} missing after {what}")
        out[p] = what
        log(f"install {p}: {what} into {pdir / 'knowledge'}")
    return out


def run_learn(s: Settings, profiles: Sequence[str]) -> Dict[str, object]:
    cmd = [sys.executable, str(ROOT / "scripts" / "knowledge-learn.py"), *profiles, "--timeout", str(s.learn_timeout)]
    env = dict(os.environ, HERMES_HOME=str(s.hermes_home), KNOWLEDGE_CANONICAL=str(s.canon))
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    log("learn: " + " ".join(cmd[1:]))
    proc = subprocess.run(cmd, env=env, check=False)
    status = subprocess.run([sys.executable, str(ROOT / "scripts" / "knowledge-learn.py"), "--status", "--json", *profiles],
                            capture_output=True, text=True, env=env, check=False)
    try:
        rows = json.loads(status.stdout)
    except ValueError:
        rows = []
    if proc.returncode != 0:
        raise StepError(f"knowledge-learn exit {proc.returncode} (state and logs under "
                        f"{s.hermes_home}/profiles/<p>/knowledge-learn/)")
    return {"exit": proc.returncode, "status": rows}


def legacy_duplicates(s: Settings, profiles: Sequence[str]) -> List[Dict[str, object]]:
    """Profile-local .md/.txt (not references) whose bytes equal a canonical file. Read-only."""
    by_hash: Dict[str, Dict[str, str]] = {}
    sources = s.canon / "sources"
    if sources.is_dir():
        for d in sorted(sources.iterdir()):
            meta = d / "source.json"
            if not meta.is_file():
                continue
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for f in m.get("files", []):
                by_hash[str(f.get("sha256"))] = {"source_id": d.name, "canonical_rel": f"sources/{d.name}/{f.get('rel')}",
                                                 "sources": ", ".join(m.get("sources", []))}
    out = []
    for p in profiles:
        root = s.kroot / p
        if not root.is_dir():
            continue
        for f in kp.book_files(root, kp.TEXT_EXTS):
            if kp.is_ref(f):
                continue
            hit = by_hash.get(kp.sha256_file(f))
            if hit:
                out.append({"profile": p, "staged": f"{p}/{f.relative_to(root).as_posix()}", **hit,
                            "note": "legacy profile-local copy; not migrated, not deleted"})
    return out


# --------------------------------------------------------------------------- #
# Printing
# --------------------------------------------------------------------------- #
def print_plan(s: Settings, v: kp.Validated, dry: bool) -> None:
    print()
    print("== knowledge apply plan ==" if not dry else "== knowledge apply plan (DRY-RUN: nothing written) ==")
    print(f"plan: {s.plan_path} (id {s.plan_id})")
    print(f"library: {s.books} (read-only)   canonical: {s.canon}   knowledge root: {s.kroot}")
    print(f"profiles: {s.profiles_dir}   hermes: {s.hermes_home}")
    for name, spec in v.new_profiles.items():
        print(f"- create profile {name}: {spec['purpose']}")
    unique = {v.identities[i['source']].source_id: i['source'] for i in v.assign}
    for it in v.assign:
        ident = v.identities[it["source"]]
        extra = f"   purpose: {it['purpose']}" if it.get("purpose") else ""
        sec = "   (secondary)" if it.get("secondary_reason") else ""
        print(f"- assign {it['profile']} <- {it['source']}   canonical {ident.source_id}{sec}{extra}")
    for it in v.skipped:
        print(f"- skip   {it['source']}")
    for it in v.existing:
        print(f"- keep   {it['source']} (existing: {', '.join(it.get('owners') or []) or 'already owned'})")
    print(f"canonical copies to make or verify: {len(unique)}   profiles affected: {', '.join(v.affected) or '-'}")
    steps = "validate -> profiles -> assignments -> canonical -> references -> install -> learn -> report"
    if not s.install:
        steps = steps.replace(" -> install -> learn", " (install and learn skipped: --no-install)")
    elif not s.learn:
        steps = steps.replace(" -> learn", " (learn skipped: --no-learn)")
    print(f"steps: {steps}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="knowledge-apply", description="Apply one reviewed knowledge plan.")
    p.add_argument("--plan", required=True, help="plan JSON written by knowledge-classify (edited by you)")
    p.add_argument("--approve", action="store_true", help="apply the plan exactly as written (APPROVE=1)")
    p.add_argument("--dry-run", action="store_true", help="validate and show the actions; write nothing")
    p.add_argument("--no-learn", action="store_true", help="stop after install/sync; do not run knowledge-learn")
    p.add_argument("--no-install", action="store_true", help="stop after references; no Hermes profile changes")
    p.add_argument("--legacy-report", action="store_true",
                   help="read-only: list profile-local copies of books that have a canonical copy")
    return p


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    s = Settings(args)
    try:
        s.check()
        plan = kp.load_plan(s.plan_path)
        s.plan_id = plan["_plan_id"]
        ck = Checkpoint(s)
        if args.legacy_report:
            names = sorted(kp.existing_profiles(s.profiles_dir))
            dups = legacy_duplicates(s, names)
            print(f"== legacy duplicates (read-only) ==\ncanonical: {s.canon}   knowledge root: {s.kroot}")
            for d in dups:
                print(f"- {d['staged']}  ==  {d['canonical_rel']}  (library: {d['sources']})")
            print(f"{len(dups)} profile-local copy/copies of canonical books. {LEGACY_NOTE}")
            return 0
        v = kp.validate_plan(plan, s.books, s.profiles_dir, ck.data.get("profiles_created", []))
    except kp.PlanError as exc:
        err(str(exc))
        return 1
    print_plan(s, v, args.dry_run)
    if args.dry_run:
        print("\nDRY-RUN: validated; nothing was written. Apply with: " + s.resume_cmd())
        return 0
    if not args.approve:
        err("the plan validates, and nothing was applied: approval is required. "
            f"Re-run with APPROVE=1 on the make command line: {s.resume_cmd()}")
        return 1

    s.run_dir.mkdir(parents=True, exist_ok=True)
    ck.data["profiles"] = v.affected
    ck.done("validate", items=len(plan["items"]), assign=len(v.assign))
    report: Dict[str, object] = {"plan": str(s.plan_path), "plan_id": s.plan_id, "started": kp.now_iso(),
                                 "profiles": v.affected, "outcome": "running"}
    step = "profiles"
    try:
        report["profiles_created"] = create_profiles(s, v, ck)
        ck.done(step, created=report["profiles_created"])
        step = "assignments"
        report["assigned"] = write_assignments(s, v)
        ck.done(step, count=len(report["assigned"]))
        step = "canonical"
        canon_meta: Dict[str, Dict[str, object]] = {}
        for src, ident in v.identities.items():
            if ident.source_id not in canon_meta and any(i["source"] == src for i in v.assign):
                canon_meta[ident.source_id] = canonicalize(s, ident)
        report["canonical"] = sorted(canon_meta)
        ck.done(step, sources=sorted(canon_meta))
        step = "references"
        report["references"] = write_references(s, v, canon_meta)
        ck.done(step, count=len(report["references"]))
        step = "install"
        if s.install:
            report["install"] = install_profiles(s, v.affected)
            ck.done(step, profiles=report["install"])
        else:
            report["install"] = {"skipped": "--no-install"}
            ck.done(step, skipped=True)
        step = "learn"
        if s.learn:
            report["learn"] = run_learn(s, v.affected)
            ck.done(step)
        else:
            report["learn"] = {"skipped": "--no-learn" if not args.no_install else "--no-install"}
            ck.done(step, skipped=True)
        step = "report"
        report["legacy_duplicates"] = legacy_duplicates(s, v.affected)
        report["legacy_note"] = LEGACY_NOTE
        report["skipped"] = [i["source"] for i in v.skipped]
        report["existing"] = [i["source"] for i in v.existing]
        report["outcome"] = "complete"
        report["finished"] = kp.now_iso()
        (s.run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        ck.done(step)
    except (StepError, kp.PlanError, OSError, subprocess.SubprocessError) as exc:
        ck.failed(step, str(exc))
        report.update(outcome="failed", failed_step=step, error=str(exc)[:600], finished=kp.now_iso())
        (s.run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        err(f"step {step} failed: {exc}")
        print(f"Finished steps are kept; the library and existing knowledge are untouched.\n"
              f"Resume: {s.resume_cmd()}   (same plan; verified work is skipped)\n"
              f"checkpoint: {ck.path}\nreport: {s.run_dir / 'report.json'}")
        return 3

    print()
    print("== knowledge apply report ==")
    print(f"outcome: complete   profiles: {', '.join(v.affected) or '-'}   new: {', '.join(report['profiles_created']) or '-'}")
    print(f"assigned: {len(report['assigned'])}   canonical: {len(report['canonical'])}   references: {len(report['references'])}")
    if report["legacy_duplicates"]:
        print(f"legacy duplicates ({len(report['legacy_duplicates'])}; not migrated, not deleted):")
        for d in report["legacy_duplicates"]:
            print(f"  - {d['staged']}  ==  {d['canonical_rel']}")
    if s.learn:
        for row in report["learn"].get("status", []):
            print(f"learn {row['profile']}: {row['status']} (learned {row['learned']}, todo {row['pending'] + row['changed']}, "
                  f"blocked {row['blocked']})")
    print(f"report: {s.run_dir / 'report.json'}")
    print(f"checkpoint: {ck.path}")
    print("Next: make knowledge-learn-status   (git diff hermes/profiles shows the new book lists and assignments)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        err("interrupted; re-run the same command to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
