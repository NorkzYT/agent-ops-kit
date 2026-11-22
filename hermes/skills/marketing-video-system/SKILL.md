---
name: marketing-video-system
description: Turn user screenshots, clips, logos, product facts and brand details into a rights-clean marketing video, rendered locally with HyperFrames by default (Remotion only when explicitly requested). Trigger when someone asks to make/produce/render a marketing, promo, ad, launch or demo video from assets and facts (e.g. "use these Kairo screenshots and facts to make a marketing video").
version: 2.0.0
metadata:
  hermes:
    tags: [marketing, video, hyperframes, remotion, render, rights, provenance, qa]
    category: marketing
    requires_toolsets: [terminal, file]
---

# Marketing Video System

Produce a marketing video from a user's brief and assets. New videos use the
HyperFrames lane at `tools/hyperframes-marketing/` (pinned `hyperframes@0.9.80`,
local render; Chrome uses a dead proxy, but network isolation is not proven). The Remotion lane at `tools/remotion-marketing/`
is legacy: use it only when the user explicitly asks for Remotion or is editing
an existing Remotion campaign, and say which engine you used. Never switch
engines silently when one fails; report the failure. Reference:
`docs/marketing-video.md`.

Trigger this skill whenever the user wants a marketing, promo, ad, launch or
demo **video** built from screenshots, clips, a logo, product facts, brand
colours, a CTA, an aspect ratio or music. Example: "Use these Kairo screenshots
and facts to make a marketing video."

This skill **creates artifacts only**. Never publish or post the video to any
channel unless the user explicitly asks for that exact action. User approval is
required before publication.

## Inputs to collect (the brief)

Ask for whatever is missing; do not invent any of it.

- **Screenshots / video clips** and where each came from (for provenance).
- **Logo** file (optional; a generated wordmark is used when absent).
- **Product facts** — each claim with evidence (repo file, analytics query,
  citation). On-screen factual claims come only from here.
- **Brand** — name, primary/secondary/accent colours.
- **CTA** — text and optional URL.
- **Aspect ratio(s)** — `16:9`, `9:16`, `1:1` (one or more).
- **Music preference** — original, public-domain, licensed (with proof), or
  "generate a safe tone". Popular copyrighted tracks without rights are refused.

## Workflow

1. **Brief.** Write `brief.md` from the inputs into the campaign workspace
   `data/marketing/video/<slug>/` (copy from
   `tools/remotion-marketing/templates/campaign/`; the template is shared by
   both engines). `<slug>` is gitignored.
2. **Research / claims validation.** For every on-screen claim, confirm a fact
   with evidence exists (supplied facts, repo evidence, analytics, or a
   citation). Put them in `facts.json`. Unsupported numeric or superlative
   claims are blocked or softened — put verifiable claims in `factIds`, keep
   headlines as non-factual slogans.
3. **Storyboard.** The marketing role writes a scene-by-scene `props.json`
   against the shared props shape (`tools/remotion-marketing/src/schema.ts`;
   sample: `tools/hyperframes-marketing/props/sample.json`): intro → feature →
   proof → media → outro. Each scene has a caption for sound-off legibility.
4. **Strategy review.** The `strategy` role reviews any pricing or positioning
   claim before render. Changes to pricing/positioning need its sign-off.
5. **Asset intake.** Record every asset in `provenance.json` with source,
   license, commercial-use flag and rights holder. Put media in
   `data/marketing/video/<slug>/assets/` and map ids (paths relative to that
   folder) in `props.json.assetPaths`. Check template, font and media notices
   per asset; a framework's license says nothing about an asset's rights.
6. **Build the composition.** The pipeline generates an editable
   `project.<ratio>/index.html` from the props. Edit that HTML only when a
   scene needs something the generator lacks; keep assets local (no CDN
   scripts, web fonts or remote media) and re-run the project asset gate.
   Set `props.typography` to a cleared font file: a generic or unshipped
   family makes HyperFrames fetch Google Fonts, and the gate refuses it.
7. **Render.** Run the pipeline (claims gate → provenance gate → build →
   project asset gate → pinned local HyperFrames render → poster → QA):

   ```bash
   make marketing-video-install          # once: npm ci --ignore-scripts, pinned
   make marketing-video-render \
     PROPS=data/marketing/video/<slug>/props.json \
     PROVENANCE=data/marketing/video/<slug>/provenance.json \
     OUT=data/marketing/video/<slug> RATIOS=16:9,9:16 QUALITY=draft
   ```

   Use `QUALITY=draft` while iterating and `QUALITY=delivery` for the final.
   The gates run **before** any frame renders, so an unsafe brief or an
   unlicensed file in the project never produces a video. Legacy lane, only on
   explicit request: `ENGINE=remotion make marketing-video-render ...`
   (`tools/remotion-marketing/README.md`).
8. **Music rights.** Music must be original, public-domain, correctly licensed,
   or generated under terms allowing commercial use. `mode: generated` synth's
   an original tone (`scripts/generate-tone.mjs`, normalised to -14 LUFS). The
   provenance gate fails when a used track lacks complete commercial-use rights.
9. **QA.** `scripts/qa-video.mjs` (run automatically by the pipeline, or
   standalone) checks duration, audio track, resolution, fps, caption coverage,
   safe margins and web loudness (-14 LUFS target). It writes `qa.<ratio>.json`.
10. **Deliver.** Attach `final.<ratio>.mp4`, `poster.png`, `qa.<ratio>.json`,
    `render-manifest.json` and `provenance.json` to the Kanban task with `kanban_complete`. Report the
    absolute paths. Do not publish.

## Gates (build fails when any is red)

- **Claims:** `npm run check:claims -- --props <props.json>` — every on-screen
  factual claim is backed by a fact with evidence.
- **Provenance:** `npm run check:provenance -- --props <props.json> --provenance <provenance.json>`
  — every used asset has complete, commercial-use rights.
- **Project assets (HyperFrames):** `node tools/hyperframes-marketing/scripts/check-html-assets.mjs --project <ws>/project.<ratio> --props <props.json> --provenance <provenance.json>`
  — every reference in the editable HTML is a cleared local file; no URLs.
- **QA:** `npm run qa -- --video <mp4> --props <props.json> --poster <png>`
  (in `tools/remotion-marketing/`; the HyperFrames pipeline runs it itself and
  also fails a render whose frames never change).
- Gates only, no render: `make marketing-video-gates PROPS=... PROVENANCE=... OUT=...`.

## Reproducibility

HyperFrames is pinned to `0.9.80` (registry integrity in
`tools/hyperframes-marketing/package-lock.json`); install with
`make marketing-video-install`, never `npx hyperframes@latest` or a global
install. The upstream HyperFrames skills, plugin, Studio app, cloud render,
`publish`/`share` and telemetry are not used. Remotion (legacy) stays pinned to
`4.0.524`.

## Refusals

- Popular copyrighted music/footage without rights → refuse; offer a generated
  or public-domain alternative.
- A claim with no evidence → block the render; ask for a source or soften copy.
- Publishing/posting → only on an explicit, specific request.
- A failed HyperFrames render → report it with `render.<ratio>.log`; do not
  re-render with Remotion unless the user asks for Remotion.
