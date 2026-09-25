"""Tests for the document-converter skill (hermes/skills/document-converter).

Every fixture is synthesised at test time (tiny EPUB zips and hand-built PDFs),
so no book content is read or committed. PDF tests need poppler-utils and skip
without it; the encrypted-PDF test also needs Ghostscript.

Run: python3 -m unittest discover -s scripts/tests -p 'test_document_converter.py'
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "hermes" / "skills" / "document-converter" / "scripts" / "doc2md.py"

sys.dont_write_bytecode = True  # keep __pycache__ out of the installed skill dir
spec = importlib.util.spec_from_file_location("doc2md", SCRIPT)
doc2md = importlib.util.module_from_spec(spec)
sys.modules["doc2md"] = doc2md
spec.loader.exec_module(doc2md)

HAVE_POPPLER = bool(shutil.which("pdftotext") and shutil.which("pdfinfo"))
HAVE_PDFTOPPM = bool(shutil.which("pdftoppm"))
HAVE_GS = bool(shutil.which("gs"))

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Fixture Handbook</dc:title><dc:creator>Test Author</dc:creator>
  </metadata>
  <manifest>
    <item id="c1" href="{c1}" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/ch%202.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/><itemref idref="css"/></spine>
</package>"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>ignored</title><style>p{}</style></head>
<body><h1>Chapter One</h1>
<p>First <em>fixture</em> paragraph with <strong>bold</strong> &amp; an entity.</p>
<ul><li>alpha</li><li>beta</li></ul>
<ol><li>step one</li><li>step two</li></ol>
<blockquote><p>Quoted line.</p></blockquote>
<script>alert('x')</script>
</body></html>"""

CH2 = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2>Section Two</h2><p>Second   chapter
text.</p><pre>code  block</pre>
<table><tr><th>k</th><th>v</th></tr><tr><td>a</td><td>1</td></tr></table>
</body></html>"""


def make_epub(path: Path, *, c1: str = "text/ch1.xhtml", encryption: str | None = None,
              opf: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf if opf is not None else OPF.format(c1=c1))
        z.writestr("OEBPS/text/ch1.xhtml", CH1)
        z.writestr("OEBPS/text/ch 2.xhtml", CH2)
        z.writestr("OEBPS/style.css", "p{}")
        if encryption:
            z.writestr("META-INF/encryption.xml", encryption)
    return path


def encryption_xml(algorithm: str) -> str:
    return f"""<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
  xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData><enc:EncryptionMethod Algorithm="{algorithm}"/>
  <enc:CipherData><enc:CipherReference URI="OEBPS/text/ch1.xhtml"/></enc:CipherData></enc:EncryptedData>
</encryption>"""


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Write a minimal valid PDF; each page is a list of text lines (may be empty)."""
    objs: list[bytes] = []
    n = len(pages)
    font_id = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, lines in enumerate(pages):
        ops = ["BT /F1 12 Tf 14 TL 72 720 Td"]
        for line in lines:
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                    f"/Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


LONG_LINE = "The quick brown fox jumps over the lazy fixture dog again"


def run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = doc2md.main(argv + ["--json"])
    return code, json.loads(buf.getvalue())


def by_source(report: dict) -> dict:
    return {r["source"]: r for r in report["results"]}


def snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            snap[str(p)] = ("link", os.readlink(p))
        elif p.is_file():
            st = p.stat()
            snap[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_mode)
    return snap


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="doc2md-test-")
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "books"
        self.dst = self.tmp / "out"
        self.src.mkdir()

    def tearDown(self):
        self._tmp.cleanup()


