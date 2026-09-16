# Marketing video team: task prompts

Fill the placeholders and paste into each Kanban task description. Workspace is
`data/marketing/video/<slug>/` (gitignored), copied from
`tools/remotion-marketing/templates/campaign/`.

## validate_claims (assignee: research)
```
Validate every factual claim for the {product} marketing video.
Inputs: {supplied_facts}, repo evidence, analytics.
Write facts.json: each fact = {id, claim, evidence}. Evidence must be a real
source (file path, query, citation). Flag any brief copy that makes a numeric or
superlative claim with no evidence; it must be softened or dropped.
Expected output: facts.json and a list of claims to soften.
```

## storyboard (assignee: marketing, parent: validate_claims)
```
Write props.json for the {product} video against
tools/remotion-marketing/src/schema.ts.
- Brand: {name, primary/secondary/accent colours}. CTA: {text,url}.
- Aspect ratio(s): {16:9|9:16|1:1}. Music: {mode}.
- Scenes intro→feature→proof→(media)→outro; each has a caption. Put verifiable
  claims in factIds only; keep headlines as non-factual slogans.
- Record every asset in provenance.json (source, license, commercialUse,
  rightsHolder) and map ids in props.json.assetPaths.
Expected output: props.json + provenance.json that pass check:claims and
check:provenance.
```

## review_positioning (assignee: strategy, parent: storyboard)
```
Review pricing and positioning claims in props.json. Return specific change
requests or approve with LGTM. Nothing renders until this passes.
```

## build_and_render (assignee: coder, parent: review_positioning)
```
Reuse the MarketingVideo composition. Add components only if a scene needs one;
keep props typed. Run:
  cd tools/remotion-marketing && npm ci
  node scripts/render-campaign.mjs --props <ws>/props.json \
    --provenance <ws>/provenance.json --out <ws> --ratios {ratios} --draft true
The pipeline runs the claims + provenance gates first, then poster/draft/final.
Expected output: poster.png, draft.mp4, final.<ratio>.mp4 in the workspace.
```

## qa_and_deliver (assignee: coder, parent: build_and_render)
```
Run QA on each final render:
  node scripts/qa-video.mjs --video <ws>/final.<ratio>.mp4 \
    --props <ws>/props.<ratio>.json --poster <ws>/poster.png \
    --report <ws>/qa.<ratio>.json
Confirm duration, audio track, resolution, fps, caption coverage, safe margins
and loudness (-14 LUFS target). Attach final MP4(s), poster, qa.<ratio>.json and
provenance.json to the task. Report absolute paths. Do not publish; wait for
user approval.
```
