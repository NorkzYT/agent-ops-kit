# Development-test mode (`agent_ops_kit.dev_test` in Hermes config)

Selects the path a workflow request's database tests take. The setting is not a secret, so
it lives in Hermes' own `config.yaml` and is written with the supported `hermes config set`.
It does not go in `.env`, and the kit installer does not write it.

```bash
hermes config set agent_ops_kit.dev_test.require_dev_test_run false   # true | false
hermes config set agent_ops_kit.dev_test.backend compose              # compose | dev_test_run
hermes config unset agent_ops_kit.dev_test.backend                    # back to unset
```

`dev_test_mode.py config-plan --require false --backend compose` validates a pair and prints
the exact commands, starting with a `cp -p` backup of `config.yaml`. It runs nothing. Use it
to prepare an approved change. The live config is not touched by anything in this kit.

Two environment names, `AOK_REQUIRE_DEV_TEST_RUN` and `AOK_TEST_BACKEND`, are accepted as
aliases (exact case only; `aok_test_backend` is an error). There is no override order:

- Only config or only the environment is set: that source decides.
- Both are set and select the same mode: that mode.
- Both are set and disagree: error.
- Neither is set: the default.

| REQUIRE | BACKEND | Mode | Code card says |
|---|---|---|---|
| unset | unset | default | unchanged text: `dev_test_run` when listed, else the operator-authorized Compose path, else block |
| `true` | unset / `dev_test_run` | `dev_test_run` | only `dev_test_run`. No Compose path, even with an authorization comment. Without the tool, a capability block |
| `false` | `compose` | `compose` | never call `dev_test_run`, even when listed or named by a scanner `RECOVERY`. The setting authorizes the project's Compose dev services for tests, with the same inspection rules as the default text |
| unset | `compose` / `dev_test_run` | that backend | as above |
| `true` | `compose` | error | conflict |
| `false` | unset / `dev_test_run` | error | `false` needs a backend that still runs the tests |
| any other value | | error | |

An unreadable `agent_ops_kit` block is also an error: a flow mapping, a list, a duplicate or
unknown key. Every workflow command that reads the mode exits 3 on an error
(`capability: dev-test mode: …`), so the card blocks.

`config.yaml` is read on every workflow command, so no gateway restart is needed. Note that
`make hermes-install --force` (and `make hermes-config-apply`) re-render `config.yaml` from the
kit template and drop the `agent_ops_kit` keys; the mode returns to the default for new
requests until the operator sets it again. Existing cards keep the mode they recorded.

## Durable per-card mode

`intake` resolves the mode once, from `--dev-test-mode compose|dev_test_run|default` or else
from the settings. A CLI value that conflicts with an explicit setting is refused. Intake
writes `Dev-test-mode: <mode>` on the code, round and review cards. The default writes no
header, so a default card is byte-identical to one rendered before this feature. From then
on, the card is the authority:

- Later rounds read the mode from the header, even if they are rendered by a process that
  does not have the setting.
- A worker whose settings name a different explicit mode, or an invalid value, is refused
  (exit 3) by `preflight`, every `completion` command and next-round creation. `release` is
  never refused.
- A card without a mode keeps the default rules only on a worker whose settings name no
  mode. A worker set to a mode refuses it (exit 3): the default is never assumed. The
  operator either updates the request's cards (below) or unsets the setting.
- A round or review card whose `Dev-test-mode` differs from its code card's (a header line
  dropped or changed on either) is refused by `advance` and `verdict` (exit 3).
- Only the header counts. A comment, a record or a free-text "authorization" on the card
  never sets or changes a mode, whoever wrote it (on Validate the coder and the
  orchestrator are the same `default` profile).

## Moving an existing request to a mode (operator body edit)

A native-v1 request created before the setting (for example Validate's blocked
`t_623f313c` and its four sibling cards) moves to an explicit mode only by the operator's
own, supported `hermes kanban edit <id> --body`. Agents never apply it: they have no
body-edit tool and shell board writes are fenced. No comment, record or dashboard write is
involved. The approval is the operator's own, outside the board.

`dev-test-mode body-plan` prepares the exact edits and is read-only on the board (it only
runs `hermes kanban show`):

```bash
mkdir -m 700 <empty dir>
python3 <the workflow.py the cards run> dev-test-mode body-plan \
  <round-1 code card> --mode compose --out-dir <empty dir>
```

The new bodies embed the running script's own path, so the plan must run from the exact
`workflow.py` path the cards' `python3 <path> ...` commands already name (for cards made by
the installed skill, that installed path, with this change installed there). Run from any
other copy (a kit checkout, a scratch clone) it refuses before writing any plan file and
names the cards' path; there is no path override and it never rewrites a path.

For every card of the round whose body differs from what intake renders today in that mode,
it prints:

- `old_sha256` (the board body now) and `new_sha256`, with byte counts;
- `header_added`: `Dev-test-mode: <mode>` on the code and round cards, nothing on the
  validation and report cards (they carry no request header);
- `request_sha256`: the verbatim request, identical before and after;
- `removed_lines`: the old text the new body no longer carries (generator drift and the
  replaced database-test paragraph), for the operator to review;
- `new_body`, written to `<dir>/<card>.body` (0600, exclusive create; an identical rerun is
  accepted, other content is refused);
