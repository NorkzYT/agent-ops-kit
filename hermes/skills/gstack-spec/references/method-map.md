# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `spec/SKILL.md` | Phases 1-4 (why, scope and boundaries, technical interrogation, draft), issue quality standards (stakeholder context, verified current state, prioritized change, do-not-touch, dependency graph, file table, testable acceptance criteria, testing pyramid, root cause for bugs, effort, rollback) and the standard issue template | Typed contract; criteria trace to requirement IDs of the reused accepted PRD or to statements; every criterion covered by the testing plan at its level; bug framing needs root cause and a regression criterion, nothing else |
| `spec/sections/gate-and-file.md` | Phase 4.5 quality gate and fail-closed redaction before anything leaves the machine; Phase 5 filing | Redaction gate over the whole analysis; filing is a local draft only and publication needs its own approval |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, `--dedupe` issue search, codex/outside-model semantic review,
visibility probing through gh/glab, issue filing, archive writes into a gstack
state directory, `--execute` agent spawn and worktree creation, co-author
trailers. Not adapted (not in the verified evidence bundle):
`spec/sections/manifest.json`.