class HtmlToMarkdownTest(unittest.TestCase):
    def test_structure(self):
        md = doc2md.html_to_markdown(CH1, heading_offset=1)
        self.assertIn("## Chapter One", md)
        self.assertIn("First *fixture* paragraph with **bold** & an entity.", md)
        self.assertIn("- alpha\n- beta", md)
        self.assertIn("1. step one\n2. step two", md)
        self.assertIn("> Quoted line.", md)
        self.assertNotIn("alert", md)
        self.assertNotIn("ignored", md)

    def test_pre_and_table(self):
        md = doc2md.html_to_markdown(CH2)
        self.assertIn("Second chapter text.", md)
        self.assertIn("```\ncode  block\n```", md)
        self.assertIn("k | v\na | 1", md)


class EpubTest(Base):
    def test_converts_in_spine_order_with_frontmatter(self):
        (self.src / "sub").mkdir()
        make_epub(self.src / "sub" / "book.epub")
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 0, rep)
        r = by_source(rep)["sub/book.epub"]
        self.assertEqual(r["status"], "converted")
        text = (self.dst / "sub" / "book.md").read_text()
        self.assertTrue(text.startswith('---\nsource: "sub/book.epub"\n'))
        self.assertIn('title: "Fixture Handbook"', text)
        self.assertIn('authors: ["Test Author"]', text)
        self.assertIn("# Fixture Handbook", text)
        self.assertLess(text.index("## Chapter One"), text.index("### Section Two"))
        self.assertNotIn("p{}", text)

    def test_drm_epub_reported_encrypted(self):
        make_epub(self.src / "drm.epub",
                  encryption=encryption_xml("http://www.w3.org/2001/04/xmlenc#aes128-cbc"))
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 3)
        self.assertEqual(by_source(rep)["drm.epub"]["status"], "encrypted")
        self.assertFalse((self.dst / "drm.md").exists())

    def test_font_obfuscation_is_not_drm(self):
        make_epub(self.src / "fonts.epub", encryption=encryption_xml("http://www.idpf.org/2008/embedding"))
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(by_source(rep)["fonts.epub"]["status"], "converted")

    def test_member_traversal_rejected(self):
        make_epub(self.src / "evil.epub", c1="../../../etc/passwd")
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        r = by_source(rep)["evil.epub"]
        self.assertEqual(r["status"], "error")
        self.assertIn("escapes archive root", r["message"])

    def test_entity_declarations_rejected(self):
        bomb = OPF.format(c1="text/ch1.xhtml").replace(
            '<?xml version="1.0" encoding="utf-8"?>',
            '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>')
        make_epub(self.src / "bomb.epub", opf=bomb)
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertIn("entity", by_source(rep)["bomb.epub"]["message"])

    def test_not_a_zip(self):
        (self.src / "broken.epub").write_bytes(b"not a zip")
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 3)
        self.assertEqual(by_source(rep)["broken.epub"]["status"], "error")


