# Runtime patches to the installed Hermes

The kit configures Hermes through documented keys, skills and prompts. Four
behaviours it depends on live in the Hermes runtime itself and are not
configurable; they are carried here as git patches against the installed
checkout (`~/.hermes/hermes-agent`, Hermes v0.21.2, base commit `9ebe5777`; 0001–0003 are the
three commits of the clean repair branch `feat/kanban-supervised-terminal-clean`, 0004 is the combined diff of the
fourth and fifth commits of `feat/kanban-autonomous-reviewed` (d15a70ddc8, f04d4adc80), which carries the same three before them;
no trailers) so a reinstall or upgrade can re-apply them and
`make hermes-runtime-patch-status` can tell whether the live runtime still has them.

| Patch | What it changes | Why the kit needs it |
|-------|-----------------|----------------------|
| `0001-feat-kanban-hand-the-worker-s-own-terminal-its-task-.patch` | The dispatcher-owned worker's own terminal keeps its task identity (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_SUPERVISED_TERMINAL`, worker pid; never the claim lock). The CLI lets that terminal comment/attach/heartbeat/notify-subscribe its own card, create cards, and link cards when both endpoints are its own or created by it (plus one shape the workflow needs: a card it created as a new parent of a card its own card already gates, the report card). The grant (task, mandatory run id, worker pid, short expiry) is re-verified inside every DB write transaction: task `running` under that pid and run, run row alive, grant unexpired; it never covers board metadata. Delegate children, cron runs and every other spawn surface stay fenced; lifecycle transitions stay in-process tool calls. Kanban identity never persists in the bash session snapshot. | `workflow.py` runs in the worker's terminal and must know its card (`$HERMES_KANBAN_TASK`) and record `code-sha`, evidence and round cards on it. Without this every `hermes kanban comment/create/link` from a worker is refused and the worker mistakes its own pid for a competing run (incident `t_12595b96`, run 369). |
| `0002-fix-approvals-honour-permanent-command_allowlist-pat.patch` | Headless runs (`hermes chat -q`, cron, unattended platforms) honour the operator's permanent `command_allowlist` pattern keys ("Always" answers). Session-only approvals, the hardline floor and the tirith scan are unchanged. | Kanban workers are `-q` sessions; the live config already holds `script execution via -e/-c flag`, yet workers were denied `python3 -c` (incident runs 367–368). |
| `0003-fix-kanban-stop-guard-counts-only-successful-termina.patch` | The worker turn-end guard counts a terminal `kanban_*` call only when its result arrived and is not an error (`{"error"}`, `{"ok": false}`, `{"success": false}`, `Error:` text; a call with no result is pending, not terminal), includes `kanban_request_review`/`kanban_request_changes`, and tells the worker to continue (collect a running delegate child, retry transient failures) instead of blocking for unfinished work; a card that already has its review/validation child is finished with `kanban_complete`, never a duplicate review request. | A refused `kanban_complete` used to end the turn as a protocol violation; the old nudge pushed workers to block on incomplete work. |

| `0004-feat-kanban-typed-dev_test_run-tool-scanner-recovery.patch` (runtime commits d15a70ddc8 + f04d4adc80) | New `dev_test_run` tool (kanban toolset, dispatcher-owned workers only): typed arguments, root-owned registry + runner re-verified (owner, mode, symlink, pinned SHA-256) on every call, the fixed invocation scanned by the normal guards, empty environment; the runner binds to the live claim and repository lease and sandboxes the tests. **Fails closed on isolation**: the registry must carry per-profile `isolation.mode`; only `credential-broker-netns` is approved (not implemented yet), so for the rejected `same-uid-landlock` design the tool is hidden, refuses to run and emits no `RECOVERY:` directive (workers block naming the missing capability). With an approved profile, an unattended rejection carries `RECOVERY:` and `kanban_block` refuses until it was tried; recovery state is per (task, run) and cleared only by an actual runner start or a recorded precondition refusal. Additive guards: registered credential file / test container reads blocked in every mode (and in file tools); non-loopback database targets (URL host, `?host=`, multi-host, `hostaddr=`, `PGHOST`, `PGHOSTADDR`, `PGSERVICE*`) and URL passwords refused in unattended deny mode (text guard, not a boundary). | Workers are `-q` sessions; Tirith warns (`raw_ip_url`, `schemeless_to_sink`) on the approved `PGPASSFILE=… *_DATABASE_URL=postgresql://…@127.0.0.1:55443/… go test` command and the deny mode blocked the card (incident `t_12595b96`, run 370). See `docs/dev-test-runner.md`. |

## Commands

```bash
make hermes-runtime-patch-status      # applied | applicable | conflict, per patch
make hermes-runtime-patch-apply       # apply the applicable ones (working tree only), then: make hermes-restart
scripts/hermes-runtime-patch.sh check --src /path/to/hermes-agent   # exit 0 iff nothing conflicts
```

`apply` touches only the files a patch names and refuses when one of them is
dirty in the runtime checkout; an unrelated dirty file never blocks it. Nothing
is committed, reset or restarted. After `hermes update`, run `status`: an
upstream that already contains a change reports it `applied`; a `conflict` means
the patch must be rebased by hand on the new source (the tests under
`tests/tools/test_kanban_supervised_terminal.py`, `tests/agent/test_kanban_stop.py`,
`tests/tools/test_single_query_approval_mode.py` and `tests/tools/test_dev_test_tool.py` in the
patch set are the contract to keep green). 0004 needs a root-owned runner and registry with an approved `isolation.mode`
before `dev_test_run` appears; the installer currently refuses (`REJECTED`), so the tool stays
hidden and no recovery directive is emitted.

Regenerate after changing the runtime branch:

```bash
git -C ~/.hermes/hermes-agent format-patch -3 --no-signature --zero-commit --no-numbered -o "$PWD/hermes/runtime-patches" <base>..<third>
# 0004 is one combined patch (format-patch header + `git diff <third> HEAD`) so status can classify it on its own
# commit messages carry no trailers (the kit's commit-msg hook rejects Co-Authored-By; the runtime commits follow the same rule)
```

Tests: `make test-hermes-runtime-patch`.
