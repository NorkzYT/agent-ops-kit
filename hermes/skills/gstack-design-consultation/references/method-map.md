# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `design-consultation/SKILL.md` | Phase 1 product context and the memorable-thing forcing question; "propose with reasons", coherence, accept the user's final choice; anti-slop rules | context comes from an accepted, digest-bound product brief; the memorable thing is a requester answer with a source or stays pending; rules enforced by the validator |
| `design-consultation/sections/proposal-and-preview.md` | Phase 3 proposal (aesthetic, decoration, layout, colour, motion, type procedure, banned/overused faces, font-verification fallback, anti-slop list, light/dark from the use scene, coherence validation); Phase 5 Path B preview requirements | type rules and font verification checked against supplied sources; semantic tokens with computed WCAG contrast; designed empty/loading/error/permission states and journey added as required outputs; the preview is rendered offline from the analysis with no script, no web fonts and no remote assets |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, question tuning and taste profiles, prior/captured learnings,
web research inside this step (public research is a separate approved
capability), outside-model design voices, the gstack design binary and AI
mockups, comparison boards, browser setup and `open` of the preview, the
JavaScript light/dark toggle (both palettes are shown statically instead),
loading web fonts by `<link>`, and writing or updating `DESIGN.md` in a
repository. Not adapted (not in the verified evidence bundle):
`design-consultation/sections/manifest.json`.
