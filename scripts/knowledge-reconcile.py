#!/usr/bin/env python3
"""knowledge-reconcile.py — propose (default) or, on explicit approval, record
which library book is the book-list source of staged profile knowledge.

    knowledge-reconcile.py                          proposals only; nothing changes
    knowledge-reconcile.py --scope edu              look for candidates in part of the library
    knowledge-reconcile.py --approve 'alpha=edu/Book/book.epub'
                                                    check an approval (still nothing changes)
    knowledge-reconcile.py --apply --approve 'alpha=edu/Book/book.epub'
    knowledge-reconcile.py --apply --approvals FILE append approved lines

It reports, by content (scripts/knowledge_match.py: bounded normalized-text
fingerprints, exact or strong evidence, never file names):

  A. staged files under data/knowledge/<profile> that no line of that
     profile's book list stages from an existing library path, with every
     library file whose text matches (candidates). Several candidates are all
     listed; none is chosen for you;
  B. library books no book list includes whose text is already staged
     ("already imported, source not linked"), with every owner profile;
  C. library paths listed in more than one book list. The policy is one
     canonical profile per book; these are reported for an operator decision
     and never changed or removed here.

With --apply and an approval `<profile>=<library path>`, the line is appended
to hermes/profiles/<profile>/knowledge-sources.txt only when every book file at
that path matches a staged file of that profile without a source (exact or
strong) and no other profile lists that path or stages its text. Linking
into such a second profile needs an explicit secondary approval
(--secondary PROFILE=SOURCE --secondary-reason TEXT). Nothing is staged, synced, deleted or learned; duplicate staged data
is kept; the library is only read.

Exit 0 when everything was handled, 3 when an approval was refused or could
not be applied, 1 on a usage or safety error. See docs/knowledge-classify.md.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import knowledge_match as km  # noqa: E402


def _load_classify():
    spec = importlib.util.spec_from_file_location("knowledge_classify_shared", HERE / "knowledge-classify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


kc = _load_classify()
SafetyError = kc.SafetyError


@dataclass
class Candidate:
    source: str                  # library file
    evidence: str                # exact | strong
    score: float
    stages_to: str               # path knowledge-stage.sh would write below <kroot>/<profile>
    same_file: bool              # True: a refresh updates this staged file, no second copy
    listed_in: List[str] = field(default_factory=list)


@dataclass
class Orphan:
    profile: str
    staged: str
    reason: str
    candidates: List[Candidate] = field(default_factory=list)
    note: str = ""


@dataclass
class Decision:
    profile: str
    source: str
    action: str = "would-link"   # would-link | linked | already-listed | refused | failed
    note: str = ""
    secondary_reason: str = ""
    error: bool = False


def settings() -> "kc.Settings":
    env = os.environ.get
    return kc.Settings(
        books=Path(env("BOOKS_ROOT", "/mnt/books")),
        kroot=Path(env("KNOWLEDGE_ROOT", str(ROOT / "data" / "knowledge"))),
        staging=Path(env("KNOWLEDGE_STAGING", str(ROOT / "data" / "knowledge-staging"))),
        profiles_dir=Path(env("PROFILES_DIR", str(ROOT / "hermes" / "profiles"))),
        ignore_file=Path(env("KNOWLEDGE_IGNORE", str(ROOT / "hermes" / "knowledge-ignore.txt"))),
        private_file=Path(env("KNOWLEDGE_PRIVATE", str(ROOT / "hermes" / "knowledge-private.txt"))),
        doc2md=Path(env("DOC2MD", str(ROOT / "hermes" / "skills" / "document-converter" / "scripts" / "doc2md.py"))),
        convert=env("KNOWLEDGE_CONVERT", "1") == "1",
    )


def library_files(s, lib: "km.Library", scopes: Sequence[str]) -> List[str]:
    """Library book files to search for candidates (ignored paths left out)."""
    books = s.books.resolve()
    ignored = kc.private_policy(s.ignore_file)
    roots = list(scopes) or sorted(p.name for p in books.iterdir()
                                   if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    out: List[str] = []
    for r in roots:
        for f in km.walk_files(books / r, km.BOOK_EXTS):
            rel = f.relative_to(books).as_posix()
            if "/" in rel and not kc.covered_by(rel, ignored):
                out.append(rel)
    return out


def candidates_for(orphans: List[Orphan], corpus: "km.Corpus", lib: "km.Library", files: Sequence[str],
                   manifests: Dict[str, List[str]]) -> None:
    by_key = {(sf.profile, sf.rel): sf for sf in corpus.files}
    live = [o for o in orphans if by_key[(o.profile, o.staged)].fp is not None]
    for o in orphans:
        if by_key[(o.profile, o.staged)].fp is None:
            o.note = f"fewer than {km.MIN_TOKENS} words of text: cannot be matched by content"
    if not live or not files:
        return
    print(f"[knowledge-reconcile] fingerprinting {len(files)} library file(s)", file=sys.stderr)
    fps = lib.fingerprints(files)
    for o in live:
        target = by_key[(o.profile, o.staged)].fp
        for f in files:
            ev, score = km.compare(target, fps[f][0])
            if ev:
                out = km.stage_output(f)
                o.candidates.append(Candidate(f, ev, score, out, out == o.staged,
                                              sorted(p for p, es in manifests.items() if kc.covered_by(f, es))))


def analyse(s, lib, corpus, manifests, scopes, with_candidates: bool):
    orphans = [Orphan(sf.profile, sf.rel, sf.orphan_reason) for sf in corpus.orphans()]
    if with_candidates:
        candidates_for(orphans, corpus, lib, library_files(s, lib, scopes) if orphans else [], manifests)
    else:
        for o in orphans:
            o.note = "candidates not searched in an approvals run (run without APPROVE for proposals)"
    imported = []
    for rel in kc.uncategorized(s):
        sm = km.match_source(lib, corpus, rel)
        if sm.state != "new":
            imported.append(sm)
    return orphans, imported


def multi_owner(manifests: Dict[str, List[str]]) -> List[Dict[str, object]]:
    """Book-list lines covered by more than one profile's book list (reported, never changed)."""
    out = []
    for e in sorted({e for es in manifests.values() for e in es}):
        owners = sorted(p for p, es in manifests.items() if kc.covered_by(e, es))
        if len(owners) > 1:
            out.append({"source": e, "profiles": owners})
    return out


