# Marketing video

New marketing videos render with **HyperFrames** (`tools/hyperframes-marketing/`).
**Remotion** (`tools/remotion-marketing/`) stays as the legacy lane and runs only
when you ask for it with `ENGINE=remotion`. There is no fallback: if the chosen
engine fails, the command fails and says so.

Both lanes share the same campaign props, the same claims and asset-rights
gates, and the same ffprobe QA (`tools/remotion-marketing/scripts/`, Node stdlib
only). Hermes reaches this through the `marketing-video-system` skill and the
`marketing-video-team` recipe.

## Install (project-local, pinned)

```bash
make marketing-video-install                  # HyperFrames 0.9.80, npm ci --ignore-scripts
ENGINE=remotion make marketing-video-install  # legacy lane only
```

`hyperframes@0.9.80` is pinned exactly in `package.json` and locked with its
registry integrity in `package-lock.json`. It was checked against the upstream
identity before pinning: npm `repository.url` is
`git+https://github.com/heygen-com/hyperframes.git`, the CLI `package.json` at
upstream commit `0774d96462ab3e1145ac5186a3cbba98426f75f0` declares version
`0.9.80`, and the tarball's sha1 `1dd7519bff42260f7f3b6fa43e793332d68d9643`
matches the registry. Nothing is installed globally; install scripts are off
(`.npmrc`). Needs Node 22+, FFmpeg and a Chrome (default `/usr/bin/google-chrome`).

## Render

```bash
make marketing-video-render                    # tracked synthetic sample → tools/hyperframes-marketing/out
make marketing-video-render \
  PROPS=data/marketing/video/<slug>/props.json \
  PROVENANCE=data/marketing/video/<slug>/provenance.json \
  OUT=data/marketing/video/<slug> RATIOS=16:9,9:16 QUALITY=delivery
make marketing-video-gates PROPS=... PROVENANCE=... OUT=...   # gates only, no render
ENGINE=remotion make marketing-video-render OUT=...           # legacy lane, explicit
```

Assets resolve from `ASSETS=` (default: `assets/` next to the props file). The
HyperFrames lane runs, in order, stopping at the first failure:

1. claims gate — every numeric/superlative on-screen claim needs a fact with evidence;
2. provenance gate — every used asset needs a record with `commercialUse: true`
   and a commercial license;
3. generated tone (when `music.mode` is `generated`);
4. build `project.<ratio>/` (editable `index.html` + copied, cleared assets only);
5. **project asset gate** (`check-html-assets.mjs`) — scans the built HTML/CSS
   and `assets/`: URLs, `data:` URIs, absolute or escaping paths, script network
   calls and any file without a cleared record are refused;
6. `hyperframes render --strict --refuse-outside-files` from the pinned local
   CLI, with a private `HOME`, a minimal environment (no inherited tokens),
   telemetry, update checks and skill installs off, and Chrome launched through
   `scripts/chrome-offline.sh` (Chrome's non-loopback requests go to a dead
   local proxy; background services off). This is **not** network isolation:
   the HyperFrames Node CLI runs with ordinary host networking, and no test
   proves that a render makes no outbound connection;
7. poster, motion sample (frames must change) and ffprobe QA → `qa.<ratio>.json`.

Outputs in `OUT`: `final.<ratio>.mp4`, `poster.png`, `qa.<ratio>.json`,
`project.<ratio>/`, `render.<ratio>.log`, `render-manifest.json` (exact command,
exit, duration) and `provenance.json`.

## Editing a HyperFrames project

`project.<ratio>/index.html` is plain HTML with CSS keyframes and
`data-start`/`data-duration` clips. Edit it, then re-run the asset gate before
rendering it again:

```bash
node tools/hyperframes-marketing/scripts/check-html-assets.mjs \
  --project data/marketing/video/<slug>/project.16x9 \
  --props data/marketing/video/<slug>/props.json \
  --provenance data/marketing/video/<slug>/provenance.json
```

Do not add CDN scripts (the upstream templates load GSAP from jsDelivr), web
fonts or remote media: the gate refuses them. Fonts are assets too: props must
set `typography` (`family` plus `faces[{assetId, weight, style}]`), each face a
font file under `assets/` with a cleared provenance record. HyperFrames 0.9.80
rewrites a generic primary family (`sans-serif`, `system-ui`, ...) to "Inter"
and downloads Inter from Google Fonts at compile time, and does the same for a
named family without a local `@font-face`, so the gate refuses both. The
sample uses Liberation Sans 2.1.5 (SIL OFL 1.1, license `ofl-1.1`), vendored
unmodified with its copyright/license notice and source record in
`tools/hyperframes-marketing/fonts/liberation-2.1.5/` (`SOURCE.md`). The run
copies it into `assets/fonts/` only when its sha256 matches the provenance
record (`localSource`, resolved relative to the provenance file, + `sha256`); a
mismatched or missing file stops the run. Host font packages are never used:
other builds of the same font (for example a CI runner image's) have different
bytes and would be refused.
Upstream HyperFrames skills,
registry blocks, Studio, cloud render, `publish`/`share` and the plugin are not
installed or used here.

## Tests

```bash
make test-marketing-video                    # HyperFrames builder/gate tests + dispatcher tests (no render)
ENGINE=remotion make test-marketing-video    # legacy lane unit tests
```

## Known limits

- Without `chrome-headless-shell`, local renders fall back to screenshot
  capture and may use software GL (as on the host these were measured on), so
  a 9 s 1080p video can take minutes.
- The sample font is vendored in the repo (sha256 pinned), so a fresh clone
  gets a working sample without a system font package; your own campaigns
  need their own cleared font files and provenance records.
- Chrome still makes a UDP route probe to a public IPv6 resolver address at
  start; a UDP `connect` sends no packet. TCP traffic was observed only to
  loopback and the dead proxy port in the recorded trace.
- `--docker` renders are not wired; they would pull an image from a registry.
