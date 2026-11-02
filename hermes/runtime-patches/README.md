# Runtime patches to the installed Hermes

The kit configures Hermes through documented keys, skills and prompts. Three
behaviours it depends on live in the Hermes runtime itself and are not
configurable; they are carried here as git patches against the installed
checkout (`~/.hermes/hermes-agent`, Hermes v0.21.2, local branch
`feat/discord-channel-org`, base commit `9ebe5777`) so a reinstall or upgrade
can re-apply them and `make hermes-runtime-patch-status` can tell whether the
live runtime still has them.

| Patch | What it changes | Why the kit needs it |
|-------|-----------------|----------------------|
| `0001-feat-kanban-hand-the-worker-s-own-terminal-its-task-.patch` | The dispatcher-owned worker's own terminal keeps its task identity (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_SUPERVISED_TERMINAL`, worker pid; never the claim lock). The CLI lets that terminal comment/attach/heartbeat/notify-subscribe its own card, create cards and link cards it owns or created, after the board confirms the task runs under that worker pid and run. Delegate children, cron runs and every other spawn surface stay fenced; lifecycle transitions stay in-process tool calls. Kanban identity never persists in the bash session snapshot. | `workflow.py` runs in the worker's terminal and must know its card (`$HERMES_KANBAN_TASK`) and record `code-sha`, evidence and round cards on it. Without this every `hermes kanban comment/create/link` from a worker is refused and the worker mistakes its own pid for a competing run (incident `t_12595b96`, run 369). |
| `0002-fix-approvals-honour-permanent-command_allowlist-pat.patch` | Headless runs (`hermes chat -q`, cron, unattended platforms) honour the operator's permanent `command_allowlist` pattern keys ("Always" answers). Session-only approvals, the hardline floor and the tirith scan are unchanged. | Kanban workers are `-q` sessions; the live config already holds `script execution via -e/-c flag`, yet workers were denied `python3 -c` (incident runs 367–368). |
| `0003-fix-kanban-stop-guard-counts-only-successful-termina.patch` | The worker turn-end guard counts a terminal `kanban_*` call only when its result is not an error, includes `kanban_request_review`/`kanban_request_changes`, and tells the worker to continue (collect a running delegate child, retry transient failures) instead of blocking for unfinished work. | A refused `kanban_complete` used to end the turn as a protocol violation; the old nudge pushed workers to block on incomplete work. |

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
`tests/tools/test_kanban_supervised_terminal.py`, `tests/agent/test_kanban_stop.py`
and `tests/tools/test_single_query_approval_mode.py` in the patch set are the
contract to keep green).

Regenerate after changing the runtime branch:

```bash
git -C ~/.hermes/hermes-agent format-patch -3 --no-signature --zero-commit --no-numbered -o hermes/runtime-patches <base>..HEAD
```

Tests: `make test-hermes-runtime-patch`.