@unittest.skipUnless(HAVE_POPPLER, "poppler-utils (pdftotext/pdfinfo) not installed")
class PdfTest(Base):
    def test_text_pdf_reflowed_with_page_markers(self):
        make_pdf(self.src / "text.pdf", [[LONG_LINE, "continues here."], [LONG_LINE + " two"]])
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 0, rep)
        r = by_source(rep)["text.pdf"]
        self.assertEqual((r["status"], r["method"], r["pages"]), ("converted", "pdftotext", 2))
        text = (self.dst / "text.md").read_text()
        self.assertIn("<!-- page 1 -->", text)
        self.assertIn("<!-- page 2 -->", text)
        self.assertIn(LONG_LINE + " continues here.", text)
        self.assertIn("# text", text)  # no PDF Title -> file stem

    def test_scanned_without_ocr_reported(self):
        make_pdf(self.src / "scan.pdf", [[], []])
        with mock.patch.object(doc2md, "tool", side_effect=lambda n: None if n == "tesseract" else shutil.which(n)):
            code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 3)
        r = by_source(rep)["scan.pdf"]
        self.assertEqual(r["status"], "scanned")
        self.assertIn("tesseract", r["message"])
        self.assertFalse((self.dst / "scan.md").exists())

    def test_ocr_never(self):
        make_pdf(self.src / "scan.pdf", [[]])
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst), "--ocr", "never"])
        self.assertEqual(by_source(rep)["scan.pdf"]["status"], "scanned")

    def test_mixed_pdf_is_partial_without_ocr(self):
        make_pdf(self.src / "mixed.pdf", [[LONG_LINE], []])
        with mock.patch.object(doc2md, "tool", side_effect=lambda n: None if n == "tesseract" else shutil.which(n)):
            code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 0)
        r = by_source(rep)["mixed.pdf"]
        self.assertEqual(r["status"], "partial")
        self.assertIn("1 page(s) have no text layer", r["warnings"][0])

    @unittest.skipUnless(HAVE_PDFTOPPM, "pdftoppm not installed")
    def test_ocr_fallback_uses_tesseract(self):
        """A stub `tesseract` on PATH proves the render+OCR wiring for empty pages only."""
        bindir = self.tmp / "bin"
        bindir.mkdir()
        stub = bindir / "tesseract"
        stub.write_text('#!/bin/sh\ntest -f "$1" || exit 1\necho "OCR stub text for $(basename "$1")"\n')
        stub.chmod(0o755)
        make_pdf(self.src / "mixed.pdf", [[LONG_LINE], []])
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
            _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        r = by_source(rep)["mixed.pdf"]
        self.assertEqual((r["status"], r["method"], r["ocr_pages"]), ("converted", "pdftotext+ocr", 1), r)
        text = (self.dst / "mixed.md").read_text()
        self.assertIn("OCR stub text for p2.png", text)
        self.assertIn(LONG_LINE, text)

    @unittest.skipUnless(HAVE_GS, "ghostscript not installed")
    def test_encrypted_pdfs_reported(self):
        ps = "/Helvetica findfont 12 scalefont setfont 72 700 moveto (secret) show showpage"
        base = ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-dEncryptionR=3", "-dKeyLength=128"]
        subprocess.run(base + ["-sOwnerPassword=o", "-sUserPassword=u",
                               f"-sOutputFile={self.src / 'userpw.pdf'}", "-c", ps], check=True)
        subprocess.run(base + ["-sOwnerPassword=o", "-dPermissions=-3904",
                               f"-sOutputFile={self.src / 'nocopy.pdf'}", "-c", ps], check=True)
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 3)
        res = by_source(rep)
        self.assertEqual(res["userpw.pdf"]["status"], "encrypted")
        self.assertIn("password", res["userpw.pdf"]["message"])
        self.assertEqual(res["nocopy.pdf"]["status"], "encrypted")
        self.assertIn("copying disallowed", res["nocopy.pdf"]["message"])
        self.assertFalse(self.dst.exists() and any(self.dst.rglob("*.md")))

    def test_reflow_dehyphenates(self):
        self.assertEqual(doc2md.reflow_page("exam-\nple text\n\nnext"), "example text\n\nnext")

    def test_copy_restricted_parse(self):
        info = doc2md.parse_pdfinfo("Pages: 3\nEncrypted:       yes (print:yes copy:no change:no)\n")
        self.assertTrue(doc2md.pdf_copy_restricted(info))
        self.assertFalse(doc2md.pdf_copy_restricted({"Encrypted": "yes (print:yes copy:yes)"}))
        self.assertFalse(doc2md.pdf_copy_restricted({"Encrypted": "no"}))


