# Engineering completion: the coordinator finishes the task

Goal: Hermes gets an authorized engineering task, does the work, recovers from
routine problems, validates the result, commits and pushes the approved branch,
completes the right Kanban stages and reports a verified outcome, without the
operator having to unblock or restart it again and again.

Internal fixes (concurrency, delegation, patch management, security tooling)
matter only where they make that possible. This page holds the current bounded
milestone, its acceptance criteria and the deferred backlog.

## Design decision

No second coordinator and no new board chain. The existing default orchestrator
stays the persistent engineering coordinator. It runs the `kanban-workflow`
code card, and the existing graph (code → lanes → round → review → report) stays
the source of truth. The completion policy lives in
`hermes/skills/kanban-workflow/scripts/completion.py`, called as
`workflow.py completion …`. Its state is `completion` records written through
the workflow's ledger-first `put_record` onto the request's own cards: no other
board tool, no direct SQLite access.

## Stages and criteria

| Stage | Met when (computed from records + the current source) |
|-------|--------------------------------------------------------|
| understand | an acceptance-criteria record exists (criteria and required checks recovered from the request, card and repository before asking) |
| plan | a plan record at least as new as the criteria |
| implement | the current round's latest delegate record is `complete` and no delegate is still `started` (`partial` / `failed` / `interrupted` keep it unmet with the remaining work) |
| test | every required check has a passing test record whose source content id equals the current one |
| review | a passing full-diff review record on the current content |
| commit | the tree is clean at a commit, and no index entry is flagged assume-unchanged or skip-worktree |
| push | HEAD is the SHA `record-sha` recorded, and (in `handoff`) `git ls-remote` shows the branch at that SHA now |
| validate | every lane of the final round has evidence that the exact SHA passed (existing gate rows) |
| complete | the existing report gate passes (independent review approved that SHA) and a remote-verified handoff exists for it |

**Implementation handoff** = understand through push. `completion handoff`
exits 0 only then and prints the `kanban_complete` summary. Completing the code
card lets its validation children (e.g. the Mac card) start through the board's
own dependency graph. **Final completion** = validate + complete, recorded by
`completion final` on the report card. A finished delegate is never completion.

### Source identity

`HEAD`, the branch, and one content id: SHA-256 over every tracked and
untracked (not ignored) path's git mode (exec bit, symlink `120000`, gitlink
`160000`) and the digest of its bytes (a symlink's target string, never
followed; a submodule's index object; special files by type only). The ledger
keeps digests only, never contents or paths. Evidence counts only while its
content id equals the current one. A later edit, rebase or checkout makes it
stale. A commit of the same bytes keeps it valid.

Limits: ignored files (build output, local env files) are outside the identity.
An index entry flagged `assume-unchanged` or `skip-worktree` hides its changes
from `git status`, thus the commit stage (and handoff) fails closed while any
exists, modified or not; a sparse checkout therefore never hands off.
The identity is taken when the result is recorded, thus record right after the
command runs. Records are same-uid self-assertions by the coordinator (the
command, exit code and counts), not a sandboxed re-run.

### Recovery and blocks

- `completion recover --class routine` records the failure signature, the
  diagnosis and the next action. An identical (signature, diagnosis, action)
  repeat exits 2: change the diagnosis or the approach. Attempts are bounded
  per stage (8 since the stage was last met). After that it exits 4 with
  `recovery-exhausted: … work is preserved; smallest intervention: …`.
  "Met" is stage-specific: test = the latest result of every required check on
  one source content is a pass; review = a passing full-diff review; implement,
  commit, push = a handoff; validate, complete = a final completion. A unit
  pass while lint still fails resets nothing.
- A recorded stop stays first in `next` until its own stage is met after it, a
  later handoff, or `completion resolve --stop <rid> --reason <decision>`. An
  approval stop closes only through `resolve`; no test pass closes it. The
  resolve record is the coordinator's statement of the operator's decision; it
  grants no approval the card's human gates require.
- `--class approval|device|capability|conflict|dependency` exits 4 with the
  matching block prefix (`human-approval:`, `device:`, `capability:`,
  `checkout:`, `dependency:`).
- `completion block-check --reason R` exits 0 only for those prefixes (and
  `recovery-exhausted:` after a recorded exhaustion). Anything else (failing
  tests, a missing tool, a Docker error, a timeout) is refused as routine and
  counted as an avoidable block.
- Security gates are not weakened by this work: the approval rules
  (`approvals`, hardline floor, `command_allowlist`), the human gates in every
  card, protected branches and the fail-closed `dev_test_run` path. Not claimed:
  Tirith is not active on the official runtime (upstream removed it in
  911da42e9e; config migration v50 enables nothing in its place), and no other
  command scanner is asserted to be active. This removes avoidable
  interruptions, not safety controls.

### Ownership

Completion writes on a `--checkout` card need this run's live lease (renewed,
never taken). Another live owner, or one whose liveness can't be decided, is
refused and recorded as `owner-conflict`. A timed-out heartbeat or a delegate's
exit never makes the checkout free. The lease is cooperative same-uid evidence,
not OS containment, and `completion status` says so. Held (blocked) cards are
listed and never unblocked or promoted by the script.

