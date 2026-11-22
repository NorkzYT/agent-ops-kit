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
Write props.json for the {product} video against the shared props shape
(tools/remotion-marketing/src/schema.ts; sample
tools/hyperframes-marketing/props/sample.json).
- Brand: {name, primary/secondary/accent colours}. CTA: {text,url}.
- Aspect ratio(s): {16:9|9:16|1:1}. Music: {mode}.
- Scenes intro→feature→proof→(media)→outro; each has a caption. Put verifiable
  claims in factIds only; keep headlines as non-factual slogans.
- Record every asset in provenance.json (source, license, commercialUse,
  rightsHolder) and map ids in props.json.assetPaths (paths relative to
  <ws>/assets/).
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
Render with the default HyperFrames lane (pinned, local). Run:
  make marketing-video-install
  make marketing-video-render PROPS=<ws>/props.json \
    PROVENANCE=<ws>/provenance.json OUT=<ws> RATIOS={ratios} QUALITY=draft
The pipeline runs the claims, provenance and project asset gates first, then
renders and QAs each ratio. If the render fails, report render.<ratio>.log;
do not switch to Remotion unless the task says ENGINE=remotion.
Expected output: final.<ratio>.mp4, poster.png, qa.<ratio>.json,
render-manifest.json in the workspace.
```

## qa_and_deliver (assignee: coder, parent: build_and_render)
```
The HyperFrames pipeline already wrote qa.<ratio>.json; to re-run QA by hand:
  node tools/remotion-marketing/scripts/qa-video.mjs --video <ws>/final.<ratio>.mp4 \
    --props <ws>/props.json --poster <ws>/poster.png \
    --report <ws>/qa.<ratio>.json
Confirm duration, audio track, resolution, fps, caption coverage, safe margins
and loudness (-14 LUFS target). Attach final MP4(s), poster, qa.<ratio>.json and
provenance.json to the task. Report absolute paths. Do not publish; wait for
user approval.
```
