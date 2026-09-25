# Converting books to profile knowledge

Profile knowledge sync copies only `.md` and `.txt` files. The
`document-converter` skill converts EPUB and PDF books into Markdown so a
profile can learn from them. It is copy-only. It reads the library, writes new
files elsewhere, and never changes a book.

## Fast path

Convert selected books from `/mnt/books/edu` straight into a profile's
knowledge folder, then sync and learn:

```bash
make knowledge-convert SRC=/mnt/books/edu DEST=data/knowledge/strategy \
  ARGS="--dry-run 'The Art of War - Sun Tzu' 'Zero to One - Notes on Startups, or How to Build the Future'"
# check the plan, then run the same command without --dry-run
make hermes-profile NAME=strategy
```

Then, in the `strategy` profile's chat:

```
/learn ~/.hermes/profiles/strategy/knowledge
```

`ARGS` takes book folders or files relative to `SRC`. Leave out the paths to
convert the whole tree. `data/` is gitignored, so converted text never reaches
Git.

The script can also be run directly:

```bash
python3 hermes/skills/document-converter/scripts/doc2md.py \
  --source /mnt/books/edu --dest data/knowledge/strategy --json "The Art of War - Sun Tzu"
```

| Option | Default | Purpose |
|--------|---------|---------|
| `--dry-run` | off | List what would convert. Reads no book content and writes nothing |
| `--force` | off | Rebuild outputs that are newer than their source |
| `--ocr auto\|never` | `auto` | OCR pages that have no text layer, when tesseract is installed |
| `--ocr-lang` | `eng` | tesseract language, such as `eng+deu` |
| `--max-ocr-pages` | `400` | Report a book as `scanned` instead of running OCR on more pages than this |
| `--max-bytes` | 1 GiB | Skip source files larger than this |
| `--json` | off | Print a JSON report instead of a table |

## What it handles

| Input | How | Result |
|-------|-----|--------|
| EPUB | Python stdlib: the zip is read in memory, and chapters follow the OPF spine order | Headings, lists, quotes, emphasis, code and tables become Markdown |
| PDF with a text layer | poppler `pdftotext` | Reflowed paragraphs with `<!-- page N -->` markers |
| Scanned PDF | `pdftoppm` + `tesseract`, only for pages without text | `method: ocr` or `pdftotext+ocr` |
| Scanned PDF, no tesseract | — | `scanned`, nothing written |
| DRM EPUB (Adobe/other `encryption.xml`, `rights.xml`) | — | `encrypted`, not bypassed. Font obfuscation alone is fine |
| PDF needing a password, or with copying disallowed | — | `encrypted`, not bypassed |
| `.jpg`, `.opf`, `.db`, `.original_epub`, … | — | `unsupported` |

Each output starts with YAML front matter (`source`, `converter`, `method`,
`title`, `authors`) so a profile can cite the book it learned from. The full
status table is in `hermes/skills/document-converter/SKILL.md`.

## Safety guarantees

- Sources are opened read-only. No path under `--source` is ever written.
- `--source` and `--dest` must not overlap, in either direction. If they do,
  the script exits with code 2.
- Path arguments containing `..`, or resolving outside `--source`, are refused.
- Symlinks in the source are reported, not followed.
- An existing destination `.md` that doc2md did not write is left alone
  unless `--force` is given.
- Outputs are written to a temp file, then renamed into place. A destination
  directory that is a symlink leading outside `--dest` is refused.
- EPUB member paths that escape the archive and XML entity declarations are
  rejected. Member and total sizes are capped. Nothing is extracted to disk.
- OCR images go to a private temp directory that is removed afterwards.

## Enabling OCR

Ubuntu and Debian:

```bash
sudo apt-get install -y tesseract-ocr        # add tesseract-ocr-<lang> for other languages
```

Then rerun the conversion with `--force` for books reported as `scanned` or
`partial`.

## Profile Builder integration

The `profile-builder` profile proposes knowledge manifests from `/mnt/books`.
With this skill installed (`make hermes-install` copies `hermes/skills/*` into
`~/.hermes/skills/agent-ops-kit/`), its approved flow is:

1. Propose the book folders for the new profile, from filenames only.
2. After approval, dry run `doc2md.py` with `--dest data/knowledge/<profile>`
   and show the plan.
3. Convert, and report every status that is not `converted`.
4. Run `make hermes-profile NAME=<profile>`, then `/learn` in the new profile.
5. Read one converted file to check its front matter and text.

To make this part of the profile's standing instructions, add one line to
`hermes/profiles/profile-builder/SOUL.md` under **Workflow**:

```
- Convert approved EPUB/PDF sources with the `document-converter` skill (dry run first, output under data/knowledge/<profile>); never write into /mnt/books.
```

## Limits

- PDF reading order is whatever `pdftotext` produces. Multi-column pages,
  sidebars and footnotes can interleave.
- EPUB images, links and footnote anchors are dropped. Image alt text is kept.
- There is no MOBI, AZW3, DJVU or DOCX support. These are reported as
  `unsupported`.
- OCR quality depends on the scan and on tesseract. Spot-check the output.
- DRM is never removed. If the operator owns a DRM-free copy, convert that
  copy instead.