def decide(s, lib, corpus, manifests, approvals: Dict[str, str], profiles, apply: bool,
           secondary: Dict[str, str], reason: str) -> List[Decision]:
    """Check each approval against content evidence; append only with --apply."""
    out = []
    for rel, prof in approvals.items():
        d = Decision(prof, rel)
        out.append(d)
        if prof not in profiles:
            d.action, d.error, d.note = "refused", True, f"{prof!r} is not a book-backed profile"
            continue
        if rel in manifests.get(prof, []):
            d.action, d.note = "already-listed", "already in the book list"
            continue
        files = lib.book_files(rel)
        orphans = [sf for sf in corpus.orphans() if sf.profile == prof and sf.fp is not None]
        fps = lib.fingerprints(files)
        missing = []
        for f in files:
            hits = [sf.rel for sf in orphans if km.compare(fps[f][0], sf.fp)[0]]
            if not hits:
                missing.append(f)
        if not files or missing:
            d.action, d.error = "refused", True
            d.note = ("no exact/strong content evidence that " + (", ".join(missing) or rel)
                      + f" is the source of a staged file in {prof} without a book-list source")
            continue
        others = {p for p, es in manifests.items() if p != prof and kc.covered_by(rel, es)}
        others |= {m.profile for f in files for m in corpus.matches(fps[f][0]) if m.profile != prof}
        if others and rel not in secondary:
            d.action, d.error, d.note = "refused", True, kc.secondary_refusal(prof, rel, sorted(others))
            continue
        if others:
            d.secondary_reason = reason
        second = [km.stage_output(f) for f in files
                  if km.stage_output(f) not in {sf.rel for sf in orphans}]
        if second:
            d.note = ("next refresh also stages " + ", ".join(second)
                      + " (a second copy; nothing is deleted)")
        if not apply:
            continue
        try:
            kc.append_manifest(profiles[prof].manifest, rel)
            d.action = "linked"
        except (SafetyError, OSError) as exc:
            d.action, d.error, d.note = "failed", True, str(exc)
    return out