### Resume

Each completion write also records a `resume` snapshot with the next unmet
criteria. `completion status <any card> [--repo-dir DIR]` recomputes stages,
`next`, the owner and held cards from durable records. A failed card comment is
re-synced from the ledger like every other workflow record.

### Capabilities

`completion capabilities --repo-dir DIR` matches allowlisted manifest names
(go.mod, package.json, pyproject.toml, Cargo.toml, Gradle, Xcode, Dockerfile,
compose files, migrations, GitHub workflows, toolchain pins) and reads
`package.json` script names. It executes nothing. It reports hint commands
(marked not executed) and the approved capabilities the task would need:
`dev_test_run` for database tests, an approved isolated runner for Docker (no
unrestricted Docker access), the Mac lane, the deferred Windows lane, GitHub
Actions read for the exact SHA. `.claude/bootstrap/analyze_repo.sh` is a Claude
Code bootstrap that runs tools and writes files, thus it isn't reused here.

### Metrics

`completion metrics --json` reads ledger records only. Every rate carries its
count, its denominator and an unknown count. No baseline is assumed. A recovery
attempt counts as a success only when its stage was met afterwards or the failed
check it followed passed later; a pass of another check is unknown, not success. Durations,
lost-work incidents and operator unblocks are not recorded and are reported as
unknown.

## Milestone plan (2026-11-04)

### A. Implemented and verified

- Central-writer workflow: intake, record-sha with a remote check, peer
  evidence per exact SHA, fix rounds, independent review, gate
  (`scripts/tests/test_kanban_workflow.py`).
- Designated-checkout lease, writer classification, provenance manifests
  (`scripts/tests/test_repo_lease.py`).
- This milestone: completion stages, source-bound evidence, bounded recovery,
  block classification, handoff/final, resume, owner refusal, ledger metrics,
  capability detection (`scripts/tests/test_completion.py`, synthetic board and
  local bare remote). Rollout allowlist includes `scripts/completion.py`.
- Patch manager fixture suite (`scripts/test_hermes_runtime_patch.sh`); its
  real manifest is archived legacy: every patch `unreviewed`, the base pinned
  as provenance only to the local customized commit `9ebe577798ea…` (not
  upstream), thus every real `apply` is refused (by design). None of 0001–0004
  applies to the official runtime 71736c9f and none is an install prerequisite
  (`hermes/runtime-patches/README.md`).
- Native board transport (native-v1, this milestone): `workflow.py` reads the
  board and prints native `kanban_create`/`kanban_comment`/`kanban_link` ops,
  read back before any result; records trusted by their role's runtime author;
  lease owner from the board's `worker_bound` managed-worker binding, failing
  closed on ambiguity (`docs/discord-kanban-workflow.md`, "Board transport").
  Verified by fakes and by the real upstream handlers on a throwaway home
  (`UpstreamCompatTest`); not exercised on the live board.

### B. Strictly necessary for safe, complete task execution

1. Review this milestone against the acceptance list below and commit it.
2. Install the skill files with `make hermes-dev-workflow-apply` (operator
   authorization; no restart needed for skill files).
3. No runtime patch is required: the workflow runs on the unpatched official
   runtime through the native board transport. Repository steps on a
   `--checkout` card need the board's task-scoped worker binding; where the
   runtime does not provide an unambiguous one (a shared owner process, a
   sibling bound to the same chain) they stop with exit 3 `capability:` while
   the board transport keeps working.
