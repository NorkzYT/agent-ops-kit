# Hermes autonomous development on the Ubuntu dev VM: audit and repair

Scope: why Kanban code cards kept blocking (regression case `t_12595b96`,
checkout `/opt/repos/validate`, branch `feat/integration-recording-qa`, Mac child
`t_8e1c1f11`), what in the installed Hermes runtime and in this kit caused it,
what changed, how it was tested, and what remains gated on the operator.
Development environment only; nothing here touches production, secrets or the
live board.

## Authoritative files and versions

| Component | Path | Version / state at audit |
|-----------|------|--------------------------|
| Hermes runtime | `~/.hermes/hermes-agent` | v0.21.2 (2026.9.11), branch `feat/discord-channel-org`, base `9ebe5777`; repair on branch `feat/kanban-supervised-terminal` (`e3e1cd8c`, `2092fedd`, `b6f839c7`) |
| Live config | `~/.hermes/config.yaml` | `approvals:` absent (defaults deny), `command_allowlist: [script execution via -e/-c flag]`, `kanban.dispatch_in_gateway: true`, `max_in_progress: 2`, `terminal.cwd: /opt/repos` |
| Kit | `/opt/repos/claude-code-autopilot` (`NorkzYT/agent-ops-kit`) | branch `feat/default-terra-gpt-6.1` from `7edc0818` |
| Workflow skill | `hermes/skills/kanban-workflow/scripts/workflow.py`, new `repo_lease.py`, `SKILL.md` 2.2.0 | installed copy under `~/.hermes/skills/agent-ops-kit/kanban-workflow/` (refreshed by `make hermes-config-apply`) |
| Config template | `hermes/config.yaml.tmpl`, rendered by `scripts/hermes-install.sh` | now renders `approvals` (deny) and preserves `command_allowlist` on `--force` |
| Runtime patches | `hermes/runtime-patches/*.patch`, `scripts/hermes-runtime-patch.sh` | three patches, see README there |
| Approved CWD scanner | `/usr/local/libexec/hermes-validate-cwd-scan` (root, 0755), sudoers `NOPASSWD` exact, no arguments | fixed to `/opt/repos/validate`; exit 0 none / 1 matches / 2 incomplete; not modified |
| Approved test DB form | `PGPASSFILE=~/.config/hermes-test-db/pgpass MATCHING_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55443/matching_test?sslmode=disable` | passes the approval scanner as is (`hermes approvals test` verdict: allow) |

## Incident: what actually happened

The card's log and comments (read only) show four causes, in order of weight.

