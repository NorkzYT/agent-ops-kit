# Method map (gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `qa/SKILL.md` | QA baseline before fixes, test plan context from the engineering review, output structure, report contents (functional coverage as passing/failing/blocked/not-run contracts, never a score), separation of browser and functional results | Audit half only: report and charter rendered from supplied captures; Phase 8 fix loop, commits and TODOS updates removed |
| `qa/sections/qa-patterns.md` | Full/quick/regression modes, documenting issues with replayed evidence, "never invent issues", untested categories excluded | Mode scaling enforced on the charter; browser outcomes need supplied screenshots; nothing scored without evidence |
| `qa/sections/system-functional.md` | Functional modes, the contract map (success, invalid input, auth, CLI process, state transitions, timeout, retry, idempotency, concurrency, partial failure) and outcomes (pass/fail/blocked/not run/inconclusive/not applicable), retained exact command, exit, stdout and stderr | Captures carry the exact command and exit status; the validator recomputes pass/fail and refuses a pass without a matching capture |

Disabled upstream behaviours: shared preamble and telemetry, update_check and
auto-upgrade, platform/branch detection, the Aside/gstack browser and its
setup, local port probing, browser cookie import and authenticated sessions
(direct human login only), learnings, Phase 7 triage into fixes, the Phase 8
fix loop and regression-test writes, commits, report copies into a gstack
state directory, TODOS.md updates. Not adapted (not in the verified evidence
bundle): `qa/sections/manifest.json`, `qa/sections/scope.md`,
`qa/sections/exploratory.md`, `qa/sections/browser-verify.md`.
