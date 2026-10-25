---
name: document-converter
description: "Copy-only EPUB/PDF to Markdown for profile knowledge."
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [knowledge, epub, pdf, markdown, ocr, profiles]
    category: productivity
    requires_toolsets: [terminal]
---

# Document converter

Turn EPUB and PDF books into Markdown that `make hermes-profile` can sync into a
profile's `knowledge/` folder. The converter only reads the source library and
writes new `.md` files into a separate destination. It never moves, edits or
deletes a book.

## When to use

- A profile needs knowledge from a library such as `/mnt/books/edu`, and the
  books are EPUB or PDF. Knowledge sync only copies `.md` and `.txt`.
- The operator asked for specific books or folders. Do not convert a whole
  library unless the operator asks for that.

## Prerequisites

- Python 3.9 or newer. EPUB conversion uses only the standard library.
- PDF conversion needs poppler-utils (`pdftotext`, `pdfinfo`).
- OCR of scanned pages needs `pdftoppm` and `tesseract`. Without tesseract,
  scanned PDFs are reported as `scanned` and nothing is written for them.

Script path: `~/.hermes/skills/agent-ops-kit/document-converter/scripts/doc2md.py`
(repo: `hermes/skills/document-converter/scripts/doc2md.py`).

## Workflow

1. Dry run the selected books first, from the agent-ops-kit checkout. A dry
   run reads directory entries only:
   `terminal(command="python3 ~/.hermes/skills/agent-ops-kit/document-converter/scripts/doc2md.py --source /mnt/books/edu --dest data/knowledge/strategy --dry-run 'The Art of War - Sun Tzu'", timeout=60)`
   The same thing through Make is
   `make knowledge-convert SRC=/mnt/books/edu DEST=data/knowledge/strategy ARGS="--dry-run 'The Art of War - Sun Tzu'"`.
2. Show the operator the planned books and destination, and get approval.
3. Convert. Use the same command without `--dry-run`. Add `--json` for a
   machine-readable report. Allow about 10 minutes per large PDF and much
   longer when OCR runs.
4. Report each status. Everything other than `converted` and `up-to-date`
   needs a sentence in the report (see the table below).
5. Sync the output into the profile with `make hermes-profile NAME=<profile>`,
   then run `/learn ~/.hermes/profiles/<profile>/knowledge` in that profile.

Arguments after the options are paths relative to `--source`, either book
folders or files. With no paths, the whole source tree is converted.

| Status | Meaning | What to tell the operator |
|--------|---------|---------------------------|
| `converted` | Markdown written | Output path |
| `partial` | Written, but blank PDF pages between text pages had no text and OCR was unavailable (short title/divider pages and leading/trailing blank pages do not count) | Page count missing; install tesseract and rerun with `--force` |
| `up-to-date` | Output is newer than the source | Nothing to do (`--force` rebuilds) |
| `would-convert` | Dry run only | — |
| `unsupported` | Not an EPUB or PDF (covers, `.opf`, `Thumbs.db`) | Ignored |
| `encrypted` | DRM EPUB, password PDF, or PDF with copying disallowed | Not converted, and not bypassed |
| `scanned` | No text layer and OCR is unavailable or disabled | Install tesseract, or skip the book |
| `symlink` | Symlink in the source, not followed | Ignored |
| `too-large` | Over `--max-bytes` or the EPUB size caps | Ignored |
| `error` | Unreadable file, unsafe path, or tool failure | Quote the message |

Exit code 0 means nothing failed. 3 means at least one file was `encrypted`,
`scanned` or `error`. 2 means a usage error, such as overlapping roots.

## Output format

One `.md` per book, mirroring the source folders (`<dest>/<book folder>/<file>.md`).
If an EPUB and a PDF share a name, both keep their extension (`book.epub.md`,
`book.pdf.md`). An existing `.md` that doc2md did not write is never
overwritten without `--force`.
Each file starts with YAML front matter (`source`, `converter`, `method`,
`title`, `authors`) and a `# Title` heading. EPUB chapters keep their
headings, one level down. PDF text is reflowed into paragraphs with
`<!-- page N -->` markers.

## Pitfalls

- `--dest` must not be inside `--source`, and `--source` must not be inside
  `--dest`. Never write into `/mnt/books`.
- Converted books are copyrighted text. Keep them under the gitignored `data/`
  tree or `~/.hermes`. Never commit them, paste them into a chat, or publish them.
- A folder of converted books is not permission to use them for anything else.
  The profile's SOUL.md rules still apply.
- PDF text order follows `pdftotext`. Multi-column layouts, footnotes and
  tables can interleave. Spot-check one chapter before running `/learn`.
- EPUB images and links are dropped. Image alt text is kept as `[image: …]`.
- `--ocr-lang` must be a tesseract language that is installed (`eng`, `eng+deu`).

## Verification

- `python3 -m unittest discover -s scripts/tests -p 'test_document_converter.py'`
  from the repo. It uses synthetic fixtures only.
- After a real conversion, run `search_files` on the destination for `*.md`,
  then `read_file` the first 40 lines of one output. Check that the front
  matter `source` matches and the text is readable.
