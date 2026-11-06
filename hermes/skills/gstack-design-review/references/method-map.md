# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `design-review/SKILL.md` | Phases 1–6 audit baseline: first impression format, design-system extraction, the ten-category checklist, trunk test, critique format, grading (A start, high −1, medium −½, category weights), "screenshots are evidence", depth over breadth, quick wins | checklist items that can be read from markup, CSS and tokens run as static checks over digest-verified artifacts; visual items need supplied captures; first impression only from a supplied screenshot; grades only for categories something looked at |
| `design-review/sections/craft-audit.md` | lint-cannot-see rules: claims come from the user, illustrative content looks real, ship floor (title, skip link, landmarks, alt, labels, real links), motion rules (reduced motion, no plain ease-in), copy tells | skip link, landmarks, alt, labels, dead links, reduced motion, easing, gradients, uniform radius, buzzwords, "Oops" and placeholder names checked statically; invented usability or screen-reader results rejected |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, prior/captured learnings, the Aside/`$B` browser and its setup,
browser cookie import, the gstack design lint binary and DOM dump, diff-aware
mode (git/gh commands), the design binary and target mockups, outside voices,
Phase 7 triage into fixes, the Phase 8 source fix loop, commits and
regression tests, Phase 9 re-audit after fixes, TODOS.md updates and writing
`design-baseline.json` into a state directory. Repairs go to the existing
engineering path as a separate card. Not adapted (not in the verified evidence
bundle): `design-review/sections/manifest.json`.
