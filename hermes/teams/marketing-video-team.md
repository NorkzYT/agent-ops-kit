# Marketing video team: research → marketing → coder → QA → strategy

Builds a rights-clean marketing video from a user brief with the
`marketing-video-system` skill and the reusable Remotion project at
`tools/remotion-marketing/`. The pipeline gates claims and asset rights before
anything renders, and keeps a human approval step before publication.

## Roles

- **research** — Claims Validator. Turn the brief's facts into `facts.json`.
  Confirm every on-screen claim has evidence (supplied facts, repo files,
  analytics, citations). Block or soften unsupported numeric/superlative claims.
- **marketing** — Storyboard + Copy. Write the scene-by-scene `props.json`
  against `tools/remotion-marketing/src/schema.ts`. Verifiable claims go in
  `factIds`; headlines stay non-factual. Record asset intake in
  `provenance.json`.
- **strategy** — Reviewer. Sign off pricing and positioning claims before render.
- **coder** — Remotion Builder. Reuse the `MarketingVideo` composition, add
  components only when a scene needs them, then run the render pipeline.
- **QA** (a `coder` clone or the same coder) — run `scripts/qa-video.mjs` and
  confirm duration, audio, resolution, fps, captions, safe margins and loudness.

## Wiring

```bash
make hermes-profile NAME=research
make hermes-profile NAME=marketing
make hermes-profile NAME=strategy
make hermes-profile NAME=coder

hermes kanban create "Validate claims + facts.json for <product> video"   --assignee research
hermes kanban create "Storyboard props.json + provenance.json"            --assignee marketing --parent t_facts
hermes kanban create "Review pricing/positioning claims"                  --assignee strategy  --parent t_story
hermes kanban create "Build composition + render still/draft/final"       --assignee coder     --parent t_review
hermes kanban create "QA render (ffprobe) + attach MP4/poster/QA/prov"    --assignee coder     --parent t_build
hermes kanban watch
```

The coder attaches `final.<ratio>.mp4`, `poster.png`, `qa.<ratio>.json` and
`provenance.json`, then calls `kanban_complete`. The orchestrator reports the
paths and asks the user for approval — it never publishes on its own.

Task prompts are in `marketing-video-team-tasks.md`.
