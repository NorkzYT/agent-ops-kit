#!/usr/bin/env python3
"""doc2md.py — copy-only EPUB/PDF to Markdown converter for Hermes knowledge.

Reads books from a source root, writes one Markdown file per book under a
separate destination root, and never writes to, moves or deletes a source file.

    doc2md.py --source /mnt/books/edu --dest ~/knowledge-md/edu
    doc2md.py --source /mnt/books/edu --dest ~/knowledge-md/edu "Meditations - Marcus Aurelius"
    doc2md.py --source /mnt/books/edu --dest /tmp/out --dry-run --json

Supported:
  .epub  stdlib only (zipfile + XML/HTML parsers), spine order, headings kept
  .pdf   poppler `pdftotext` for text-layer PDFs; pages with no text layer are
         OCRed with `pdftoppm` + `tesseract` when both are installed

Every file gets one status: converted, partial, up-to-date, would-convert,
unsupported, encrypted, scanned, symlink, too-large, error. Exit code is 0 when
nothing failed, 3 when any file was encrypted/scanned/error, 2 on usage errors.

Safety:
  * source and destination must not overlap (neither inside the other)
  * explicit paths must resolve inside --source (no `..`, no absolute escape)
  * symlinks in the source are never followed; only regular files are read
  * EPUB members are read in memory, never extracted; member paths that escape
    the archive root and DTD entity declarations are rejected; size is capped
  * outputs are written to a temp file in the destination and renamed; a
    destination path that is a symlink or resolves outside --dest is refused
  * DRM/encrypted EPUBs and password or copy-restricted PDFs are reported,
    not bypassed

Python 3.9+ stdlib only. External tools are optional and detected at runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote
from xml.etree import ElementTree as ET

VERSION = "1.0.0"
SUPPORTED = {".epub", ".pdf"}
FAILED_STATUSES = {"encrypted", "scanned", "error"}

MAX_FILE_BYTES = 1024 * 1024 * 1024        # 1 GiB per source file
MAX_EPUB_MEMBER_BYTES = 64 * 1024 * 1024   # 64 MiB per XHTML/OPF member
MAX_EPUB_TOTAL_BYTES = 512 * 1024 * 1024   # 512 MiB of text read per EPUB
PDF_TIMEOUT = 600                          # seconds per poppler call
OCR_PAGE_TIMEOUT = 180                     # seconds per page (render + OCR)
MIN_PAGE_CHARS = 25                        # below this a page has "no text layer"

# Font obfuscation is not DRM: the text is readable and EPUB readers ignore it.
FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
}


class ConvertError(Exception):
    """A per-file failure with a status and a human-readable reason."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Result:
    source: str
    status: str
    output: Optional[str] = None
    method: Optional[str] = None
    message: str = ""
    pages: Optional[int] = None
    ocr_pages: Optional[int] = None
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------- paths ---

def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_roots(source: Path, dest: Path) -> tuple[Path, Path]:
    if not source.is_dir():
        raise SystemExit(f"doc2md: source is not a directory: {source}")
    src = source.resolve(strict=True)
    dst = dest.resolve(strict=False)
    if is_within(dst, src) or is_within(src, dst):
        raise SystemExit(
            f"doc2md: source ({src}) and destination ({dst}) must not overlap; "
            "write outputs to a separate folder"
        )
    return src, dst


def resolve_target(src_root: Path, arg: str) -> Path:
    """Map a CLI path argument to a path inside the source root, or refuse."""
    raw = Path(arg)
    if ".." in raw.parts:
        raise ConvertError("error", f"refusing path with '..': {arg}")
    candidate = raw if raw.is_absolute() else src_root / raw
    # Resolve the parent (not the leaf) so a symlinked leaf is still detected
    # as a symlink later instead of being silently followed.
    parent = candidate.parent.resolve(strict=False)
    if not is_within(parent / candidate.name, src_root):
        raise ConvertError("error", f"path is outside --source: {arg}")
    return parent / candidate.name


