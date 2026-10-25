#!/usr/bin/env python3
"""knowledge_match.py — find library books whose content is already staged in a
profile corpus (data/knowledge/<profile>) under another file name or path.

Shared by knowledge-classify.py, knowledge-reconcile.py and the
knowledge-refresh.sh inventory. Read-only: the library and data/knowledge are
only read; EPUB/PDF text comes from the document-converter in a private temp
folder that is deleted afterwards (never the quarantine staging area).

Fingerprint (bounded, never the file name):
  * read at most MAX_CHARS characters per file;
  * drop converter front matter, Markdown image/link markup, case, Unicode
    compatibility forms, punctuation and all whitespace -> a word stream of at
    most MAX_TOKENS words;
  * exact digest of that stream + a bottom-K sketch of SHINGLE-word shingles.
Two texts match only with evidence:
  exact   identical normalized word stream
  strong  estimated shingle resemblance >= STRONG
and both need MIN_TOKENS words. A near collision (same series, a shared
preface, a revised edition) stays below STRONG and is not a match.

CLI (read-only; used by scripts/knowledge-refresh.sh):
  knowledge_match.py inventory   NUL-separated library paths on stdin; prints
                                 NUL-terminated triples: kind, a, b
      new       <path>      ""
      imported  <path>      <owners and evidence>
      partial   <path>      <owners and evidence>
      orphan    <profile>   <staged file> (<why>)
Env: BOOKS_ROOT, KNOWLEDGE_ROOT, PROFILES_DIR, DOC2MD, KNOWLEDGE_CONVERT.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]

MAX_CHARS = 8_000_000        # characters read per file
MAX_TOKENS = 300_000         # words fingerprinted per file
SHINGLE = 7                  # words per shingle
SKETCH = 1024                # bottom-k sketch size
STRONG = 0.85                # resemblance for a strong match
MIN_TOKENS = 200             # shorter texts never match
BOOK_EXTS = (".epub", ".pdf", ".md", ".txt")
TEXT_EXTS = (".md", ".txt")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_FRONTMATTER_RE = re.compile(r"\A﻿?---\r?\n.*?\r?\n---\r?\n", re.S)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORD_RE = re.compile(r"[^\W_]+")
_MASK = (1 << 64) - 1


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Fingerprint:
    tokens: int
    digest: str
    sketch: frozenset


def normalize(text: str) -> List[str]:
    """Word stream: no front matter, markup, case, punctuation or whitespace."""
    text = _FRONTMATTER_RE.sub("", text[:MAX_CHARS], count=1)
    text = _LINK_RE.sub(r"\1", _IMAGE_RE.sub(" ", text))
    text = unicodedata.normalize("NFKC", text).casefold()
    return _WORD_RE.findall(text)[:MAX_TOKENS]


def fingerprint(text: str) -> Optional[Fingerprint]:
    words = normalize(text)
    if len(words) < MIN_TOKENS:
        return None
    ids = [zlib.crc32(w.encode("utf-8")) for w in words]
    # hash() of an int tuple is deterministic (only str/bytes hashing is salted).
    shingles = {hash(tuple(ids[i:i + SHINGLE])) & _MASK for i in range(len(ids) - SHINGLE + 1)}
    digest = hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()
    return Fingerprint(len(words), digest, frozenset(heapq.nsmallest(SKETCH, shingles)))


def read_bounded(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(MAX_CHARS)


def fingerprint_file(path: Path) -> Optional[Fingerprint]:
    try:
        return fingerprint(read_bounded(path))
    except OSError:
        return None


def resemblance(a: Fingerprint, b: Fingerprint) -> float:
    union = heapq.nsmallest(SKETCH, a.sketch | b.sketch)
    if not union:
        return 0.0
    return sum(1 for h in union if h in a.sketch and h in b.sketch) / len(union)


def compare(a: Optional[Fingerprint], b: Optional[Fingerprint]) -> Tuple[str, float]:
    """('exact' | 'strong' | '', score). Missing or short texts never match."""
    if a is None or b is None:
        return "", 0.0
    if a.digest == b.digest:
        return "exact", 1.0
    if min(a.tokens, b.tokens) < STRONG * max(a.tokens, b.tokens) * 0.9:
        return "", 0.0                       # sizes too far apart for a strong match
    score = resemblance(a, b)
    return ("strong" if score >= STRONG else ""), round(score, 3)


# --------------------------------------------------------------------------- #
# Manifests and the staged corpus
# --------------------------------------------------------------------------- #
def manifest_entries(path: Path) -> List[str]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.rstrip("\r")
        if line.strip() and not line.startswith("#"):
            out.append(line)
    return out


def load_manifests(profiles_dir: Path) -> Dict[str, List[str]]:
    """Book lists of every book-backed profile (not _template, no symlinks)."""
    out: Dict[str, List[str]] = {}
    for m in sorted(profiles_dir.glob("*/knowledge-sources.txt")):
        name = m.parent.name
        if name.startswith("_") or not NAME_RE.match(name) or m.is_symlink():
            continue
        out[name] = manifest_entries(m)
    return out


def stage_output(rel: str) -> str:
    """Where knowledge-stage.sh puts a library file: below the collection, .md for EPUB/PDF."""
    rest = rel.split("/", 1)[1] if "/" in rel else rel
    stem, ext = os.path.splitext(rest)
    return stem + ".md" if ext.lower() in (".epub", ".pdf") else rest


def produces(entry: str, staged_rel: str) -> bool:
    """True when manifest entry <collection>/<rest> stages to staged_rel."""
    if "/" not in entry:
        return False
    rest = entry.split("/", 1)[1].rstrip("/")
    if staged_rel == rest or staged_rel.startswith(rest + "/"):
        return True
    stem, ext = os.path.splitext(rest)
    return ext.lower() in (".epub", ".pdf") and staged_rel == stem + ".md"


def walk_files(root: Path, exts: Sequence[str]) -> List[Path]:
    """Regular, non-hidden files below root (symlinks are not followed)."""
    if root.is_symlink():
        return []
    if root.is_file():
        return [root] if root.suffix.lower() in exts else []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if not f.startswith(".") and p.suffix.lower() in exts and not p.is_symlink() and p.is_file():
                out.append(p)
    return out


@dataclass
class StagedFile:
    profile: str
    rel: str                                   # below <kroot>/<profile>
    path: Path
    linked_by: List[str] = field(default_factory=list)    # manifest entries present in the library
    missing_by: List[str] = field(default_factory=list)   # manifest entries missing from the library
    _fp: object = None

    @property
    def fp(self) -> Optional[Fingerprint]:
        if self._fp is None:
            self._fp = fingerprint_file(self.path) or False
        return self._fp or None

    @property
    def orphan_reason(self) -> str:
        """'' when a manifest entry of its profile stages it from an existing library path."""
        if self.linked_by:
            return ""
        if self.missing_by:
            return "manifest source missing from the library: " + ", ".join(self.missing_by)
        return "no book-list entry stages this file"


@dataclass
class Match:
    profile: str
    staged: str
    evidence: str            # exact | strong
    score: float
    linked_by: List[str]

    def describe(self) -> str:
        via = f"via {', '.join(self.linked_by)}" if self.linked_by else "no manifest source"
        return f"{self.profile}: {self.staged} ({self.evidence} {self.score:.2f}, {via})"


class Corpus:
    """Every .md/.txt under <kroot>/<profile>/, with its manifest linkage."""

    def __init__(self, books: Path, kroot: Path, manifests: Dict[str, List[str]]):
        self.books, self.kroot, self.manifests = books, kroot, manifests
        self.files: List[StagedFile] = []
        if not kroot.is_dir():
            return
        for pdir in sorted(kroot.iterdir()):
            if not pdir.is_dir() or pdir.is_symlink() or not NAME_RE.match(pdir.name):
                continue
            entries = manifests.get(pdir.name, [])
            for f in walk_files(pdir, TEXT_EXTS):
                rel = f.relative_to(pdir).as_posix()
                if rel.startswith("refs/") and rel.endswith(".ref.md"):
                    continue                         # canonical reference, not book text
                sf = StagedFile(pdir.name, rel, f)
                for e in entries:
                    if e.startswith("/") or ".." in e.split("/"):
                        continue                     # knowledge-stage.sh refuses these
                    if produces(e, rel):
                        exists = (books / e).exists() and not (books / e).is_symlink()
                        (sf.linked_by if exists else sf.missing_by).append(e)
                self.files.append(sf)

    def orphans(self) -> List[StagedFile]:
        return [f for f in self.files if f.orphan_reason]

    def matches(self, fp: Optional[Fingerprint]) -> List[Match]:
        out = []
        if fp is None:
            return out
        for sf in self.files:
            ev, score = compare(fp, sf.fp)
            if ev:
                out.append(Match(sf.profile, sf.rel, ev, score, list(sf.linked_by)))
        return out


# --------------------------------------------------------------------------- #
# Library sources
# --------------------------------------------------------------------------- #
class Library:
    """Read-only access to library book text. EPUB/PDF go through the converter
    into a private temp folder that is removed right away; results are cached
    per file for the run."""

    def __init__(self, books: Path, doc2md: Path, convert: bool = True):
        self.books, self.doc2md, self.convert = books, doc2md, convert
        self._fp: Dict[str, Tuple[Optional[Fingerprint], str]] = {}

    def book_files(self, rel: str) -> List[str]:
        src = self.books / rel
        return [p.relative_to(self.books).as_posix() for p in walk_files(src, BOOK_EXTS)]

    def fingerprints(self, rels: Sequence[str]) -> Dict[str, Tuple[Optional[Fingerprint], str]]:
        """{file rel: (fingerprint or None, problem)} for library files. A problem
        means the text could not be read; a text too short to match has none."""
        todo = [r for r in rels if r not in self._fp]
        convert: Dict[str, List[str]] = {}
        for r in todo:
            p = self.books / r
            if p.suffix.lower() in TEXT_EXTS:
                try:
                    self._fp[r] = (fingerprint(read_bounded(p)), "")   # too short: checked, no match
                except OSError as exc:
                    self._fp[r] = (None, f"unreadable: {type(exc).__name__}")
            elif not self.convert:
                self._fp[r] = (None, "EPUB/PDF not checked (conversion off)")
            else:
                coll, rest = r.split("/", 1)
                convert.setdefault(coll, []).append(rest)
        for coll, rests in convert.items():
            self._convert(coll, rests)
        return {r: self._fp[r] for r in rels}

    def _convert(self, coll: str, rests: List[str]) -> None:
        tmp = tempfile.mkdtemp(prefix="knowledge-match.")
        try:
            outputs: Dict[str, str] = {}
            if self.doc2md.is_file():
                proc = subprocess.run([sys.executable, str(self.doc2md), "--source", str(self.books / coll),
                                       "--dest", tmp, "--json", "--ocr", "never", "--", *rests],
                                      capture_output=True, text=True, check=False, timeout=1800)
                try:
                    for res in json.loads(proc.stdout).get("results", []):
                        if res.get("status") in ("converted", "partial") and res.get("output"):
                            outputs[res.get("source", "")] = res["output"]
                except ValueError:
                    pass
            for rest in rests:
                out = outputs.get(rest)
                rel = f"{coll}/{rest}"
                if out and Path(out).is_file() and Path(out).resolve().is_relative_to(Path(tmp).resolve()):
                    self._fp[rel] = (fingerprint(read_bounded(Path(out))), "")
                else:
                    self._fp[rel] = (None, "could not extract text")
        except (OSError, subprocess.SubprocessError) as exc:
            for rest in rests:
                self._fp.setdefault(f"{coll}/{rest}", (None, f"converter failed: {type(exc).__name__}"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class SourceMatch:
    source: str
    state: str                        # already-imported | partially-imported | new
    owners: List[str] = field(default_factory=list)
    matches: List[Match] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    review: List[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [m.describe() for m in self.matches]
        if self.unmatched:
            parts.append("not staged: " + ", ".join(self.unmatched))
        if self.review:
            parts.append("reconciliation review: " + ", ".join(self.review))
        return "; ".join(parts)


def match_source(lib: Library, corpus: Corpus, rel: str) -> SourceMatch:
    """Classify one library path against the staged corpus. Never picks an owner."""
    files = lib.book_files(rel)
    fps = lib.fingerprints(files)
    sm = SourceMatch(rel, "new")
    matched = 0
    for f in files:
        fp, problem = fps[f]
        hits = corpus.matches(fp)
        if hits:
            matched += 1
            sm.matches += hits
        else:
            sm.unmatched.append(f)
            if problem:
                sm.problems.append(f"{f}: {problem}")
    sm.owners = sorted({m.profile for m in sm.matches})
    if matched and matched == len(files):
        sm.state = "already-imported"
    elif matched:
        sm.state = "partially-imported"
        sm.review.append("partially-imported")
    if len(sm.owners) > 1:
        sm.review.append("multiple-owners")
    return sm


# --------------------------------------------------------------------------- #
# CLI (inventory porcelain for knowledge-refresh.sh)
# --------------------------------------------------------------------------- #
def from_env() -> Tuple[Library, Corpus]:
    env = os.environ.get
    books = Path(env("BOOKS_ROOT", "/mnt/books")).resolve()
    kroot = Path(env("KNOWLEDGE_ROOT", str(ROOT / "data" / "knowledge")))
    profiles_dir = Path(env("PROFILES_DIR", str(ROOT / "hermes" / "profiles")))
    doc2md = Path(env("DOC2MD", str(ROOT / "hermes" / "skills" / "document-converter" / "scripts" / "doc2md.py")))
    lib = Library(books, doc2md, convert=env("KNOWLEDGE_CONVERT", "1") == "1")
    return lib, Corpus(books, kroot, load_manifests(profiles_dir))


def inventory(sources: Iterable[str], out) -> None:
    lib, corpus = from_env()

    def rec(*fields: str) -> None:
        out.write(b"".join(f.encode("utf-8", "surrogateescape") + b"\0" for f in fields))

    for rel in sources:
        sm = match_source(lib, corpus, rel)
        kind = {"already-imported": "imported", "partially-imported": "partial"}.get(sm.state, "new")
        rec(kind, rel, sm.describe() if kind != "new" else "; ".join(sm.problems))
    for sf in corpus.orphans():
        rec("orphan", sf.profile, f"{sf.rel} ({sf.orphan_reason})")


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["inventory"]:
        print(__doc__.split("CLI", 1)[1].strip(), file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    inventory([x for x in data.split("\0") if x], sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