1. **The worker's own terminal had no task identity and was fenced.**
   `agent/delegation_context.delegated_child_subprocess_env()` scrubbed
   `HERMES_KANBAN_TASK/RUN_ID/CLAIM_LOCK` and set `HERMES_DELEGATED_CHILD_CONTEXT=1`
   whenever the *process* had `HERMES_KANBAN_TASK`, i.e. for every subprocess of a
   dispatcher worker including its own terminal (`tools/environments/local.py`
   `_make_run_env`, `process_registry._spawn_env`). Consequences: `$HERMES_KANBAN_TASK`
   expanded empty in card instructions; `workflow.py` refused with "must run in the
   card's own worker terminal, not inside delegate_task"; every `hermes kanban
   comment/create/link/notify-subscribe` from a worker was denied at the CLI and at
   `kanban_db.write_txn` (`_assert_not_delegated_child_mutation`) and opened the
   board read-only (`kanban_db_connect.connect`); the model, seeing the board's
   `worker_pid` (its own process) and no identity in its shell, concluded "a
   separate claimed coordinator run 369 is active (PID 3702260); this session has
   no task worker environment" and blocked. The upstream intent (a shell is not a
   board-write grant; test `tests/tools/test_kanban_descendant_scope.py`) is right
   and is preserved.
2. **The CWD scan treated every process in the checkout as a writer and failed on
   permission errors.** The card text asked for `find /proc/*/cwd -lname <checkout>`;
   root-owned processes gave EACCES, processes exiting mid-scan gave ENOENT, and an
   open VS Code (and later the coordinator's own shells) counted as competing
   writers. The operator installed the root-owned scanner; the workflow had no
   integration for it and no classification.
3. **Legitimate uncommitted work had no provenance.** The coding subagent's
   changes were "uncommitted per instructions"; the next run saw a dirty tree and
   the checkout rules said "clean tree or block". A manifest of hashes existed only
   as an ad-hoc file in `/tmp`.
4. **Headless approvals ignored the operator's standing decision; the stop guard
   pushed toward blocking.** The worker runs as `hermes chat -q`; in
   `tools/approval._unattended_deny` a dangerous-pattern hit is denied without
   consulting `command_allowlist`, so `python3 -c …` (pattern key already approved
   permanently) was blocked three times and the model tried `$(docker exec …
   printenv POSTGRES_PASSWORD)` instead, which tirith correctly flagged (reading a
   container secret). `agent/kanban_stop.py` counted a *refused* `kanban_complete`
   as terminal and its nudge said "finish or kanban_block".

What was fine and left alone: dispatcher claims, claim-lock heartbeats and stale
reclaim (`hermes_cli/kanban_db_dispatch.py`), run/retry accounting and
protocol-violation streaks, the Mac child graph release (completing the parent
releases the queued child), the Discord notifier/templates, the approved test DB
invocation (allowed by the scanner without any config change), the in-process
`kanban_*` tool gating for delegate children and cron (`tools/kanban_tools.py`,
`cron/scheduler.py` non-dispatcher context), and the hardline/tirith floors.

## Changes

### Hermes runtime (branch `feat/kanban-supervised-terminal`, three commits = `hermes/runtime-patches/000{1,2,3}`)

1. **Supervised worker terminal** (`agent/delegation_context.py`,
   `tools/environments/local.py`, `tools/process_registry.py`,
   `tools/environments/base_session_env.py`, `hermes_cli/kanban.py`,
   `hermes_cli/kanban_db.py`, `hermes_cli/kanban_db_connect.py`).
   `supervised_worker_terminal_env()` applies the existing fence and then, only when
   the current process is the dispatcher-owned worker (not a delegate child, not a
   cron run), hands back `HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`,
   `HERMES_KANBAN_SUPERVISED_TERMINAL=1` and `HERMES_KANBAN_SUPERVISED_WORKER_PID`
   from `os.environ` (never from the caller's env, never the claim lock). Only the
   terminal tool's foreground and background spawns use it; execute_code, MCP, LSP,
   codex, browser, TTS, skill hooks and cron keep the plain fence. The bash session
   snapshot never persists `HERMES_KANBAN_*` or the fence (they are decided per
   spawn), closing the path where a snapshot taken by the worker would hand its
   identity to a command issued inside a delegate child.
   `supervised_terminal_identity()` verifies marker, ids and that the worker pid is
   an ancestor. The CLI (`_delegated_child_cli_denial`) lets such a terminal run
   `comment`, `attach`, `heartbeat`, `notify-subscribe` on its own card or cards it
   created, `create` (tagged `creator_task_id`), and `link` when one endpoint is
   owned/created; the board must show the task `running` under that worker pid and
   run. Everything else (`complete`, `block`, `request-review`, `unblock`, `claim`,
   boards, …) and every foreign card stays denied. The DB fence opens only inside
   `kanban_db.supervised_terminal_mutation()`, which re-verifies the identity; the
   connection is then read-write without ever initializing or migrating.
2. **Headless approvals honour permanent pattern keys** (`tools/approval.py`):
   `_unattended_deny` skips the pattern block when the key is in the permanent
   `command_allowlist` (legacy aliases included); session-only approvals, the
   hardline floor and the tirith scan are unchanged. No `approve`/yolo anywhere.
3. **Stop guard** (`agent/kanban_stop.py`): a terminal `kanban_*` call counts only
   when its result is not an error; `kanban_request_review`/`kanban_request_changes`
   are terminal; the nudge tells the worker to continue (collect a running delegate
   child, retry transient failures, block only for human decisions, confirmed
   conflicts or unresolvable dependencies; a refused board call is not terminal).

### Kit

- **`repo_lease.py`** (stdlib, next to `workflow.py`): repository identity
  (canonical root, absolute git dir, origin, branch, HEAD); durable lease file
  under `~/.hermes/kanban/workflow-runs/leases/<sha256(gitdir)>.json` (0600 in a
  0700 dir, exclusive create via `link`, atomic replace, `O_NOFOLLOW`, symlink
  refusal) owned by board/task/run + anchor pid and its kernel start time;
  acquire/renew/recover/release with bounded recovery (dead or pid-reused owner
  only; an alive owner with a stale heartbeat is a conflict; another host is
  "not decidable"); scanner registry (`cwd-scanners.json`) accepting only
  `sudo -n /abs/helper` or `/abs/helper` that is root-owned and not group/world
  writable, exit-code contract 0/1/2, unprivileged `/proc` fallback that marks
  other users' processes as unknown; classification per match: `self-chain`,
  `own-descendant`, `git-mutation` (blocking), `agent-writer` (blocking; by
  executable name only when outside our chain), `interactive`, `service`, `gone`
  (ENOENT/ESRCH) and `unknown` (EACCES/EPERM or incomplete scan; blocking);
  manifests of the card's dirty paths (status, worktree sha256, index oid; no
  contents) with `checkpoint`, `adopt` (explicit `{path: sha256}` provenance that
  must match byte for byte) and `verify` (known / changed / unknown; a different
  branch, remote or git dir invalidates the manifest); `preflight` combining
  remote, `index.lock`, protected/wrong/diverged branch, other worktrees, lease,
  writers and dirty provenance into exit 0 / 2 / 3.
- **`workflow.py`**: `require_worker_context` accepts the supervised worker
  terminal and still refuses fenced descendants; `own_card` binds commands to
  `$HERMES_KANBAN_TASK`; new subcommands `preflight`, `checkpoint`, `adopt`,
  `release` driven by the code card header; designated-checkout card text replaced
  (`preflight` before editing, `checkpoint` after writes, `release` at the end,
  explicit exit-code handling, no manual `/proc` scan, no "block because dirty");
  step 6 says unfinished work is not a blocker; `record-sha` rejects untracked
  files and, in a designated checkout, re-runs lease + writer + clean checks right
  before writing the record. The existing `--checkout` and named feature-merge
  approval work is kept.
- **Installer/template**: `approvals: {cron_mode, single_query_mode,
  unattended_mode}: deny` rendered explicitly (documented keys);
  `command_allowlist` read from the live config and preserved across `--force`.
- **Runtime patch tracking**: `hermes/runtime-patches/` + `scripts/hermes-runtime-patch.sh
  status|check|apply` (working tree only, dirty targets refused, unrelated dirty
  files ignored, idempotent) + `make hermes-runtime-patch-status|apply`.
- **Prompts/docs**: `SKILL.md` rules (supervised terminal, no block for unfinished
  work, preflight/checkpoint/release, approved test-DB form only), coder profile
  `SOUL.md` team section, runbook `docs/discord-kanban-workflow.md` (recovery,
  setup, troubleshooting), this document.

## Threat boundaries

- **Cooperative scoping, not confinement.** Like the upstream fence, the
  supervised-terminal grant is enforced on env markers plus board verification and
  process ancestry. It defends against the real failure modes (worker identity
  lost, copied env in an unrelated process, stale pid after a reclaim, pid reuse,
  cron or delegate children inheriting ownership, snapshot leakage). It does not
  confine a same-uid adversary that forges the env or writes SQLite directly;
  neither did the previous fence.
- **Lifecycle stays in-process.** A shell can never complete, block, review,
  unblock or claim a card; those remain the worker's `kanban_*` tool calls, gated as
  before for delegate children and cron.
- **No approval bypass.** Defaults stay deny; the only change is that a decision
  the operator recorded permanently applies headlessly too. Hardline patterns and
  tirith findings are unaffected; `single_query_mode: approve`, yolo and scanner
  disabling are not used or recommended anywhere.
- **Nothing destructive, ever.** No reset/stash/clean/rm in the checkout; leases,
  locks and files are never removed to pass a check; an alive lease owner is never
  taken over; processes are never killed.
- **No secrets in state.** Leases and manifests hold ids, paths and hashes; the
  claim lock is never forwarded or persisted; the password file is referenced by
  path only.
- **Fail closed on uncertainty.** Unreadable `/proc`, an incomplete or failed
  scan, an undecidable lease owner, an unregistered scanner on a host with other
  users' processes, or dirty paths without provenance all stop the card with the
  exact detail.

## Tests (real runs)

Hermes (`~/.hermes/hermes-agent`, `venv/bin/python -m pytest -p no:cacheprovider`):

| Suite | Result |
|-------|--------|
| Focused baseline before changes (7 kanban/delegation/approval files) | 88 passed, 59 s |
| `tests/tools/test_kanban_supervised_terminal.py` (new: env surfaces, ancestry verification, CLI grant/denials incl. delegate child, stale pid, blocked card, snapshot leak) | 12 passed |
| `tests/tools/test_single_query_approval_mode.py` (+7), `tests/agent/test_kanban_stop.py` (+5), `tests/tools/test_snapshot_*` | 48 passed |
| Broad regression (kanban tools/CLI/db, delegation, approvals, environments, terminal, cron isolation) | 876 passed, 20 skipped, 1 failed: `test_terminal_yield_to_background::test_redirect_mid_command…` — flaky; passes alone twice on the patched tree and on a clean baseline worktree |

Kit (`/opt/repos/claude-code-autopilot`):

| Suite | Result |
|-------|--------|
| Baseline `make test-router` (all `scripts/tests`) before changes | 87 kanban-workflow tests OK; full discover OK (1 skipped), 7 min |
| `scripts/tests/test_repo_lease.py` (new: lease acquire/renew/release, alive vs dead owner, pid reuse, other host, symlink refusal, two real processes racing, classification incl. real parked `git commit`, scanner contract/registration/fallback, manifests + adopt, preflight git-state/dirty/scan/lease, CLI) | 23 OK (8 s) |
| `scripts/tests/test_kanban_workflow.py` (updated card text; new `WorkerContextTest`, `DesignatedCheckoutLeaseTest`: preflight/checkpoint/release round trip, adopt, record-sha untracked + lease + foreign agent revalidation, CLI) | 93 OK (3 min 56 s; was 87 baseline) |
| `scripts/test_hermes_runtime_patch.sh` | 24 passed |
| `scripts/test_hermes_install.sh` (+7: approvals deny, empty allowlist, `--force` keeps live entries) | 47 passed |
| `scripts/tests/test_makefile.py` | 22 OK |
| Live, read-only: real scanner via `repo_lease.run_scan` + `classify_scan` on `/opt/repos/validate` | complete, 510 process dirs, 0 matches, ok |

## Rollout

1. Review and push the kit branch; refresh the installed skill copies
   (`make hermes-config-apply`, which also renders `approvals` and keeps the live
   `command_allowlist`), copy `hermes/profiles/coder/SOUL.md` if that profile is
   used for workers.
2. `make hermes-runtime-patch-status` → all three `applied` on this host (the
   runtime branch already carries the commits) or `applicable` after a reinstall →
   `make hermes-runtime-patch-apply`.
3. Register the scanner once:
   `python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/repo_lease.py register-scanner /opt/repos/validate -- sudo -n /usr/local/libexec/hermes-validate-cwd-scan`.
4. For `t_12595b96`: bootstrap provenance of the preserved work from the
   authorized manifest, in the card's own worker terminal:
   `workflow.py adopt <code card> --repo-dir /opt/repos/validate --manifest /tmp/validate-t_12595b96-preserved-sha256.json`
   (refuses if any listed file changed since; unlisted dirty paths are reported and
   stay a conflict until recorded or decided). That card is not a kit workflow card
   (no `Workflow: code` header); either re-intake with `--checkout /opt/repos/validate
   --branch feat/integration-recording-qa --base 3752788a…` or run `repo_lease.py`
   directly with `--task t_12595b96`.
5. `make hermes-restart` from the operator's own terminal (not from inside Hermes).

Rollback: `git -C ~/.hermes/hermes-agent switch feat/discord-channel-org` (or
`git apply -R` each patch), `make hermes-config-apply` from the previous kit
commit, restart. Leases/manifests under `~/.hermes/kanban/workflow-runs/` are
metadata and can be deleted; nothing in any repository was changed by this work.

## Remaining gates and gaps

- Live activation (skill copy refresh, scanner registration, patch status,
  gateway restart) is the operator's/parent's step; nothing live was changed.
- The supervised-terminal grant is Linux-only (`/proc` ancestry); on the Mac
  worker it is simply never granted (unchanged behaviour there).
- `adopt` keys on worktree content hashes; renames and mode-only changes appear as
  unknown paths and need a `checkpoint` from the owning card.
- `agent-writer` classification is by executable name (`hermes`, `claude`,
  `codex`, `aider`, `gemini`, `cursor-agent`, `copilot`, `goose`, `opencode`) only
  when the process is outside our chain; an unknown agent binary with no tty is
  reported as `service`, not blocked. The lease, `index.lock` and git-mutation
  checks still catch the writes that matter.
- Upstream Hermes may change `_unattended_deny`, the CLI denial list or the
  terminal env path; `hermes-runtime-patch.sh status` reports `conflict` and the
  three test files in the patches are the contract to rebase against.
- `test_terminal_yield_to_background` flakiness is pre-existing and unrelated.
