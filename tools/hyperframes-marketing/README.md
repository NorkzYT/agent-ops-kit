# HyperFrames Marketing Video Lane (default)

Turns the shared campaign props into an editable HyperFrames project
(`index.html` + `assets/`) and renders it locally with the pinned
`hyperframes@0.9.80` CLI. Claims, provenance and QA come from
`../remotion-marketing/scripts/` (shared, Node stdlib only); this lane adds a
project asset gate for the editable HTML. Full guide: `docs/marketing-video.md`.

## Commands

```bash
npm ci --ignore-scripts      # or: make marketing-video-install
npm test                     # builder + gate tests, no render
node scripts/render-campaign.mjs --out <dir> [--props p.json --provenance prov.json \
  --assets <dir> --ratios 16:9,9:16,1:1 --quality draft|looks|delivery --gates-only]
node scripts/check-html-assets.mjs --project <dir>/project.16x9 --props p.json --provenance prov.json
```

## Layout

```
scripts/build-composition.mjs   props → index.html (CSS keyframes, local @font-face only, no GSAP/CDN/web fonts)
scripts/check-html-assets.mjs   pre-render gate over the built/edited project
scripts/render-campaign.mjs     gates → build → asset gate → render → poster → QA
scripts/chrome-offline.sh       Chrome launcher: dead proxy for non-loopback, background services off
props/                          synthetic sample + provenance (generated tone, sha256-pinned OFL Liberation Sans)
tests/                          node:test, incl. an unlicensed-image negative control
```

## Pin

| Package | Version | Registry integrity | Upstream identity checked |
|---|---|---|---|
| `hyperframes` | 0.9.80 | `sha512-L/ZwmUUDfCES0TxHgHzSUWmJ2t4948rafc4GU/uPi4H++chB+LRUghzGbg3FBGgVa13V3thLXalz/LC/7yH47w==` | `repository.url` heygen-com/hyperframes; CLI `package.json` at `0774d96462ab3e1145ac5186a3cbba98426f75f0` is 0.9.80; Apache-2.0 |

Install scripts are disabled (`.npmrc`); the only dependency that declares one
is `esbuild`, whose platform binary arrives as an optional dependency.

## Render isolation

`render-campaign.mjs` runs the CLI with a minimal environment (no inherited
tokens), a private `HOME` under the output dir, a short private `TMPDIR`
(Chrome's socket path limit), `HYPERFRAMES_NO_TELEMETRY=1`, `DO_NOT_TRACK=1`,
update checks, auto-install and skill installs off, `--strict` and
`--refuse-outside-files`. It never calls `publish`, `share`, `cloud`, `lambda`,
`skills`, `init` or `upgrade`.

Asset paths are checked before anything is written (also with `--gates-only`):
URLs, absolute paths, `..` escapes and any symlink below the assets root are
refused (exit 2). The generated tone is staged in a temp dir and renamed into
place; font copies use `COPYFILE_EXCL`. A concurrent local writer racing these
checks is out of scope.

### Network

Network isolation is **not proven**. `chrome-offline.sh` points Chrome at a
dead proxy for non-loopback hosts and turns background services off, but:

- the HyperFrames CLI is a Node process with normal host networking; nothing
  here stops or observes its own requests (compile-time font resolution in
  0.9.80 fetches Google Fonts for generic/unknown families, which the asset
  gate refuses, but other code paths are not audited);
- a Chrome proxy flag is not a sandbox, and no test captures render traffic.

The project asset gate (`check-html-assets.mjs`) is a regex heuristic over
HTML, CSS, SVG and JS (URLs, `data:` URIs, `font`/`font-family` including the
shorthand, script network calls, URL literals, imports, `eval`, DOM/style
writes, meta refresh, `srcdoc`). Obfuscated script can evade it. For a real
guarantee, run the render in a network namespace with no route (for example a
container with `--network none`); that is not done or tested here.
