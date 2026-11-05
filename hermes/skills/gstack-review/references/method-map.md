# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `review/SKILL.md` | Pre-landing review flow: scope drift (scope creep, missing requirements), critical pass, confidence calibration with the pre-emit quote gate, TODOS cross-reference, verification of claims | The diff is a digest-verified artifact; findings must quote the cited new-file line; test claims need a captured log; the fix-first step is removed |
| `review/checklist.md` | PR type rule from paths, the two-pass category list (critical then informational), not-applicable reporting, sideways sweep and forced-critical classes, severity classification, fix-first heuristic (mechanical vs ask) | PR type computed and compared; category coverage enforced per type; forced-critical classes cannot be downgraded; the heuristic becomes a `fix_route` label for the engineering path |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, platform/base-branch detection and git/gh commands, review bot
comment handling, specialist and adversarial (red team) dispatch, outside
voices, exploratory QA inside review, Fix-First auto-fix and approved-fix
application, commits, co-author trailers, review log and learnings writes.
Not adapted (not in the verified evidence bundle):
`review/sections/manifest.json`, `review/sections/plan-completion.md`,
`review/sections/shared-code-reuse.md`.
