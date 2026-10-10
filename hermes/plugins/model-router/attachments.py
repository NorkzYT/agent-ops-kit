"""Exact separation of sender prose from inline attachment relays.

Wire shape (verified read-only against the installed core): a platform
adapter caches each file (``gateway.platforms.base.cache_document_from_bytes``
-> ``<HERMES_HOME>/cache/documents/doc_<hex12>_<name>``) and, for small text
files, PREPENDS ``f"[Content of {name}]:\\n{file_text}"`` to the sender's text
(several relays joined by a blank line; Discord/Telegram/Slack decode the bytes
strictly, WhatsApp/Feishu use ``read_text``). The gateway then adds the
``[sender] `` prefix, channel backfill + ``[New message]``, and one document
note per file IN FRONT (``gateway/run.py::_build_document_context_note``)
naming the file and its cache path. The relay has no closing delimiter, so the
end of the file text is only provable by comparing it with the cached file.

:func:`separate_sender_prose` does exactly that and fails closed: a relay
counts as attached content only when a gateway note in the runtime head names
a canonical regular file directly in the active document cache whose decoded
content follows the relay header verbatim, ending at a blank line or at the end
of the turn. Anything it cannot prove (relay without a note, a note path
outside the cache, a symlink, a missing/oversized/mismatched file, two notes
matching at different lengths, wire markers inside a file, a relay header left
in the remaining prose) is ``proven=False`` and the caller routes nothing.

Only the bounded file bytes are read, never echoed: the result carries the
sender's prose, stable labels and (``attached``, never in ``repr``) the text of
the verified relays exactly as they appear in the turn, so a caller can route
an attachment-only turn on the file the sender uploaded as the prompt.

:func:`read_documents` is the one path that reads a document the gateway did
NOT inline (large text, PDF, DOCX), for the specialist classifier only. It
accepts exactly the gateway notes in the runtime head that name a regular,
non-symlink file directly in the active cache, reads at most a bounded prefix
(text) or extracts bounded text locally (``pdftotext`` for PDF, ``zipfile``
for DOCX, inside a time budget) and returns the text with content-free labels.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import stat
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import task_intent as ti

# Bounds. Every adapter inlines at most 100 KB; a cached file that cannot fit in
# the turn (> 4 bytes per character of the raw text) cannot have been relayed
# and is stat-validated but never read.
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_NOTES = 32
# Non-inlined documents (read_documents): bounded reads and local extraction.
MAX_DOCUMENTS = 4
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024     # stat bound for PDF/DOCX
DOC_TEXT_READ_BYTES = 256 * 1024          # prefix of a large text file
DOC_MAX_CHARS = 8000                      # text kept per document
PDF_MAX_PAGES = 30
PDF_TIMEOUT = 6.0                         # seconds per PDF, inside the caller's deadline
DOCX_MAX_XML_BYTES = 8 * 1024 * 1024

# Container/remote spellings of the cache dir in a note
# (tools.credential_files.to_agent_visible_cache_path); the file is always read
# from the ACTIVE cache dir by basename.
_TRANSLATED_CACHE_DIRS = ("/root/.hermes/cache/documents", "~/.hermes/cache/documents")

_NOTE_LOOSE_RE = re.compile(r"\[The user sent (?:a text document|a document): '")
_NOTE_RES = (
    re.compile(r"(?m)^\[The user sent a text document: '(?P<name>[^'\n]*)'\. Its content has been "
               r"included below\. The file is also saved at: (?P<path>[^\n]+)\]$"),
    re.compile(r"(?m)^\[The user sent a text document: '(?P<name>[^'\n]*)'\. It is saved at: "
               r"(?P<path>[^\n]+)\. Its content is not inlined here\. Read the cached file yourself "
               r"before answering when the user's request involves its contents\.\]$"),
    re.compile(r"(?m)^\[The user sent a document: '(?P<name>[^'\n]*)'\. It is saved at: "
               r"(?P<path>[^\n]+)\. Its text is not inlined here \(it's a binary format such as PDF "
               r"or DOCX\)\. To read it, extract the document's text yourself — for example with "
               r"the terminal tool or the ocr-and-documents skill — before answering, instead of "
               r"asking the user to paste the contents\.\]$"),
)
_HEADER_LINE_RE = re.compile(r"(?m)^\[Content of (?P<name>[^\n]*?)\]:$")
# Any relay header left in prose (also mid-line, e.g. behind an unstripped prefix).
_HEADER_ANYWHERE_RE = re.compile(r"(?m)\[Content of [^\n]*\]:$")
_UNSAFE_NAME_RE = re.compile(r"[^\w.\- ]")
_BLANK = "\n\n"


@dataclass(frozen=True)
class Separation:
    """``attachment``: a relay or document note is present. ``proven``: the
    sender's prose was separated exactly (always True without an attachment).
    ``prose``: sender-typed text ('' when attachment-only or unproven).
    ``attached``: the verified relay bodies, blank-line joined ('' unless
    proven; a non-inlined document is never read for its text).
    ``attached_parts``: the same bodies, one per relay."""

    attachment: bool
    proven: bool
    prose: str = field(repr=False)
    reason: str = "none"
    attached: str = field(default="", repr=False)
    attached_parts: Tuple[str, ...] = field(default=(), repr=False)


@dataclass
class _Doc:
    names: Tuple[str, ...]
    variants: Tuple[str, ...] = ()


class _Unproven(Exception):
    pass


def active_cache_dir() -> Path:
    """``$HERMES_HOME/cache/documents`` (process home, as the gateway resolves it)."""
    home = os.environ.get("HERMES_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".hermes") / "cache" / "documents"


def separate_sender_prose(raw_text: str, *, cache_dir: Optional[Path] = None) -> Separation:
    """Split the current turn into sender prose and verified attachment relays."""
    head, seg = ti.split_sender_segment(raw_text or "")
    headers = list(_HEADER_LINE_RE.finditer(seg))
    if not (_NOTE_LOOSE_RE.search(head) or _NOTE_LOOSE_RE.search(seg) or headers
            or _HEADER_ANYWHERE_RE.search(seg)):
        return Separation(False, True, seg)
    try:
        docs = _load_docs(head, len(raw_text), headers, cache_dir or active_cache_dir())
        prose, bodies = _strip_relays(seg, headers, docs)
    except _Unproven as exc:
        return Separation(True, False, "", f"unproven:{exc}")
    except OSError:
        return Separation(True, False, "", "unproven:io")
    return Separation(True, True, prose, "separated" if prose else "attachment-only",
                      _BLANK.join(bodies), tuple(bodies))


def has_document_note(raw_text: str) -> bool:
    """A gateway note for a NON-inlined document (large text, PDF, DOCX) sits
    in the runtime head. Text only; nothing is read."""
    head, _seg = ti.split_sender_segment(raw_text or "")
    return any(rx.search(head) for rx in _NOTE_RES[1:])


def wire_text_marker(text: str) -> str:
    """Label of gateway/runtime wire text inside attachment content ('' when
    none): runtime envelopes and blocks (``task_intent.runtime_marker``) plus
    relay headers and document notes the file itself quotes."""
    marker = ti.runtime_marker(text)
    if marker:
        return marker
    if _HEADER_ANYWHERE_RE.search(text) or _NOTE_LOOSE_RE.search(text):
        return "relay"
    return ""


def _load_docs(head: str, raw_len: int, headers: list, cache_dir: Path) -> List[_Doc]:
    notes = [m for rx in _NOTE_RES for m in rx.finditer(head)]
    if len(notes) != len(_NOTE_LOOSE_RE.findall(head)):
        raise _Unproven("note-format")
    if len(notes) > MAX_NOTES:
        raise _Unproven("too-many-notes")
    if not cache_dir.is_absolute():
        raise _Unproven("cache-dir")
    # Contents matter when a relay header must be matched, or when the backfill
    # cut ([New message], last occurrence) could have landed inside a file.
    need_content = bool(headers) or ti.NEW_MESSAGE_MARKER in head
    budget = MAX_TOTAL_BYTES
    docs: Dict[str, _Doc] = {}
    for note in notes:
        base = _cache_basename(note.group("path"), cache_dir)
        if base is None:
            raise _Unproven("note-path")
        shown = base.split("_", 2)[2] if len(base.split("_", 2)) >= 3 else base
        if note.group("name") != _UNSAFE_NAME_RE.sub("_", shown):
            raise _Unproven("note-name")
        if base in docs:
            continue
        doc = _Doc(names=(_UNSAFE_NAME_RE.sub("_", shown), shown, base))
        data = _read_cached(cache_dir, base, raw_len, need_content)
        if data is not None:
            budget -= len(data)
            if budget < 0:
                raise _Unproven("total-size")
            doc.variants = _decodings(data)
            if ti.NEW_MESSAGE_MARKER in head and any("[New message]" in v for v in doc.variants):
                raise _Unproven("marker-in-file")
        docs[base] = doc
    return list(docs.values())


def _cache_basename(path: str, cache_dir: Path) -> Optional[str]:
    """Basename of a note path that names a file DIRECTLY in the document cache."""
    if "\x00" in path:
        return None
    parent, sep, base = path.rpartition("/")
    if not sep or base in ("", ".", "..") or len(base.encode("utf-8", "surrogatepass")) > 255:
        return None
    if parent != str(cache_dir) and parent not in _TRANSLATED_CACHE_DIRS:
        return None
    return base


def _lstat_regular(cache_dir: Path, base: str) -> os.stat_result:
    try:
        st = os.lstat(os.path.join(str(cache_dir), base))
    except OSError:
        raise _Unproven("missing")
    if not stat.S_ISREG(st.st_mode):  # symlink, fifo, dir, device
        raise _Unproven("not-regular")
    return st


def _open_regular(cache_dir: Path, base: str, st: os.stat_result) -> int:
    """fd of the lstat-checked file (O_NOFOLLOW + same-inode fstat)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.path.join(str(cache_dir), base), flags)
    fst = os.fstat(fd)
    if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise _Unproven("changed")
    return fd


