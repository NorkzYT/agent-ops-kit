# Campaign workspace template

Copy this folder to `data/marketing/video/<slug>/` (gitignored) to start a
campaign. The `marketing-video-system` skill drives the pipeline from here.

```bash
slug=my-campaign
cp -r tools/remotion-marketing/templates/campaign data/marketing/video/$slug
# fill brief.md, facts.json, provenance.json, props.json
mkdir -p data/marketing/video/$slug/assets   # media mapped by props.assetPaths
make marketing-video-install
make marketing-video-render PROPS=data/marketing/video/$slug/props.json \
  PROVENANCE=data/marketing/video/$slug/provenance.json \
  OUT=data/marketing/video/$slug RATIOS=16:9,9:16 QUALITY=draft
# legacy lane, only on explicit request: ENGINE=remotion make marketing-video-render ...
```

Files:

- `brief.md` — the inputs (product, brand, CTA, aspect ratios, music, assets).
- `facts.json` — supported claims; each fact needs real evidence.
- `props.json` — typed storyboard (see `src/schema.ts`).
- `provenance.json` — rights record for every asset used.

Renders (`poster.png`, `final.<ratio>.mp4`, `qa.<ratio>.json`, `project.<ratio>/`) land
next to these files and stay gitignored.
