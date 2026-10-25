# Campaign workspace template

Copy this folder to `data/marketing/video/<slug>/` (gitignored) to start a
campaign. The `marketing-video-system` skill drives the pipeline from here.

```bash
slug=my-campaign
cp -r tools/remotion-marketing/templates/campaign data/marketing/video/$slug
# fill brief.md, facts.json, provenance.json, props.json
cd tools/remotion-marketing && npm ci
node scripts/render-campaign.mjs \
  --props ../../data/marketing/video/$slug/props.json \
  --provenance ../../data/marketing/video/$slug/provenance.json \
  --out ../../data/marketing/video/$slug --ratios 16:9,9:16 --draft true
```

Files:

- `brief.md` — the inputs (product, brand, CTA, aspect ratios, music, assets).
- `facts.json` — supported claims; each fact needs real evidence.
- `props.json` — typed storyboard (see `src/schema.ts`).
- `provenance.json` — rights record for every asset used.

Renders (`poster.png`, `draft.mp4`, `final.<ratio>.mp4`, `qa.<ratio>.json`) land
next to these files and stay gitignored.
