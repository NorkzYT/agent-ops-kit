# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `plan-design-review/SKILL.md` | Step 0 initial rating and "what a 10 looks like", DESIGN.md status, existing design leverage, the 0-10 rating method (rate, gap, recommend, decide), design principles | Typed analysis; design system status bound to a reviewed design package or brief; mockups are a paid action needing approval and never run here |
| `plan-design-review/sections/review-sections.md` | Seven passes with the anti-skip rule, the interaction state table (loading/empty/error/success/partial), the journey storyboard, AI-slop hard rejections and the below-8 cap, design system alignment, responsive and accessibility, unresolved decisions table, decisions carried across passes | All seven passes required; missing state cells and unsupported journey steps keep their pass below 10; storyboard support must be a verbatim plan quote; decisions settle only through supplied answers |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, platform/branch detection, brain context and learnings, the
design binary and Step 0.5 mockups, comparison boards, outside design voices,
plan-file edits, TODOS.md and JSONL task writes, review log and readiness
dashboard writes, ExitPlanMode. Not adapted (not in the verified evidence
bundle): `plan-design-review/sections/manifest.json`,
`plan-design-review/sections/checklist.md`.
