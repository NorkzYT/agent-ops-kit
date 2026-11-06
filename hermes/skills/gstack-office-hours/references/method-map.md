# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `office-hours/SKILL.md` | Startup vs builder modes, Phase 3 premise challenge (premises the user agrees or disagrees with), Phase 4 alternatives (2-3, one minimal viable, one ideal architecture, optional lateral; effort/risk/pros/cons/reuses), recommendation gated on explicit approval | Premises and the approach decision settle only through a supplied, recorded requester answer; the brief is rendered as a draft that stays `needs_input` until then |
| `office-hours/sections/phase-2a-startup-diagnostic.md` | The six forcing questions, red flags (interest is not demand), smart routing by product stage, smart-skip of questions already answered | Routing table enforced; already-answered questions are `covered` by a verbatim quote of an accepted artifact; interest phrasing refused as demand evidence |
| `office-hours/sections/design-and-handoff.md` | Startup/builder design doc sections (problem, demand evidence, status quo, user and wedge, premises, approaches, recommendation, open questions, success criteria, assignment) | Rendered as `opportunity-brief.md`; handoff to the lean PRD carries no approval |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, brain context and learnings, the Phase 3.5 cross-model second
opinion, Phase 2.75 landscape web search through Aside, Phase 4.25 visual
mockups, founder signal synthesis and builder profile, the spec review loop,
brain write-back and cache refresh, the tiered relationship closing, and
writing the design doc into a gstack state directory. Not adapted (not in the
verified evidence bundle): `office-hours/sections/manifest.json`,
`office-hours/sections/phase-2b-builder-brainstorm.md`.