def iter_files(root: Path) -> Iterable[Path]:
    """Yield every non-directory entry under root without following symlinks."""
    if root.is_symlink() or root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name
        # os.walk lists symlinked dirs in dirnames but will not descend into
        # them; surface them so the report shows they were skipped.
        for name in dirnames:
            p = Path(dirpath) / name
            if p.is_symlink():
                yield p


def output_path(src_root: Path, dst_root: Path, file: Path) -> Path:
    rel = file.relative_to(src_root)
    # "book.epub" -> "book.md", unless a sibling "book.pdf" would collide; then
    # every twin keeps its extension ("book.epub.md", "book.pdf.md").
    twins = [file.with_suffix(ext) for ext in SUPPORTED if ext != file.suffix.lower()]
    if any(t.is_file() for t in twins):
        out = dst_root / rel.parent / (rel.name + ".md")
    else:
        out = (dst_root / rel).with_suffix(".md")
    if not is_within(out.parent.resolve(strict=False), dst_root):
        raise ConvertError("error", f"output would escape --dest: {out}")
    return out


def write_atomic(out: Path, dst_root: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after mkdir: a pre-existing symlinked directory in dest must not
    # redirect the write elsewhere.
    if not is_within(out.parent.resolve(strict=True), dst_root):
        raise ConvertError("error", f"output directory resolves outside --dest: {out.parent}")
    if out.is_symlink():
        raise ConvertError("error", f"refusing to overwrite symlink: {out}")
    fd, tmp = tempfile.mkstemp(prefix=".doc2md-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------- HTML -> MD ---

class MarkdownBuilder(HTMLParser):
    """Small XHTML-to-Markdown converter for book chapters (stdlib only)."""

    BLOCK = frozenset({"p", "div", "section", "article", "header", "footer", "aside",
                       "figure", "figcaption", "dl", "dt", "dd", "table", "tr", "hr",
                       "body", "main", "nav"})
    SKIP = frozenset({"script", "style", "head", "title", "svg", "math", "template"})
    HEADINGS = {f"h{n}": n for n in range(1, 7)}

    def __init__(self, heading_offset: int = 0):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.line: list[str] = []
        self.skip_depth = 0
        self.pre_depth = 0
        self.lists: list[list] = []  # [kind, counter]
        self.quote_depth = 0
        self.heading_offset = heading_offset
        self.cell_count = 0

    # -- helpers
    def _prefix(self) -> str:
        return "> " * self.quote_depth

    def _flush(self, blank: bool = True) -> None:
        text = "".join(self.line)
        self.line = []
        if self.pre_depth:
            self.out.append(text)
            return
        text = re.sub(r"[ \t\r\n ]+", " ", text).strip()
        if text:
            self.out.append(self._prefix() + text)
        if blank and self.out and self.out[-1] != "":
            self.out.append("")

    def _emit(self, s: str) -> None:
        self.line.append(s)

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        a = dict(attrs)
        if tag in self.HEADINGS:
            self._flush()
            level = min(6, self.HEADINGS[tag] + self.heading_offset)
            self._emit("#" * level + " ")
        elif tag == "pre":
            self._flush()
            self.out.append("```")
            self.pre_depth += 1
        elif tag == "blockquote":
            self._flush()
            self.quote_depth += 1
        elif tag in ("ul", "ol"):
            self._flush(blank=not self.lists)
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush(blank=False)
            indent = "  " * max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self._emit(f"{indent}{self.lists[-1][1]}. ")
            else:
                self._emit(f"{indent}- ")
        elif tag == "br":
            if self.pre_depth:
                self._emit("\n")
            else:
                self._flush(blank=False)
        elif tag in ("td", "th"):
            if self.cell_count:
                self._emit(" | ")
            self.cell_count += 1
        elif tag == "tr":
            self._flush(blank=False)
            self.cell_count = 0
        elif tag == "hr":
            self._flush()
            self.out.extend(["---", ""])
        elif tag in ("em", "i", "cite"):
            self._emit("*")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag == "code" and not self.pre_depth:
            self._emit("`")
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt:
                self._emit(f"[image: {alt}]")
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in self.HEADINGS:
            self._flush()
        elif tag == "pre":
            self._flush(blank=False)
            self.pre_depth = max(0, self.pre_depth - 1)
            self.out.extend(["```", ""])
        elif tag == "blockquote":
            self._flush()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag in ("ul", "ol"):
            self._flush(blank=False)
            if self.lists:
                self.lists.pop()
            if not self.lists and self.out and self.out[-1] != "":
                self.out.append("")
        elif tag == "li":
            self._flush(blank=False)
        elif tag == "tr":
            self._flush(blank=False)
        elif tag == "table":
            self._flush()
        elif tag in ("em", "i", "cite"):
            self._emit("*")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag == "code" and not self.pre_depth:
            self._emit("`")
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self.skip_depth:
            self._emit(data)

    def markdown(self) -> str:
        self._flush()
        text = "\n".join(self.out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n" if text.strip() else ""


def html_to_markdown(markup: str, heading_offset: int = 0) -> str:
    parser = MarkdownBuilder(heading_offset=heading_offset)
    parser.feed(markup)
    parser.close()
    return parser.markdown()


# ---------------------------------------------------------------- EPUB ---

def _safe_xml(data: bytes, what: str) -> ET.Element:
    if b"<!ENTITY" in data:
        raise ConvertError("error", f"{what}: DTD entity declarations are not allowed")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConvertError("error", f"{what}: invalid XML ({exc})") from None


def _member_path(base_dir: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    if not href or href.startswith("/") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href):
        raise ConvertError("error", f"EPUB member href not allowed: {href!r}")
    path = posixpath.normpath(posixpath.join(base_dir, href))
    if path.startswith("../") or path == ".." or path.startswith("/"):
        raise ConvertError("error", f"EPUB member escapes archive root: {href!r}")
    return path


def _read_member(zf: zipfile.ZipFile, name: str, budget: list) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise ConvertError("error", f"EPUB is missing member: {name}") from None
    if info.flag_bits & 0x1:
        raise ConvertError("encrypted", "EPUB zip entries are password-protected")
    if info.file_size > MAX_EPUB_MEMBER_BYTES:
        raise ConvertError("too-large", f"EPUB member {name} is {info.file_size} bytes")
    budget[0] -= info.file_size
    if budget[0] < 0:
        raise ConvertError("too-large", "EPUB text exceeds the per-book size cap")
    with zf.open(info) as fh:
        data = fh.read(MAX_EPUB_MEMBER_BYTES + 1)
    if len(data) > MAX_EPUB_MEMBER_BYTES:
        raise ConvertError("too-large", f"EPUB member {name} exceeds size cap")
    return data


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def epub_drm(zf: zipfile.ZipFile, budget: list) -> Optional[str]:
    names = set(zf.namelist())
    if "META-INF/rights.xml" in names:
        return "EPUB carries a DRM rights file (META-INF/rights.xml)"
    if "META-INF/encryption.xml" not in names:
        return None
    root = _safe_xml(_read_member(zf, "META-INF/encryption.xml", budget), "encryption.xml")
    for enc in root.iter(f"{{{NS['enc']}}}EncryptedData"):
        method = enc.find("enc:EncryptionMethod", NS)
        algo = method.get("Algorithm", "") if method is not None else ""
        if algo not in FONT_OBFUSCATION:
            return f"EPUB content is encrypted (DRM algorithm {algo or 'unknown'})"
    return None


def convert_epub(path: Path) -> tuple[str, dict]:
    budget = [MAX_EPUB_TOTAL_BYTES]
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ConvertError("error", "not a valid EPUB (zip) file") from None
    with zf:
        drm = epub_drm(zf, budget)
        if drm:
            raise ConvertError("encrypted", drm)
        container = _safe_xml(_read_member(zf, "META-INF/container.xml", budget), "container.xml")
        rootfile = container.find(".//c:rootfile", NS)
        if rootfile is None or not rootfile.get("full-path"):
            raise ConvertError("error", "container.xml has no rootfile")
        opf_path = _member_path("", rootfile.get("full-path"))
        opf = _safe_xml(_read_member(zf, opf_path, budget), opf_path)
        opf_dir = posixpath.dirname(opf_path)

        title = (opf.findtext(".//dc:title", default="", namespaces=NS) or "").strip()
        authors = [a.text.strip() for a in opf.findall(".//dc:creator", NS) if a.text and a.text.strip()]

        manifest = {}
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            manifest[item.get("id")] = (item.get("href", ""), item.get("media-type", ""))
        spine = [ref.get("idref") for ref in opf.findall(".//opf:spine/opf:itemref", NS)]
        if not spine:
            raise ConvertError("error", "EPUB has an empty spine (no reading order)")

        parts, seen = [], set()
        for idref in spine:
            href, media = manifest.get(idref, ("", ""))
            if not href or media not in ("application/xhtml+xml", "text/html", "application/x-dtbook+xml"):
                continue
            member = _member_path(opf_dir, href)
            if member in seen:
                continue
            seen.add(member)
            md = html_to_markdown(_decode(_read_member(zf, member, budget)), heading_offset=1)
            if md.strip():
                parts.append(md)

    if not parts:
        raise ConvertError("error", "EPUB spine contains no readable text")
    return "\n".join(parts), {"title": title, "authors": authors, "method": "epub"}


# ----------------------------------------------------------------- PDF ---

def _run(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ConvertError("error", f"{cmd[0]} timed out after {timeout}s") from None


def pdf_info(path: Path) -> dict:
    proc = _run(["pdfinfo", "-enc", "UTF-8", "--", str(path)], PDF_TIMEOUT)
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        if "password" in err.lower():
            raise ConvertError("encrypted", "PDF needs a password to open")
        raise ConvertError("error", f"pdfinfo failed: {err.strip()[:200]}")
    return parse_pdfinfo(proc.stdout.decode("utf-8", "replace"))


def parse_pdfinfo(text: str) -> dict:
    info = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip()] = value.strip()
    return info


def pdf_copy_restricted(info: dict) -> bool:
    enc = info.get("Encrypted", "no")
    return enc.startswith("yes") and bool(re.search(r"\bcopy:\s*no\b", enc))


def reflow_page(text: str) -> str:
    """Join hard-wrapped lines into paragraphs; keep blank-line breaks."""
    paras, cur = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if cur:
                paras.append(cur)
                cur = []
            continue
        cur.append(line)
    if cur:
        paras.append(cur)
    out = []
    for lines in paras:
        buf = lines[0]
        for nxt in lines[1:]:
            if re.search(r"[a-z]-$", buf) and re.match(r"^[a-z]", nxt):
                buf = buf[:-1] + nxt  # de-hyphenate "exam-\nple"
            else:
                buf += " " + nxt
        out.append(buf)
    return "\n\n".join(out)


def tool(name: str) -> Optional[str]:
    return shutil.which(name)


def ocr_page(path: Path, page: int, workdir: str, lang: str) -> str:
    base = os.path.join(workdir, f"p{page}")
    r = _run(["pdftoppm", "-r", "300", "-gray", "-png", "-singlefile",
              "-f", str(page), "-l", str(page), "--", str(path), base], OCR_PAGE_TIMEOUT)
    if r.returncode != 0:
        raise ConvertError("error", f"pdftoppm failed on page {page}")
    img = base + ".png"
    r = _run(["tesseract", img, "stdout", "-l", lang], OCR_PAGE_TIMEOUT)
    try:
        os.unlink(img)
    except FileNotFoundError:
        pass
    if r.returncode != 0:
        raise ConvertError("error", f"tesseract failed on page {page}: "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.stdout.decode("utf-8", "replace")


def convert_pdf(path: Path, ocr: str, lang: str, max_ocr_pages: int) -> tuple[str, dict]:
    if not tool("pdftotext") or not tool("pdfinfo"):
        raise ConvertError("error", "poppler-utils (pdftotext, pdfinfo) is not installed")
    info = pdf_info(path)
    if pdf_copy_restricted(info):
        raise ConvertError("encrypted", "PDF is encrypted with text copying disallowed; not bypassing")
    proc = _run(["pdftotext", "-enc", "UTF-8", "--", str(path), "-"], PDF_TIMEOUT)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        if "password" in err.lower():
            raise ConvertError("encrypted", "PDF needs a password to open")
        raise ConvertError("error", f"pdftotext failed: {err[:200]}")
    pages = proc.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()  # pdftotext ends with a form feed
    try:
        n_pages = int(info.get("Pages", len(pages)))
    except ValueError:
        n_pages = len(pages)
    pages += [""] * max(0, n_pages - len(pages))

    empty = [i for i, p in enumerate(pages) if len(re.sub(r"\s", "", p)) < MIN_PAGE_CHARS]
    ocr_ok = bool(tool("tesseract") and tool("pdftoppm"))
    warnings, ocr_done = [], 0
    method = "pdftotext"

    if empty and ocr != "never":
        if not ocr_ok:
            if len(empty) == len(pages):
                raise ConvertError(
                    "scanned", f"no text layer on any of {len(pages)} page(s); OCR unavailable "
                    "(install tesseract-ocr to enable it)")
            warnings.append(f"{len(empty)} page(s) have no text layer; OCR unavailable")
        elif len(empty) > max_ocr_pages:
            raise ConvertError(
                "scanned", f"{len(empty)} page(s) need OCR, over --max-ocr-pages {max_ocr_pages}")
        else:
            with tempfile.TemporaryDirectory(prefix="doc2md-ocr-") as work:
                for i in empty:
                    pages[i] = ocr_page(path, i + 1, work, lang)
                    ocr_done += 1
            method = "ocr" if len(empty) == len(pages) else "pdftotext+ocr"
    elif empty and len(empty) == len(pages):
        raise ConvertError("scanned", f"no text layer on any of {len(pages)} page(s); OCR disabled")
    elif empty:
        warnings.append(f"{len(empty)} page(s) have no text layer; OCR disabled")

    body = []
    for i, page in enumerate(pages, 1):
        text = reflow_page(page)
        body.append(f"<!-- page {i} -->\n\n{text}\n" if text else f"<!-- page {i} -->\n")
    meta = {"title": info.get("Title", ""), "authors": [info["Author"]] if info.get("Author") else [],
            "method": method, "pages": len(pages), "ocr_pages": ocr_done, "warnings": warnings}
    return "\n".join(body), meta


# -------------------------------------------------------------- driver ---

def frontmatter(rel: str, meta: dict) -> str:
    def q(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)
    lines = ["---", f"source: {q(rel)}", f"converter: {q('doc2md ' + VERSION)}",
             f"method: {q(meta['method'])}"]
    if meta.get("title"):
        lines.append(f"title: {q(meta['title'])}")
    if meta.get("authors"):
        lines.append("authors: [" + ", ".join(q(a) for a in meta["authors"]) + "]")
    lines.append("---\n")
    return "\n".join(lines)


def written_by_us(out: Path) -> bool:
    try:
        with open(out, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return b'\nconverter: "doc2md ' in head


def convert_one(file: Path, src_root: Path, dst_root: Path, args) -> Result:
    rel = file.relative_to(src_root).as_posix()
    res = Result(source=rel, status="error")
    try:
        if file.is_symlink():
            return Result(rel, "symlink", message="symlinks are not followed")
        if not file.is_file():
            return Result(rel, "unsupported", message="not a regular file")
        ext = file.suffix.lower()
        if ext not in SUPPORTED:
            return Result(rel, "unsupported", message=f"'{ext or 'no extension'}' is not EPUB or PDF")
        size = file.stat().st_size
        if size > args.max_bytes:
            return Result(rel, "too-large", message=f"{size} bytes > --max-bytes {args.max_bytes}")
        out = output_path(src_root, dst_root, file)
        res.output = str(out)
        if out.exists() and not args.force:
            if not written_by_us(out):
                res.message = "destination file exists and was not written by doc2md (use --force)"
                return res
            if out.stat().st_mtime >= file.stat().st_mtime:
                res.status, res.message = "up-to-date", "output is newer than source (use --force)"
                return res
        if args.dry_run:
            res.status = "would-convert"
            return res
        if ext == ".epub":
            body, meta = convert_epub(file)
        else:
            body, meta = convert_pdf(file, args.ocr, args.ocr_lang, args.max_ocr_pages)
        if not meta.get("title"):
            meta["title"] = file.stem
        text = frontmatter(rel, meta) + f"\n# {meta['title']}\n\n" + body.strip() + "\n"
        write_atomic(out, dst_root, text)
        res.method = meta["method"]
        res.pages = meta.get("pages")
        res.ocr_pages = meta.get("ocr_pages")
        res.warnings = meta.get("warnings", [])
        res.status = "partial" if res.warnings else "converted"
        return res
    except ConvertError as exc:
        res.status, res.message = exc.status, exc.message
        return res
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        res.status, res.message = "error", f"{type(exc).__name__}: {exc}"
        return res


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doc2md",
        description="Copy-only EPUB/PDF to Markdown converter. Sources are never modified.")
    p.add_argument("--source", required=True, type=Path, help="source root (read-only), e.g. /mnt/books/edu")
    p.add_argument("--dest", required=True, type=Path, help="output root, must not overlap --source")
    p.add_argument("paths", nargs="*", help="files or folders relative to --source (default: all)")
    p.add_argument("--ocr", choices=("auto", "never"), default="auto",
                   help="OCR pages without a text layer when tesseract is installed (default auto)")
    p.add_argument("--ocr-lang", default="eng", help="tesseract language (default eng)")
    p.add_argument("--max-ocr-pages", type=int, default=400, help="refuse OCR above this many pages")
    p.add_argument("--max-bytes", type=int, default=MAX_FILE_BYTES, help="skip source files larger than this")
    p.add_argument("--force", action="store_true", help="re-convert even if the output is newer")
    p.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    p.add_argument("--json", action="store_true", help="print a JSON report instead of a table")
    p.add_argument("--version", action="version", version=f"doc2md {VERSION}")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_+]+", args.ocr_lang):
        print(f"doc2md: invalid --ocr-lang {args.ocr_lang!r}", file=sys.stderr)
        return 2
    try:
        src_root, dst_root = check_roots(args.source, args.dest)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    results: list[Result] = []
    targets = args.paths or ["."]
    for arg in targets:
        try:
            target = src_root if arg == "." else resolve_target(src_root, arg)
        except ConvertError as exc:
            results.append(Result(arg, exc.status, message=exc.message))
            continue
        if not target.exists() and not target.is_symlink():
            results.append(Result(arg, "error", message="no such file or folder under --source"))
            continue
        for f in iter_files(target):
            results.append(convert_one(f, src_root, dst_root, args))

    counts: dict = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    if args.json:
        json.dump({"source": str(src_root), "dest": str(dst_root), "counts": counts,
                   "results": [asdict(r) for r in results]}, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        for r in results:
            detail = r.message or (", ".join(r.warnings) if r.warnings else r.method or "")
            print(f"{r.status:<13} {r.source}" + (f"  — {detail}" if detail else ""))
        print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 3 if any(r.status in FAILED_STATUSES for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