4. Database tests for example-app: `dev_test_run` was rejected in review and fails
   closed; the approved broker/netns design needs operator root provisioning.
   Until then a card that needs them stops with `capability:` (genuine).

### C. Deferred without weakening safety

- Progress-aware fix-loop ceiling (continue past `--max-rounds` while the lane
  failures change). The current limit stays and an operator unblock adds a round.
- Recording durations (implementation vs. infrastructure time) and operator
  unblocks in the ledger.
- Signed or sandbox-re-run evidence instead of coordinator self-assertion.
- Staging (single VM, Traefik, Cloudflare Tunnel/Access, private GHCR,
  immutable images), browser QA and staging migrations as completion stages.
- Windows lane repair; BrowserStack activation.
- Further patch-manager parser hardening (not part of this milestone).

### D. Remaining source, installation and validation steps

1. Bounded independent review of this diff (criteria below), fixes for
   blocking findings only, commit and push on the feature branch.
2. Operator: `make hermes-dev-workflow-plan`, then `make hermes-dev-workflow-apply`.
3. Patch manager: record the pinned base from an isolated upstream clone, real
   review entries, `make hermes-runtime-patch-apply SRC=<isolated>`, then an
   authorized install (docs/hermes-source-management.md).
4. Execution environment for example-app's Go/PostgreSQL tests (B.4).

### E. Earliest safe end-to-end task

A blocked code card with preserved work (example-app, with a Mac child card) once its execution
environment is approved (B.3, B.4) and its checkout's ownership is established
by `preflight`. The coordinator then runs `completion status`, records the
recovered criteria, finishes tests and fixes, reviews the full diff, commits,
pushes, runs `completion handoff` and completes the code card. The Mac child
then validates the exact SHA. A second, independent repository follows to prove
reuse. Until then no new implementation chain is created.

## Acceptance criteria for review of this milestone

1. One coordinator: no new profile, board tool or card chain; the records go
   through `put_record`; no direct DB access; no unblock/promote/block calls
   from the script.
2. Stages and handoff/final are computed by code from records and the source
   identity; a returning delegate never satisfies completion.
3. Evidence stores digests only and is rejected after any content change;
   handoff verifies the remote branch with `git ls-remote` at the exact SHA.
4. Identical recovery repeats are refused; routine recovery is bounded; only the
   listed prefixes can block. Only the stage's own progress resets the bound
   and closes a stop; an approval stop closes only by an explicit resolve.
5. A live or undecidable owner is never taken over, not on a timeout, not after
   a delegate exit; no OS containment is claimed.
6. Metrics come from ledger records with denominators and unknowns; an
   unrelated pass never counts as a recovery.
7. Capability detection executes nothing.
8. No security gate weakened (approvals, `dev_test_run` fail-closed, human
   gates, protected branches); Tirith is gone upstream and not claimed.
9. The commit stage (thus handoff) fails closed on assume-unchanged or
   skip-worktree index entries.

## Review results (2026-11-04)

Bounded review of this milestone (deleg_44c74426) found, and this diff fixes,
no scope added beyond them:

1. Any test pass reset the repeat refusal, the 8-attempt bound and open stops.
   Now only the stage's own progress does (see Recovery and blocks); approval
   stops need `completion resolve`.
2. assume-unchanged / skip-worktree entries let modified bytes look clean at
   the pushed SHA (false exact-SHA certification). The commit stage now fails
   closed while any is flagged.
3. Recovery success counted unrelated passes; now matched check or stage only,
   otherwise unknown.
4. The code card ran `completion status` before the fresh clone existed and
   completion writes before preflight's lease; the order is fixed. Scanner and
   missing-capability blocks now carry the `capability:` prefix block-check
   accepts.

Regressions: `scripts/tests/test_completion.py` (RecoveryTest,
SourceStateTest, CapabilityAndCliTest). The shell-identity change of the
installed runtime (71736c9f) is handled by the native board transport (above);
live readiness is not asserted (no live card has run it).

Broader Hermes regression suite: five failures on the candidate runtime source,
classified in an operator-side report (not shipped with the kit):
four introduced by the candidate ownership slice (`prior_owner_gone` strands
crashed cards; needs an attested drain path), one preexisting test-isolation
leak (provenance ContextVar). The installed 71736c9f has no such slice. The
suite is not green and is not claimed green.
