# Hermes autonomous development on the Ubuntu dev VM: audit and repair

> **Superseded (2026-11-04).** This is the historical record of the repair on the old local
> runtime (`9ebe5777`, a local customized commit, not upstream). The installed runtime is now the
> official Hermes 71736c9f: the supervised-terminal patch 0001 and 0002–0004 do not apply to it and
> are archived, unreviewed and not an install prerequisite (`hermes/runtime-patches/README.md`).
> Tirith was removed upstream, so the Tirith settings below are not active. The workflow now uses
> the native board transport (`docs/discord-kanban-workflow.md`, "Board transport"); statements
> below about `$HERMES_KANBAN_TASK` in worker shells and CLI board writes describe the old runtime.

Scope: why Kanban code cards kept blocking (regression case `t_12595b96`,
checkout `/opt/repos/validate`, branch `feat/integration-recording-qa`, Mac child
`t_8e1c1f11`), what in the installed Hermes runtime and in this kit caused it,
what changed, how it was tested, and what remains gated on the operator.
Development environment only; nothing here touches production, secrets or the
live board.

## Authoritative files and versions

| Component | Path | Version / state at audit |
|-----------|------|--------------------------|
| Hermes runtime | `~/.hermes/hermes-agent` | v0.21.2 (2026.9.11), branch `feat/discord-channel-org`, base `9ebe5777`; first repair on branch `feat/kanban-supervised-terminal` (`e3e1cd8c`, `2092fedd`, `b6f839c7`, messages carry a `Co-Authored-By` trailer); the reviewed repair is the clean branch `feat/kanban-supervised-terminal-clean` (three commits from `9ebe5777`, same changes plus the review fixes, no trailers), the source of the kit's patches. The live checkout still sits on the first branch until the operator activates the clean one. |
| Live config | `~/.hermes/config.yaml` | `approvals:` absent (defaults deny), `command_allowlist: [script execution via -e/-c flag]`, `kanban.dispatch_in_gateway: true`, `max_in_progress: 2`, `terminal.cwd: /opt/repos` |
| Kit | `/opt/repos/claude-code-autopilot` (`NorkzYT/agent-ops-kit`) | branch `feat/default-terra-gpt-6.1` from `7edc0818` |
| Workflow skill | `hermes/skills/kanban-workflow/scripts/workflow.py`, new `repo_lease.py`, `SKILL.md` 2.2.1 | installed copy under `~/.hermes/skills/agent-ops-kit/kanban-workflow/` (refreshed by `make hermes-dev-workflow-apply`) |
| Config template | `hermes/config.yaml.tmpl`, rendered by `scripts/hermes-install.sh` | now renders `approvals` (deny), `security.tirith_enabled: true` + `tirith_fail_open: false` (the installed tirith 0.4.2 at `~/.hermes/bin/tirith` answers `check`; the live config leaves Hermes' fail-open default, so an absent scanner would let commands run unscanned), and preserves `command_allowlist` on `--force` |
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
   created, `create` (tagged `creator_task_id`), and `link` when both endpoints are
   owned/created, or when a card it created becomes a new parent of a card its own
   card already gates (the request's report card: `open_next_round`,
   `open_device_stage` and the review hand-off in `workflow.py` need exactly this
   shape); a foreign card's graph is never changed otherwise. The board must show
   the task `running` under that worker pid and run, the run id is mandatory and
   its `task_runs` row must be `running`. Everything else (`complete`, `block`,
   `request-review`, `unblock`, `claim`, boards, …) and every foreign card stays
   denied. The DB fence opens only inside `kanban_db.supervised_terminal_mutation()`,
   which records a `SupervisedTerminalGrant` (task, run, worker pid, 120 s expiry);
   `write_txn` re-verifies that grant against the task and run rows inside every
   write transaction, so a task that was blocked, reclaimed or re-run between the
   CLI precheck and the write is denied at the write, and the grant never covers
   board metadata. The connection is read-write without ever initializing or
   migrating.
2. **Headless approvals honour permanent pattern keys** (`tools/approval.py`):
   `_unattended_deny` skips the pattern block when the key is in the permanent
   `command_allowlist` (legacy aliases included); session-only approvals, the
   hardline floor and the tirith scan are unchanged. No `approve`/yolo anywhere.
3. **Stop guard** (`agent/kanban_stop.py`): a terminal `kanban_*` call counts only
   when its result arrived and is not an error (`{"error"}`, `{"ok": false}`,
   `{"success": false}`, `Error:` text; a call with no result is pending, not
   terminal, since it changed nothing on the board); `kanban_request_review`/
   `kanban_request_changes` are terminal; the nudge tells the worker to continue
   (collect a running delegate child, retry transient failures, block only for
   human decisions, confirmed conflicts or unresolvable dependencies; a refused
   board call is not terminal) and to finish a card that already has its review or
   validation child with `kanban_complete`, never a second review request.

### Kit

- **`repo_lease.py`** (stdlib, next to `workflow.py`): repository identity
  (canonical root, absolute git dir, origin, branch, HEAD); durable lease file
  under `~/.hermes/kanban/workflow-runs/leases/<sha256(gitdir)>.json` (0600 in a
  0700 dir, exclusive create via `link`, atomic replace, `O_NOFOLLOW`, symlink
  refusal) owned by board/task/run + anchor pid and its kernel start time;
  acquire/renew/recover/release serialized by a per-repository `flock` on a
  private, symlink-refusing lock file whose inode is re-checked after locking
  (concurrent recovery of a dead owner's lease has exactly one winner; renew and
  release read and write under the same lock), bounded recovery (dead or
  pid-reused owner only; an alive owner with a stale heartbeat is a conflict;
  another host is "not decidable"); `same_owner` compares task, board, host, pid
  and pid start time, never the run id alone; the anchor (`anchor_pid`) is the
  supervising worker only when it is an ancestor whose own environment carries
  `HERMES_KANBAN_TASK=<task>`, else this process (a shared `hermes gateway`
  ancestor can never make foreign workers "own descendants"), and the self chain
  ends at that anchor; scanner registry (`cwd-scanners.json`) accepting only
  `sudo -n /abs/helper` or `/abs/helper` that is root-owned and not group/world
  writable, exit-code contract 0/1/2, unprivileged `/proc` fallback that marks
  other users' processes as unknown; classification per match: `self-chain`,
  `own-descendant`, `gone` (ENOENT/ESRCH), then one of two regimes chosen by the
  observer policy (`observer_grant`: a valid, owner-protected policy for this host,
  uid and repository).
  **Ungranted (default, the pre-PR28 d468701 rules):** `git-readonly` (`READONLY_GIT`
  subcommands), `editor`, `language-server`, `dev-server` and an idle `shell` are passive,
  but each claim is checked against `/proc/<pid>/exe` (same image name, packaging
  suffixes/aliases allowed, user installs accepted; an interpreter command line must run on
  that interpreter; a node-hosted tool's script must resolve inside `node_modules`), so
  `exec -a vite <writer>`, a symlink named like the tool and `node /tmp/vite.js` are
  `unknown`. Read forms that write or start an invoker-chosen program are `unknown`:
  `--output` (any abbreviation) on any read, `grep -O`/`--open-files-in-pager`,
  `fsck --lost-found`, `reflog expire|delete|drop`, symbolic-ref update/delete,
  `ls-remote --upload-pack`, `--exec-path=`, `--config-env`, and `-c` (or
  `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_COUNT` in the process environment) other than the
  display keys in `UNGRANTED_GIT_CONFIG_KEYS` (so `-c core.pager=...`, `-c diff.external=...`
  block; `-c core.quotepath=false -c color.ui=false status` does not). Dev servers pass
  only in serve/watch mode: `vite|next|nuxt|astro|parcel build` (and the other
  `DEV_SERVER_WRITE_COMMANDS`), `tsc`/`rollup`/`swc`/`esbuild`/`webpack` without a
  watch/serve flag (`tsc --noEmit` passes) and `turbo` tasks other than dev/start/serve
  are one-shot builds and block. Shells with arguments (the VS Code integrated terminal's
  `bash --init-file ...` included) stay `unknown`.
  **Granted (PR28 strict):** the explicit passive observations are only
  `editor` (root-owned image in root-owned directories), `language-server` (only a /proc-verified
  VS Code bundled server), `git-readonly` (only the explicit `GIT_SAFE_READS` forms, stdout not a
  terminal) and the /proc-verified `trusted-terminal`; language servers by name, dev
  servers/build tools and interpreter scripts are `unknown`; any other interactive shell is
  `unknown`: traps, `PROMPT_COMMAND` or `read -t` then a write look like a prompt to any
  `/proc` sample, so a shell whose CWD is the checkout blocks until it is closed or
  leaves the checkout. In both regimes agents, git mutations and everything unlisted block;
  a registered helper must be bound to the checkout by its own root-owned `REPOSITORY = "/abs"`
  declaration, else its PASS is incomplete), and the
  blocking kinds `git-mutation`, `agent-writer` (by executable name only when
  outside our chain) and `unknown` (any other executable or script, including
  arbitrary python/node tasks and tty-attached tools, EACCES/EPERM, incomplete
  scan); manifests of the card's dirty paths with the full identity of each
  change (status, worktree sha256 and mode, index object and mode, rename origin;
  no contents; paths literal, a dotfile stays a dotfile) with `adopt` (explicit
  `{path: sha256}` provenance that must match byte for byte) and `verify` (known /
  changed with the differing fields / unknown; a different branch, remote, root
  or git dir invalidates the manifest, but HEAD does not: a commit of some
  recorded paths leaves the others resumable, and a foreign `git add`, chmod or
  symlink swap on a recorded path is `changed`); only the checkpoint baseline is
  bound to HEAD; the gated `checkpoint`: this run
  must hold the live lease (renewed, never taken), the writer scan must be clean,
  and a baseline written by a passing `preflight` under the same lease tenure at
  the same HEAD must exist; it adopts the baseline's (task-owned) entries plus what
  changed or appeared since, reports that evidence (`kept`,
  `changed_since_baseline`, `new_since_baseline`, `dropped`, scanner, lease
  tenure) and refreshes the baseline. A dirty path that was never the task's
  cannot pass `preflight`, gets no baseline and is never laundered by
  `checkpoint`; `adopt` drops the baseline (next checkpoint needs a passing
  preflight). Limit, stated in the output: same-uid cooperative evidence that the
  lease was held and no competing writer was observed, not proof of who wrote the
  bytes. `preflight` combines remote, `index.lock`, protected/wrong/diverged
  branch, other worktrees, lease, writers and dirty provenance into exit 0 / 2 / 3.
- **`workflow.py`**: `require_worker_context` accepts the supervised worker
  terminal and still refuses fenced descendants; `own_card` binds commands to
  `$HERMES_KANBAN_TASK`; new subcommands `preflight`, `checkpoint`, `adopt`,
  `release` driven by the code card header; designated-checkout card text replaced
  (`preflight` before editing, `checkpoint` after writes, `release` at the end,
  explicit exit-code handling, no manual `/proc` scan, no "block because dirty");
  step 6 says unfinished work is not a blocker; `record-sha` rejects untracked
  files and, in a designated checkout, re-runs lease + writer + clean checks right
  before writing the record. The existing `--checkout` and named feature-merge
  approval work is kept unchanged (a feature-to-feature merge still needs the
  operator's exact, named approval; nothing here merged). `preflight` and
  `checkpoint` (both `workflow.py` and the `repo_lease.py` CLI without
  `--owner-pid`) refuse up front when no verified supervising worker is in the
  ancestry, instead of taking a lease owned by the short-lived CLI process that
  the next checkpoint could not renew; the library APIs with an explicit owner
  are unchanged.
- **Installer/template**: `approvals: {cron_mode, single_query_mode,
  unattended_mode}: deny` rendered explicitly (documented keys);
  `command_allowlist` read from the live config and preserved across `--force`;
  `security: {tirith_enabled: true, tirith_fail_open: false}` so a missing,
  crashed or timed-out scanner refuses the command instead of running it unscanned
  (Hermes' default is fail-open; `tools/tirith_security.py` reads both keys). No
  bypass or yolo anywhere. The approved test-DB command form is unchanged and was
  only re-read, not executed.
- **Runtime patch tracking**: `hermes/runtime-patches/` + `scripts/hermes-runtime-patch.sh
  status|check|apply` (working tree only, dirty targets refused, unrelated dirty
  files ignored, idempotent) + `make hermes-runtime-patch-status|apply`.
- **Prompts/docs**: `SKILL.md` rules (supervised terminal, no block for unfinished
  work, preflight/checkpoint/release, approved test-DB form only), coder profile
  `SOUL.md` team section; the coder `SOUL.md` no longer says "one bounded retry"
  or "no network calls that change external state": verification is diagnosed and
  fixed until it passes within the task's budget (an unchanged command is retried
  once, only for a transient failure), pushing the task's own feature branch is
  authorized, merges/deploys/secrets/production are not; the code card text and
  the stop nudge say the same and honour a pre-created review or validation child
  (complete releases it; no duplicate review request on the same card); runbook
  `docs/discord-kanban-workflow.md` (recovery, setup, troubleshooting), this
  document.

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
  the operator recorded permanently applies headlessly too. Implication: answering
  "Always" to an interactive approval prompt adds that pattern key to
  `command_allowlist`, and from then on headless (unattended) runs may run
  commands matching it without asking; use "Once"/"Session" for anything that
  should stay interactive, and prune `command_allowlist` to revoke. Hardline patterns and
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

## Review fixes (second pass)

Blockers found in review of the first pass and how they were closed:

| Finding | Fix | Regression |
|---------|-----|------------|
| `classify_process` called every unknown background process `service` and every tty-attached process `interactive` (non-blocking) | explicit passive name sets only; everything else `unknown` and blocking | `test_only_explicit_passive_tools_are_non_blocking`, preflight exit 3 on a plain `sleep` |
| anchor fell back to the nearest `hermes` ancestor (the gateway), so foreign workers read as own descendants | anchor must be an ancestor whose environ carries the task; else self; self chain ends at the anchor | `test_anchor_requires_a_verified_worker_identity`, `test_self_chain_stops_at_the_anchor…` |
| stale-lease recovery read/rename race; renew/release unlocked; `same_owner` trusted the run id alone | per-repo `flock` with inode re-check around acquire/renew/release/checkpoint; owner = task+board+host+pid+pid_start | `test_concurrent_stale_recovery_has_exactly_one_winner` (4 real processes), `test_same_owner_needs_the_process_identity…` |
| `verify_dirty` compared the worktree hash only; `lstrip("./")` mangled dotfiles | full entry identity (status, content, mode, index oid/mode, origin), HEAD/root binding, literal paths | `test_foreign_stage_mode_and_symlink_changes…`, `…keeps_paths_literal` |
| `checkpoint` adopted any dirty path with no gate | lease + writer scan + preflight baseline gate; changed-path evidence; adopt drops the baseline | `test_checkpoint_is_gated_and_never_launders_foreign_dirty_files` |
| coder SOUL: "one retry then report", "no network state changes" | diagnose/fix within budget, bounded retry for transient only, authorized branch pushes | prompt/SOUL assertions in `test_kanban_workflow.py` |
| tirith installed but fail-open by default | template renders `security.tirith_fail_open: false` | `test_hermes_install.sh` |
| CLI `link` with one endpoint in scope could rewire a foreign card's graph | both endpoints in scope, or created parent of a card the own card already gates | `test_worker_terminal_records_on_its_own_card_and_builds_its_cards_only` |
| grant checked the env marker only, not the board at write time; run id optional | `SupervisedTerminalGrant` re-verified in `write_txn` (status, pid, run, run row, expiry); run id mandatory | `test_grant_is_reverified_at_the_write_boundary`, missing/ended run cases in the CLI test |
| stop guard counted a terminal call with no result as success | pending is not terminal; `ok: false`/`success: false` are errors | `test_pending_terminal_call_without_result_does_not_count`, `test_error_markers_are_not_terminal` |
| runtime commits carried `Co-Authored-By` | clean branch `feat/kanban-supervised-terminal-clean` rebuilt from `9ebe5777`, original refs untouched, patches regenerated from it | `hermes-runtime-patch.sh status --src <clean worktree>`: 3 × applied |

## Rollout

1. Review and push the kit branch; refresh the installed skill copies and the
   coder SOUL with the narrow helper (not `make hermes-config-apply`, which
   re-renders `config.yaml`, `SOUL.md` and `honcho.json` from `.env`):
   ```bash
   make hermes-dev-workflow-plan    # paths, sha256, create/update/unchanged; writes nothing
   make hermes-dev-workflow-apply   # = python3 scripts/apply-dev-workflow.py --apply
   ```
   It copies only `hermes/skills/kanban-workflow/**` (no bytecode) into
   `~/.hermes/skills/agent-ops-kit/kanban-workflow/` and
   `hermes/profiles/coder/SOUL.md` into `~/.hermes/profiles/coder/` when that
   profile exists (`CODER_HOME=<dir below ~/.hermes>` to name another). Each
   replaced file gets a `<name>.bak-<timestamp>-<random>` copy and an atomic
   same-directory rename; files not in the source list are left in place. It
   refuses (and writes nothing) on a symlink, special file, foreign owner or
   world-writable directory/file anywhere from `~/.hermes` down. It never reads
   or writes `config.yaml`, `.env`, `honcho.json`, plugins, memories or the
   default `SOUL.md`, and restarts nothing.

   The security/approval keys are a separate, explicit step through the
   supported CLI (`hermes config get <key>` first records the old value); the
   live `command_allowlist` is not touched:
   ```bash
   hermes config set approvals.cron_mode deny
   hermes config set approvals.single_query_mode deny
   hermes config set approvals.unattended_mode deny
   hermes config set security.tirith_enabled true
   hermes config set security.tirith_fail_open false
   ```
   For a separate coder profile, repeat with `hermes -p coder config set …`.
2. Switch the runtime to the clean branch from the operator's own terminal:
   `git -C ~/.hermes/hermes-agent switch feat/kanban-supervised-terminal-clean`
   (the dirty, unrelated `agent/memory_manager.py` carries over untouched; the
   first branch stays as a ref). Then `make hermes-runtime-patch-status` → all
   three `applied` (on the old branch 0001 and 0003 report `conflict`, which is
   the expected answer until the switch), or `applicable` after a reinstall → build and
   review in an isolated checkout (`make hermes-runtime-patch-apply SRC=<isolated>`),
   then deploy only with authorization (docs/hermes-source-management.md).
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
`git apply -R` each patch), restore the `*.bak-<timestamp>-*` copies the helper
reported (or `make hermes-dev-workflow-apply` from the previous kit commit),
`hermes config set security.tirith_fail_open true` if needed, restart. Leases/manifests under `~/.hermes/kanban/workflow-runs/` are
metadata and can be deleted; nothing in any repository was changed by this work.

## Remaining gates and gaps

- Live activation (skill copy refresh, scanner registration, patch status,
  gateway restart) is the operator's/parent's step; nothing live was changed.
- The supervised-terminal grant is Linux-only (`/proc` ancestry); on the Mac
  worker it is simply never granted (unchanged behaviour there).
- `adopt` keys on worktree content hashes (the operator's manifest form); the
  adopted entries then carry the full identity, so later mode/stage changes are
  detected. A rename appears as its new path with `origin` recorded.
- Passive classification under a grant: editors by name only with a root-owned image in
  root-owned directories (a user copy of an interpreter named `gopls`/`vim.basic` blocks);
  language servers only as /proc-verified VS Code bundled servers (gopls, pyright, ruff
  server, tsserver... block); dev servers, build tools and interpreter scripts block; foreign
  git only in `GIT_SAFE_READS` forms (exact option tokens, so `--open-fil`/`--del`/`--lost`
  abbreviations block; status/diff/log/show/grep block because they can run repository
  filters, fsmonitor, textconv, external diff or a pager) with stdout not a terminal.
  Residual: a safe read in a partial clone can lazily fetch missing objects (transport,
  writes into the git dir). Shells other than the verified trusted terminal never pass.
- Passive classification without a grant (default): the pre-PR28 names with the image,
  write-form and build-mode checks above. Accepted residuals of that baseline: the image
  check is by name, not ownership (a user copy of an interpreter renamed `gopls` passes, as
  before PR28); a python-hosted language server's script location is not checked; reads
  still run repository-configured filters, textconv, external diff and pagers (repository
  config, not the command line; a pager child in the checkout is itself scanned);
  `GIT_PAGER`/`GIT_EXTERNAL_DIFF` in a read's environment are not inspected; `nodemon`,
  `air` and `turbo dev` run project commands by design; idle shells pass. Other users'
  processes block (their `/proc/<pid>/exe` is unreadable).
- Scanner root binding: the approved helper prints no root, so a PASS is accepted only when
  the helper's root-owned source declares `REPOSITORY = "<this checkout>"`. The live helper
  is bound to `/opt/repos/validate`; any other checkout needs a root-installed helper
  bound to it (not provided here).
- Own git reads: without a grant `status` runs as git does (repository filters normalize
  as before PR28). Under a grant `status` runs with every configured
  `filter.<driver>.clean/process` overridden empty (a `required` filter makes it fail; a
  driver name containing `=` fails closed) and fails as `unsupported` when a configured
  driver is the `filter` attribute of a tracked regular file (symlinks and gitlinks never
  run a filter and do not count): disabling it would report normalized files as changed,
  running it runs repository code. Not covered: filter drivers configured only inside a
  submodule's own repository (git recurses into submodules with their config). Same-uid
  cooperative evidence throughout: lease, scan and checkpoint prove what was observed, not
  who wrote.
- `checkpoint` after a commit needs a fresh passing `preflight` (the baseline is
  bound to HEAD; manifests are bound to root, git dir, remote and branch only, so
  a partial commit keeps the remaining paths resumable); the card text says so.
- Upstream Hermes may change `_unattended_deny`, the CLI denial list or the
  terminal env path; `hermes-runtime-patch.sh status` reports `conflict` and the
  three test files in the patches are the contract to rebase against.
- `test_terminal_yield_to_background` flakiness is pre-existing and unrelated.

## Follow-up: scanner rejection of the approved PostgreSQL tests (run 370)

Run 370 adopted its work and then blocked on a Tirith `warn` (`raw_ip_url` +
`schemeless_to_sink`, MEDIUM) that the single-query deny mode turns into a block, for
the operator-approved `PGPASSFILE=… MATCHING_TEST_DATABASE_URL=postgresql://…@127.0.0.1:55443/…
go test` command. Repair: runtime patch 0004 (`dev_test_run`, recovery directive,
`kanban_block` recovery refusal, credential and production-database guards) and the
root-owned, digest-pinned runner with its Landlock/seccomp sandbox. Root cause, design,
threat limits, install and smoke: [dev-test-runner.md](dev-test-runner.md). **An independent
review rejected that candidate's same-uid isolation for install**; it now fails closed
(runner, installer, runtime tool; all carried in the combined patch 0004 = `git diff
4f845b43ec..61cbead82d`, there is no patch 0005) and `t_12595b96` stays blocked until a revision
of the *proposed* (not approved, not implemented) credential-broker/network-namespace design in
that page is approved by the operator, implemented, provisioned and attested. A registry that
merely declares the proposed mode is not readiness.