def write_report(s, orphans, imported, multi, decisions, applied: bool, report: Optional[Path]):
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report or s.staging / "reports" / f"reconcile-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"run": {"generated": _dt.datetime.now().isoformat(timespec="seconds"), "applied": applied,
                    "library": str(s.books), "knowledge_root": str(s.kroot), "strong_threshold": km.STRONG},
            "staged_without_source": [asdict(o) for o in orphans],
            "already_imported": [asdict(sm) for sm in imported],
            "multi_owner": multi,
            "decisions": [asdict(d) for d in decisions]}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    appr = path.with_suffix(".approvals.txt")
    lines = ["# Reconcile approvals. Uncomment ONE source per staged file you want linked, then:",
             "#   make knowledge-reconcile APPLY=1 APPROVALS=" + str(appr),
             "# Format: <profile>=<library path>. Applying appends to the book list only."]
    for o in orphans:
        if o.candidates:
            lines.append(f"# {o.profile}: {o.staged}")
            for c in o.candidates:
                lines.append(f"#   {c.evidence} {c.score:.2f}, "
                             + ("same staged file" if c.same_file else f"would also stage {c.stages_to}"))
                lines.append(f"# {o.profile}={c.source}")
    appr.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, appr


def print_report(s, orphans, imported, multi, decisions, applied, rpath, apath, manifests) -> None:
    shq = kc.shq
    print()
    print("== knowledge reconcile report ==")
    print("mode: " + ("APPLY (approved lines appended; nothing staged, synced or deleted)" if applied
                      else "PROPOSAL (nothing was changed)"))
    print(f"library: {s.books} (read-only)   staged: {s.kroot}")
    print()
    print(f"Staged without a manifest source ({len(orphans)}): files no book-list line stages "
          "from an existing library path")
    for o in orphans:
        print(f"- {o.profile}: {o.staged}")
        print(f"    why: {o.reason}")
        if o.note:
            print(f"    note: {o.note}")
        for c in o.candidates:
            listed = f", listed in: {', '.join(c.listed_in)}" if c.listed_in else ""
            where = "refresh updates this same file" if c.same_file else \
                f"refresh would ALSO stage {c.stages_to} (second copy)"
            print(f"    candidate: {c.source}  ({c.evidence} {c.score:.2f}; {where}{listed})")
            if not applied:
                print("      approve: make knowledge-reconcile APPLY=1 APPROVE=" + shq(f"{o.profile}={c.source}"))
        if len(o.candidates) > 1:
            print("    several candidates: pick one canonical source yourself (none is chosen)")
        elif not o.candidates and not o.note:
            print("    no library file with exact/strong evidence: nothing to link (nothing is deleted)")
    print()
    print(f"Already imported, source not linked ({len(imported)}): library books no book list includes "
          "whose text is already staged")
    for sm in imported:
        print(f"- {sm.source}   [{sm.state}]")
        for m in sm.matches:
            print(f"    owner: {m.describe()}")
        if sm.review:
            print(f"    reconciliation review: {', '.join(sm.review)} (owners are reported, never chosen)")
        if all(m.linked_by for m in sm.matches):
            print("    already linked through the source above; nothing to classify or re-ingest. To stop listing it:")
            ignore = os.path.relpath(s.ignore_file, ROOT) if Path(s.ignore_file).resolve().is_relative_to(ROOT) \
                else s.ignore_file
            print(f"      printf '%s\\n' {shq(sm.source)} >> {ignore}")
        else:
            print("    a matching staged file has no source: see its candidates above")
    print()
    print(f"Listed in several book lists ({len(multi)}): policy is one canonical profile per book; keep a "
          "second one only with an explicit secondary approval. Reported only: nothing is changed or removed")
    for o in multi:
        print(f"- {o['source']}: {', '.join(o['profiles'])}")
    if decisions:
        print()
        print("Approvals:")
        for d in decisions:
            print(f"- {d.profile}={d.source}   action: {d.action}" + (f"   note: {d.note}" if d.note else ""))
            if d.secondary_reason:
                print(f"    secondary profile (approved): {d.secondary_reason}")
    print()
    print(f"report: {rpath}")
    print(f"approvals template: {apath}")
    linked = sorted({d.profile for d in decisions if d.action == "linked"})
    if linked:
        print("Next: review `git diff hermes/profiles`; then (not run automatically):")
        for p in linked:
            print(f"  make knowledge-refresh NAME={p}     then /learn in `hermes -p {p} chat` only if it reports updated")
    elif not applied:
        print("Nothing was changed. To link a source, approve it explicitly:")
        print("  make knowledge-reconcile APPLY=1 APPROVE='<profile>=<library path>'   (or APPROVALS=<file>)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="knowledge-reconcile",
                                description="Propose (default) or, on explicit approval, link library sources "
                                            "to staged knowledge that has none.")
    p.add_argument("--apply", action="store_true", help="append approved lines (needs --approve/--approvals)")
    p.add_argument("--approve", action="append", default=[], metavar="PROFILE=SOURCE")
    p.add_argument("--approvals", action="append", default=[], metavar="FILE")
    p.add_argument("--secondary", action="append", default=[], metavar="PROFILE=SOURCE",
                   help="explicitly approve linking a source another profile already lists or stages "
                        "(needs --secondary-reason)")
    p.add_argument("--secondary-reason", metavar="TEXT",
                   help="the substantial, recurring, independent purpose the secondary profile serves")
    p.add_argument("--scope", action="append", default=[], metavar="LIBRARY_PATH",
                   help="search only these library paths for candidates (default: whole library)")
    p.add_argument("--report", type=Path, help="JSON report path (default <staging>/reports/reconcile-<time>.json)")
    return p


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    s = settings()
    kc.check_roots(s)
    profiles = kc.load_profiles(s)
    approvals = kc.load_approvals(args.approve, args.approvals)
    secondary, reason = kc.load_secondary(args.secondary, args.secondary_reason, approvals)
    if args.apply and not approvals:
        raise SafetyError("--apply links only explicit approvals: pass APPROVE='<profile>=<library path>' "
                          "or APPROVALS=<file> (run without APPLY first for proposals)")
    for rel in list(approvals) + list(args.scope):
        kc.safe_rel(s, rel)
    for rel, prof in approvals.items():
        if prof not in profiles:
            raise SafetyError(f"approval for {rel!r}: {prof!r} is not a book-backed profile "
                              f"(choose from: {', '.join(profiles)})")
    manifests = {name: kc.manifest_entries(p.manifest) for name, p in profiles.items()}
    books = s.books.resolve()
    lib = km.Library(books, s.doc2md, convert=s.convert)
    corpus = km.Corpus(books, s.kroot, manifests)
    decisions = decide(s, lib, corpus, manifests, approvals, profiles, args.apply, secondary, reason)
    if any(d.action == "linked" for d in decisions):      # report the state after the append
        manifests = {name: kc.manifest_entries(p.manifest) for name, p in profiles.items()}
        corpus = km.Corpus(books, s.kroot, manifests)
    orphans, imported = analyse(s, lib, corpus, manifests, args.scope, with_candidates=not approvals)
    multi = multi_owner(manifests)
    rpath, apath = write_report(s, orphans, imported, multi, decisions, args.apply, args.report)
    print_report(s, orphans, imported, multi, decisions, args.apply, rpath, apath, manifests)
    return 3 if any(d.error for d in decisions) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(argv)
    except SafetyError as exc:
        print(f"[knowledge-reconcile][error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