- `command`: `b="$(cat <file>; printf .)" && b="${b%.}" && hermes kanban edit <id> --body "$b"`
  (the `printf .` keeps the body's trailing newline, which `$(...)` would strip).

It also prints `plan_sha256` over (card, old, new), and `steps`: rerun the same command
(precheck: same `plan_sha256`), apply the commands in order (code card first), rerun
(postcheck: `state: applied`, no edits). `hermes kanban edit` has no compare-and-swap, so
the precheck must hold right before, with no card claimed.

The plan refuses when:

- the card is not a native-v1 round-1 code card, or not the request's current code card,
  or the request already has later-round cards (round > 1, review or device-stage cards);
- a card to edit is `running`, has a live run, or is not `todo`, `ready` or `blocked`;
- a card to edit has another title or assignee than intake plans, was not created by the
  orchestrator or reviewer profile, or carries another verbatim request;
- the new header would change anything but adding the `Dev-test-mode` line;
- a card already records another explicit mode, or this process's setting names another
  mode;
- a body cannot pass as one `--body` argument (NUL, more than 128 KiB).

After the edits the cards are byte-identical to today's intake render in that mode. A rerun
of `intake --bind <code card>` with `--dev-test-mode <mode>` or with the setting finds the
same five cards, emits no board op and creates nothing. Without the mode it is a mismatch
(exit 4), not a silent default. A later edit of any body is also a mismatch; a dropped or
changed header line on the round card is refused as above.

For the copied `t_623f313c` graph (compose): two edits, the code card (11 removed lines: the
old dev_test_run-only database paragraph and scanner rule, and the pre-adopt checkout line)
and the round card (header line only); both validation cards and the report card are
unchanged. The held code card stays `blocked`; unblocking it is a separate operator
decision.

## Database-test evidence

- `db-tests` is a check the coordinator selects in its criteria when the change touches
  database code, queries or migrations. A database directory in the checkout does not by
  itself make it required, so docs-only and unit-only changes hand off on their own checks.
- On a mode card, every `db-tests` record needs `--backend <the mode>`. A `--backend` on
  any other check must also equal the mode. The command text is not inspected.
- Valid compose commands include the project's `compose exec -T` / `run --rm --no-deps`,
  host `go test -p 1 ./...` with `-race` and `go vet`, and the repository's make target or
  script, each against the isolated Compose test database.
- The isolated test database is migrated and seeded through the project's migration service
  only: `compose --profile <profile> run --rm --no-deps <service>` with every connection
  setting it reads overridden explicitly for the test database (`-e`; never its env-file
  values), and the password only as a read-only mounted `PGPASSFILE` (`-v <0600
  file>:<path>:ro -e PGPASSFILE=<path>`). When the service cannot read that file, block;
  never put the password in the command. `make dev-deps` (and Validate's `db-setup`,
  `db-migrate`, `db-seed`) migrate and seed the main development database (on Validate
  `dev-deps` also force-recreates the services): never for this.
- Tests that skip because no test database reached them did not run. Count executed tests
  (`go test -v` or `-json`); a run whose database tests all skipped is not a pass.
- A passing `db-tests` record needs:
  - exit code `0` (an integer, not a boolean);
  - integer counts with `passed >= 1` and no failures;
  - the digest of a non-empty `--log` file.
  A failing record must also be well-formed.
- One function (`db_record_problem`) judges a record when `completion evidence` writes it
  and again at every read. A record written by hand, by an older script or under another
  mode is judged the same way. Malformed records (a boolean exit code, string or float
  counts, a non-dict) are rejected without an exception.

## What is enforced, and what is only instruction

`workflow.py` enforces these at runtime, in its own process:

- Resolution and header validation fail closed; a comment never sets a mode.
- Conflicts between the card and the worker's settings are refused.
- The `--backend` rule and the shared record validation above.
- With `db-tests` selected on a mode card, `completion handoff` requires a passing,
  well-formed record of the mode's backend on the current source.
- On a compose card, `completion block-check` refuses a reason that names `dev_test_run` as
  the missing piece. This is a classification aid, not a security check.
- The script never runs `dev_test_run`, docker or tests itself. Tests check this with
  stand-in executables on `PATH`.

Instruction only (card text the agent follows):

- Which tools the agent calls. The script does not intercept a `dev_test_run` tool call or
  a terminal `docker` command. Tool exposure belongs to the Hermes runtime, and the live
  runtime has no `dev_test_run` tool.
- Whether `db-tests` is selected for a change.
- The Compose safety rules. As with the default text, the host scanner checks only a few
  of them.
- Whether the tests ran. A record is the agent's report bound to the source state. Its log
  digest shows that a file with that content existed when it was recorded, not that the
  command ran or produced it.

None of this is OS isolation.

## What the tests prove

`scripts/tests/test_dev_test_mode.py` uses synthetic boards (`FakeHermes`), temporary git
repositories and a throwaway `HERMES_HOME`. The `hermes config set` case runs the real
Hermes CLI in that throwaway home. The inheritance case runs a launcher → worker → shell
→ `workflow.py` chain that stands in for the Hermes dispatcher; it is not the dispatcher.

`BodyPlanTest` covers the plan, its read-only behaviour, rediscovery after the edits,
ignored self-authorization comments and drift refusals on a synthetic board. The copied live
five-card graph was planned, edited and rediscovered on a disposable real Hermes board (an
isolated `HOME`/`HERMES_HOME`, no dispatcher, the real `hermes kanban create|edit|show` CLI);
see the commit message for the run.

Not proven: behaviour on the live gateway, a live worker's tool exposure, and the edit of the
live `t_623f313c` request. It has not been applied; that is the operator's decision.
