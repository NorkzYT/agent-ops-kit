---
name: marketing-video-system
description: Turn user screenshots, clips, logos, product facts and brand details into a rights-clean marketing video with Remotion. Trigger when someone asks to make/produce/render a marketing, promo, ad, launch or demo video from assets and facts (e.g. "use these Kairo screenshots and facts to make a marketing video").
version: 1.0.0
metadata:
  hermes:
    tags: [marketing, video, remotion, render, rights, provenance, qa]
    category: marketing
    requires_toolsets: [terminal, file]
---

# Marketing Video System

Produce a marketing video from a user's brief and assets. The reusable Remotion
project lives at `tools/remotion-marketing/`; this skill drives it end to end
with rights and QA gates that fail the build when something is unsafe.

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
   `tools/remotion-marketing/templates/campaign/`). `<slug>` is gitignored.
2. **Research / claims validation.** For every on-screen claim, confirm a fact
   with evidence exists (supplied facts, repo evidence, analytics, or a
   citation). Put them in `facts.json`. Unsupported numeric or superlative
   claims are blocked or softened — put verifiable claims in `factIds`, keep
   headlines as non-factual slogans.
3. **Storyboard.** The marketing role writes a scene-by-scene `props.json`
   against `tools/remotion-marketing/src/schema.ts` (intro → feature → proof →
   media → outro). Each scene has a caption for sound-off legibility.
4. **Strategy review.** The `strategy` role reviews any pricing or positioning
   claim before render. Changes to pricing/positioning need its sign-off.
5. **Asset intake.** Record every asset in `provenance.json` with source,
   license, commercial-use flag and rights holder. Copy media into
   `tools/remotion-marketing/public/` and map ids in `props.json.assetPaths`.
6. **Build the composition.** Reuse the `MarketingVideo` composition. Only add
   or edit components under `src/` when a scene needs something new; keep props
   typed and components reusable.
7. **Render.** Run the pipeline (gates → poster still → low-res draft →
   final(s)):

   ```bash
   cd tools/remotion-marketing
   npm ci
   export REMOTION_BROWSER_EXECUTABLE=/path/to/chrome-headless-shell   # if no auto-download
   node scripts/render-campaign.mjs \
     --props   ../../data/marketing/video/<slug>/props.json \
     --provenance ../../data/marketing/video/<slug>/provenance.json \
     --out     ../../data/marketing/video/<slug> \
     --ratios  16:9,9:16 --draft true
   ```

   The pipeline runs the claims gate then the provenance gate **before** any
   frame renders, so an unsafe brief never produces a video.
8. **Music rights.** Music must be original, public-domain, correctly licensed,
   or generated under terms allowing commercial use. `mode: generated` synth's
   an original tone (`scripts/generate-tone.mjs`, normalised to -14 LUFS). The
   provenance gate fails when a used track lacks complete commercial-use rights.
9. **QA.** `scripts/qa-video.mjs` (run automatically by the pipeline, or
   standalone) checks duration, audio track, resolution, fps, caption coverage,
   safe margins and web loudness (-14 LUFS target). It writes `qa.<ratio>.json`.
10. **Deliver.** Attach `final.<ratio>.mp4`, `poster.png`, `qa.<ratio>.json` and
    `provenance.json` to the Kanban task with `kanban_complete`. Report the
    absolute paths. Do not publish.

## Gates (build fails when any is red)

- **Claims:** `npm run check:claims -- --props <props.json>` — every on-screen
  factual claim is backed by a fact with evidence.
- **Provenance:** `npm run check:provenance -- --props <props.json> --provenance <provenance.json>`
  — every used asset has complete, commercial-use rights.
- **QA:** `npm run qa -- --video <mp4> --props <props.json> --poster <png>`.

## Reproducibility

Remotion is pinned to `4.0.524` and committed via `package-lock.json`; use
`npm ci`. Official Remotion agent-skill guidance lives at
`https://www.remotion.dev/docs` — pin the same Remotion version when adopting it.

## Refusals

- Popular copyrighted music/footage without rights → refuse; offer a generated
  or public-domain alternative.
- A claim with no evidence → block the render; ask for a source or soften copy.
- Publishing/posting → only on an explicit, specific request.
