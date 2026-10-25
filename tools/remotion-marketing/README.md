# Remotion Marketing Video System

A reusable, rights-aware pipeline that turns a marketing brief and assets into a
QA'd MP4 with a poster. Built on [Remotion](https://www.remotion.dev). Driven by
the Hermes `marketing-video-system` skill and the
`marketing-video-team` recipe.

## What it gives you

- **One typed composition** (`MarketingVideo`) that renders 16:9, 9:16 and 1:1
  from the same props (`src/schema.ts`, zod-validated).
- **Reusable scene components** (`src/components/`): background, safe area,
  logo/wordmark, captions, fact badges, CTA.
- **Rights gates that fail the build**: a claims checker and a provenance
  checker run before any frame renders.
- **Generated, rights-clean music**: an original synthesised tone normalised to
  -14 LUFS. No third-party samples.
- **QA with ffprobe/ffmpeg**: duration, audio, resolution, fps, caption
  coverage, safe margins, web loudness → `qa.<ratio>.json`.

## Layout

```
src/            composition, schema, theme, reusable components
scripts/        check-provenance · check-claims · qa-video · generate-tone · render-campaign
props/          sample.json + sample.provenance.json (the tracked demo)
templates/      campaign workspace to copy into data/marketing/video/<slug>/
tests/          node:test unit tests for the three gates
```

## Quick start

```bash
npm ci                 # installs Remotion 4.0.524 (pinned via package-lock.json)
npm test               # unit tests for the claims/provenance/QA gates

# Render the tracked demo (generated geometry + tone, no third-party rights):
export REMOTION_BROWSER_EXECUTABLE=/path/to/chrome-headless-shell   # if Remotion can't auto-download
node scripts/render-campaign.mjs --out ../../data/marketing/video/sample-demo --draft true
```

Renders land in the `--out` dir: `poster.png`, `draft.mp4`,
`final.16x9.mp4`, `qa.16x9.json`, `provenance.json`.

## Gates

```bash
npm run check:claims     -- --props props/sample.json
npm run check:provenance -- --props props/sample.json --provenance props/sample.provenance.json
npm run qa               -- --video <mp4> --props props/sample.json --poster <png>
```

Music must be original, public-domain, correctly licensed, or generated under
terms that permit commercial use. Popular copyrighted tracks without rights are
rejected by the provenance gate.

## Reproducibility & Remotion agent skills

Remotion is pinned to `4.0.524` and locked in `package-lock.json`; always use
`npm ci`. Rendering needs a Chromium/`chrome-headless-shell`; Remotion downloads
one on first render, or point `REMOTION_BROWSER_EXECUTABLE` at an existing shell.

Remotion publishes official agent-skill guidance at
<https://www.remotion.dev/docs>. It is documentation rather than an npm package;
adopt it against this pinned Remotion version so behaviour stays reproducible.