class SafetyTest(Base):
    def test_overlapping_roots_refused(self):
        make_epub(self.src / "a.epub")
        for dest in (self.src / "converted", self.tmp):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(doc2md.main(["--source", str(self.src), "--dest", str(dest)]), 2)
        self.assertFalse((self.src / "converted").exists())

    def test_traversal_args_refused(self):
        outside = self.tmp / "outside.epub"
        make_epub(outside)
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst), "../outside.epub", str(outside)])
        self.assertEqual(code, 3)
        msgs = [r["message"] for r in rep["results"]]
        self.assertIn("refusing path with '..'", msgs[0])
        self.assertIn("outside --source", msgs[1])
        self.assertFalse(self.dst.exists())

    def test_symlinks_not_followed(self):
        outside = self.tmp / "secret"
        outside.mkdir()
        make_epub(outside / "private.epub")
        (self.src / "link.epub").symlink_to(outside / "private.epub")
        (self.src / "linkdir").symlink_to(outside, target_is_directory=True)
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        res = by_source(rep)
        self.assertEqual(res["link.epub"]["status"], "symlink")
        self.assertEqual(res["linkdir"]["status"], "symlink")
        self.assertNotIn("linkdir/private.epub", res)
        self.assertFalse(self.dst.exists() and any(self.dst.rglob("*.md")))

    def test_dest_symlink_escape_refused(self):
        (self.src / "sub").mkdir()
        make_epub(self.src / "sub" / "b.epub")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.dst.mkdir()
        (self.dst / "sub").symlink_to(elsewhere, target_is_directory=True)
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(by_source(rep)["sub/b.epub"]["status"], "error")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_sources_never_modified_and_rerun_is_idempotent(self):
        make_epub(self.src / "a.epub")
        (self.src / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        (self.src / "notes.opf").write_text("<x/>")
        if HAVE_POPPLER:
            make_pdf(self.src / "t.pdf", [[LONG_LINE]])
        before = snapshot(self.src)
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 0, rep)
        res = by_source(rep)
        self.assertEqual(res["cover.jpg"]["status"], "unsupported")
        self.assertEqual(res["notes.opf"]["status"], "unsupported")
        self.assertEqual(snapshot(self.src), before)
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(by_source(rep)["a.epub"]["status"], "up-to-date")
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst), "--force"])
        self.assertEqual(by_source(rep)["a.epub"]["status"], "converted")
        self.assertEqual(snapshot(self.src), before)
        self.assertEqual([p.name for p in self.dst.iterdir() if p.name.startswith(".doc2md-")], [])

    def test_same_stem_twins_do_not_collide(self):
        make_epub(self.src / "twin.epub")
        make_epub(self.src / "twin.pdf")  # content irrelevant; only the name matters
        with mock.patch.object(doc2md, "convert_pdf", return_value=("pdf body", {"method": "pdftotext"})):
            _, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        res = by_source(rep)
        self.assertEqual(res["twin.epub"]["output"], str(self.dst.resolve() / "twin.epub.md"))
        self.assertEqual(res["twin.pdf"]["output"], str(self.dst.resolve() / "twin.pdf.md"))
        self.assertTrue((self.dst / "twin.epub.md").exists() and (self.dst / "twin.pdf.md").exists())

    def test_foreign_dest_file_not_overwritten(self):
        make_epub(self.src / "a.epub")
        self.dst.mkdir()
        (self.dst / "a.md").write_text("my own notes\n")
        code, rep = run(["--source", str(self.src), "--dest", str(self.dst)])
        self.assertEqual(code, 3)
        self.assertIn("not written by doc2md", by_source(rep)["a.epub"]["message"])
        self.assertEqual((self.dst / "a.md").read_text(), "my own notes\n")

    def test_dry_run_writes_nothing(self):
        make_epub(self.src / "a.epub")
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst), "--dry-run"])
        self.assertEqual(by_source(rep)["a.epub"]["status"], "would-convert")
        self.assertFalse(self.dst.exists())

    def test_size_cap(self):
        make_epub(self.src / "a.epub")
        _, rep = run(["--source", str(self.src), "--dest", str(self.dst), "--max-bytes", "10"])
        self.assertEqual(by_source(rep)["a.epub"]["status"], "too-large")

    def test_cli_entrypoint_text_report(self):
        make_epub(self.src / "a.epub")
        proc = subprocess.run([sys.executable, str(SCRIPT), "--source", str(self.src), "--dest", str(self.dst)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("converted     a.epub", proc.stdout)
        self.assertIn("summary: converted=1", proc.stdout)


if __name__ == "__main__":
    unittest.main()