def _read_fd(fd: int, limit: int) -> bytes:
    chunks, left = [], limit
    while left > 0:
        chunk = os.read(fd, min(left, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _read_cached(cache_dir: Path, base: str, raw_len: int, need_content: bool) -> Optional[bytes]:
    """Bounded bytes of a regular, non-symlink file directly in the cache dir
    (``base`` has no separator; lstat + O_NOFOLLOW + same-inode fstat); None
    when not needed or too large to have been relayed in ``raw_len`` chars."""
    st = _lstat_regular(cache_dir, base)
    if not need_content or st.st_size > 4 * raw_len:
        return None
    if st.st_size > MAX_FILE_BYTES:
        raise _Unproven("oversize")
    fd = _open_regular(cache_dir, base, st)
    try:
        data = _read_fd(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise _Unproven("oversize")
    return data


def _universal_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decodings(data: bytes) -> Tuple[str, ...]:
    """Every text an adapter can relay for these bytes: strict UTF-8 decode
    (Discord/Telegram/Slack), strict ``read_text`` (Feishu), and ``read_text``
    with ``errors='replace'`` (WhatsApp)."""
    out: List[str] = []
    try:
        strict = data.decode("utf-8")
        out += [strict, _universal_newlines(strict)]
    except UnicodeDecodeError:
        pass
    out.append(_universal_newlines(data.decode("utf-8", errors="replace")))
    return tuple(dict.fromkeys(out))


def _relay_end(seg: str, at: int, name: str, docs: List[_Doc]) -> int:
    """End of the verified relay starting at ``at`` (past its blank-line separator)."""
    ends = set()
    named = False
    for doc in docs:
        for text in doc.variants:
            relay = f"[Content of {name}]:\n{text}"
            end = at + len(relay)
            if seg.startswith(relay, at) and (end == len(seg) or seg.startswith(_BLANK, end)):
                ends.add(min(end + len(_BLANK), len(seg)))
            elif seg[at:] == relay.rstrip():  # last relay, trailing whitespace stripped
                ends.add(len(seg))
            else:
                continue
            named = named or name in doc.names
    if len(ends) != 1 or not named:
        raise _Unproven("relay-mismatch" if not ends else "relay-ambiguous" if named else "relay-name")
    return ends.pop()


def _strip_relays(seg: str, headers: list, docs: List[_Doc]) -> Tuple[str, List[str]]:
    """``(prose, bodies)``: the sender's paragraphs and the verified relay bodies."""
    parts: List[str] = []
    bodies: List[str] = []
    pos = 0
    for header in headers:
        at = header.start()
        if at < pos:  # inside an already verified relay
            continue
        before = seg[pos:at]
        if before:
            if not before.endswith(_BLANK):
                raise _Unproven("boundary")
            parts.append(before[: -len(_BLANK)])
        pos = _relay_end(seg, at, header.group("name"), docs)
        bodies.append(seg[header.end() + 1:pos].strip())
    parts.append(seg[pos:])
    prose = _BLANK.join(p.strip() for p in parts if p.strip())
    if _HEADER_ANYWHERE_RE.search(prose) or _NOTE_LOOSE_RE.search(prose):
        raise _Unproven("residual-relay")
    return prose, [b for b in bodies if b]


# --------------------------------------------------------------------------- #
# Non-inlined documents (specialist classifier context only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Document:
    """Bounded text of one verified, non-inlined document. ``kind`` is
    ``text`` | ``pdf`` | ``docx``; the text is never in ``repr``."""

    kind: str
    text: str = field(repr=False)


def read_documents(raw_text: str, *, cache_dir: Optional[Path] = None,
                   deadline: Optional[float] = None) -> Tuple[List[Document], List[str]]:
    """``(documents, labels)`` for the documents the gateway did NOT inline.

    Only notes in the runtime head (``split_sender_segment``) count, in the
    gateway's exact wording, naming a regular non-symlink file directly in the
    active document cache whose display name matches. Inline-relay notes are
    skipped (their verified text is ``Separation.attached_parts``). Large text
    files give a bounded prefix; PDF goes through local ``pdftotext`` (first
    :data:`PDF_MAX_PAGES` pages, :data:`PDF_TIMEOUT` seconds, never past
    ``deadline``); DOCX through ``zipfile`` (bounded XML). Nothing leaves the
    host here. ``labels`` are content-free (``pdf:ok``, ``pdf:timeout``,
    ``unsupported``, ``unproven:note-path``, ...); a failure skips that
    document and never raises.
    """
    head, _seg = ti.split_sender_segment(raw_text or "")
    cache_dir = cache_dir or active_cache_dir()
    if not cache_dir.is_absolute():
        return [], ["unproven:cache-dir"]
    notes = []
    for kind, rx in (("text", _NOTE_RES[1]), ("binary", _NOTE_RES[2])):
        notes += [(m.start(), kind, m) for m in rx.finditer(head)]
    docs: List[Document] = []
    labels: List[str] = []
    seen = set()
    for _pos, kind, note in sorted(notes, key=lambda n: n[0])[:MAX_DOCUMENTS]:
        base = _cache_basename(note.group("path"), cache_dir)
        if base is None:
            labels.append("unproven:note-path")
            continue
        shown = base.split("_", 2)[2] if len(base.split("_", 2)) >= 3 else base
        if note.group("name") != _UNSAFE_NAME_RE.sub("_", shown):
            labels.append("unproven:note-name")
            continue
        if base in seen:
            continue
        seen.add(base)
        try:
            doc = _extract(cache_dir, base, kind, shown, deadline)
        except _Unproven as exc:
            labels.append(f"unproven:{exc}")
            continue
        except _Skip as exc:
            labels.append(str(exc))
            continue
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            labels.append("error")
            continue
        labels.append(f"{doc.kind}:ok" if doc.text else f"{doc.kind}:empty")
        if doc.text:
            docs.append(doc)
    return docs, labels


class _Skip(Exception):
    pass


def _extract(cache_dir: Path, base: str, kind: str, shown: str, deadline: Optional[float]) -> Document:
    st = _lstat_regular(cache_dir, base)
    suffix = os.path.splitext(shown)[1].lower()
    if kind == "text":
        fd = _open_regular(cache_dir, base, st)
        try:
            data = _read_fd(fd, DOC_TEXT_READ_BYTES)
        finally:
            os.close(fd)
        return Document("text", _bound(_universal_newlines(data.decode("utf-8", errors="replace"))))
    if suffix not in (".pdf", ".docx"):
        raise _Skip("unsupported")
    if st.st_size > MAX_DOCUMENT_BYTES:
        raise _Skip(f"{suffix[1:]}:oversize")
    fd = _open_regular(cache_dir, base, st)
    try:
        if suffix == ".pdf":
            return Document("pdf", _bound(_pdf_text(fd, deadline)))
        return Document("docx", _bound(_docx_text(fd)))
    finally:
        os.close(fd)


def _bound(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.replace("\x00", "").splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:DOC_MAX_CHARS]


def _pdf_text(fd: int, deadline: Optional[float]) -> str:
    """Local text-layer extraction of the open PDF (no OCR, no network)."""
    if os.pread(fd, 5, 0) != b"%PDF-":
        raise _Skip("pdf:not-pdf")
    tool = shutil.which("pdftotext")
    if not tool:
        raise _Skip("pdf:no-extractor")
    timeout = PDF_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout < 0.5:
        raise _Skip("pdf:budget")
    # /dev/fd/N re-opens the already verified inode in the child.
    cmd = [tool, "-q", "-enc", "UTF-8", "-nopgbrk", "-f", "1", "-l", str(PDF_MAX_PAGES),
           f"/dev/fd/{fd}", "-"]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    try:
        proc = subprocess.run(cmd, pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise _Skip("pdf:timeout")
    if proc.returncode != 0:
        raise _Skip("pdf:extract-failed")
    return proc.stdout[: DOC_MAX_CHARS * 4].decode("utf-8", errors="replace")


_DOCX_BREAK_RE = re.compile(r"</w:p>|<w:br\s*/>|<w:tab\s*/>")
_XML_TAG_RE = re.compile(r"<[^>]*>")


def _docx_text(fd: int) -> str:
    with os.fdopen(os.dup(fd), "rb") as fh, zipfile.ZipFile(fh) as zf:
        try:
            info = zf.getinfo("word/document.xml")
        except KeyError:
            raise _Skip("docx:no-body")
        if info.file_size > DOCX_MAX_XML_BYTES:
            raise _Skip("docx:oversize")
        with zf.open(info) as body:
            xml = body.read(DOCX_MAX_XML_BYTES + 1)[:DOCX_MAX_XML_BYTES].decode("utf-8", errors="replace")
    return html.unescape(_XML_TAG_RE.sub("", _DOCX_BREAK_RE.sub("\n", xml)))
