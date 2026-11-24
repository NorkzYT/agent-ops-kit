# Anatomy upstream notice

This skill vendors Anatomy by Ryan, https://github.com/wheresryan22/anatomy,
at the exact revision `1482e8469b0a9e0d8cc18ea2740a01423d29ae80`
(tree `89299cdfce386a71723a7e57bb9fe219875582fc`, 2026-10-18, "Printable
sheets: A4 and Letter PDFs from any figure (#126)"). It is MIT-licensed; the
license is in `../LICENSE` (verbatim) and repeated below. Upstream has no
NOTICE file and no third-party code in the shipped files.

## What ships (byte-identical to upstream unless listed under "Changed")

- `LICENSE`
- `kit/` (all of it, including `kit/react/`)
- `references/` except `publish.md`
- `scripts/` runtime tools: `capture.mjs`, `drive.mjs`, `lines.mjs`,
  `line-ends.mjs`, `chrome-port.mjs`, `inline-kit.mjs`, `print.mjs`,
  `print-sheet.mjs`, `turn-check.mjs`, `turn-fidelity.mjs`, `turn-bench.mjs`
- `examples/*/` sources only (`build.mjs`, `live.js`, models, CSS, sheets,
  React `.ts`/`.tsx`)

## Left out, and why

- `scripts/publish.mjs`, `references/publish.md`, `scripts/fingerprint.mjs`:
  push figures to the public Anatomy community gallery through GitHub. Not
  wanted in a local agent; nothing here publishes.
- `scripts/session.mjs`: reads the coding agent's local session transcripts
  (for the gallery). Not wanted.
- `scripts/*.test.mjs`, `tests/fixtures/`: upstream's regression suite (some
  tests need `npm install` of React/TypeScript/esbuild). Run it in a clone of
  upstream if needed.
- Built example pages (`examples/**/*.html`, `*.pdf`) and `.github/` images:
  generated output, up to 4 MB each; rebuild with `node examples/<name>/build.mjs`.
- `agents/openai.yaml`, `evals/`, `CHANGELOG.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, `README.md`: repository metadata.

## Changed for Hermes

- `SKILL.md`: rewritten as a Hermes adapter (when to use and not, workspace,
  verification, accessibility, offline rules, `MEDIA:` delivery). The upstream
  SKILL.md body is `references/method.md`, unchanged except the frontmatter
  and the one publish line.
- `scripts/chrome-offline.mjs` (new): `OFFLINE_CHROME_FLAGS`, and
  `trustedTarget()`, which accepts only a local file or a loopback URL.
- `scripts/capture.mjs`, `scripts/drive.mjs`, `scripts/turn-check.mjs`,
  `scripts/print.mjs`: the import, and `...OFFLINE_CHROME_FLAGS` added after
  `--headless=new` in the Chrome launch. `capture`, `drive` and `turn-check`
  also resolve their target through `trustedTarget()` before Chrome starts
  (instead of passing any `http(s):` URL through).
- License preservation (MIT requires the notice in copies and substantial
  portions; generated pages inline the kit's code and CSS):
  - `kit/iso-kit.mjs`, `kit/iso-kit.ts`: `ANATOMY_LICENSE` (the license,
    byte-identical to `LICENSE`) and `ANATOMY_LICENSE_COMMENT`; `pageHtml()`
    writes the comment right after `<!doctype html>`, and
    `figureSvg({ standalone })` writes it as the first child of the root
    `<svg>` element, before its embedded styles (an SVG file starts with
    `<svg`, not with the comment).
  - `scripts/inline-kit.mjs`: every helper's output starts with the full
    license as a `/*! @license */` comment (once per helper).
  - `scripts/print-sheet.mjs`: printed sheets (which embed the kit CSS) carry
    the comment after `<!doctype html>`.
  - `kit/iso-kit.ts`, `kit/iso.css`, `kit/react/draw.tsx`,
    `kit/react/live.tsx`: start with the full license as a `/*! @license */`
    comment. Whether a bundler keeps it depends on the user's toolchain, so
    bundled or minified distributions must ship `LICENSE` alongside. `kit/iso.css`
    is `ISO_CSS` after that header (`references/kit.md` and `method.md` still
    say "the same text", unchanged from upstream).
  - `SKILL.md` and `references/react.md`: the workspace copy and the React
    layout include `LICENSE`.

Upstream blob ids of the changed files: `SKILL.md` 36f22e40,
`scripts/capture.mjs` 715b644f, `scripts/drive.mjs` 825e5084,
`scripts/print.mjs` bc2c3558, `scripts/turn-check.mjs` a3a84bce.
`git diff` against those blobs (or a clone at the pinned revision) shows
every other change listed above.

## Name conflict with the official `creative/anatomy`

Hermes ships an official optional skill with the same name
(`optional-skills/creative/anatomy`, installed only by
`hermes skills install official/creative/anatomy`), ported from an older
upstream revision (`dbf697a`). This copy keeps the name `anatomy` because it
is the same upstream skill and the name is what users ask for; it differs by
the newer pin, the network-reducing Chrome flags (not isolation), the trusted-target guard and the
license embedding.

If both are installed in the same skills directory (this one as
`agent-ops-kit/anatomy`, the official one as `creative/anatomy`), Hermes no
longer resolves the bare name `anatomy`: both are listed by their exact
relative path, and `skill_view` needs that path. Resolution: install only
one. Keeping this one means not installing the official skill (or
uninstalling it with `hermes skills`); keeping the official one means
removing `agent-ops-kit/anatomy` from the skills directory. Adding one path to
`skills.disabled` hides that copy from the index, but the bare name stays
ambiguous while both are on disk, so call the other by its path.
Agent-Ops-Kit's installer never touches the official copy.

## License (verbatim)

```text
MIT License

Copyright (c) 2026 Ryan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
