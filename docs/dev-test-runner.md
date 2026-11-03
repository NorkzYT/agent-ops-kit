# Trusted Go/PostgreSQL dev tests for Kanban workers (`dev_test_run`)

Kanban coding workers run as single-query (`-q`) sessions. In that context the unattended
approval gate denies every Tirith `warn`, and nobody can approve one. That is intended and
stays as is. This page describes the narrow, typed path that lets a worker run the
operator-registered Go/PostgreSQL tests anyway, without a broad allowlist, credential exposure
or a weaker scanner.

## Incident (t_12595b96, run 370)

- Rejected command (no secret in it; the password lives in the file):
  `PGPASSFILE=/home/polymath-dev/.config/hermes-test-db/pgpass MATCHING_TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:55443/matching_test?sslmode=disable' go test -race -p 1 -count=1 ./... && go vet ./...`
  in `/opt/repos/validate/apps/matching-service-go`.
- Layer: Tirith (`tirith check --json --non-interactive --shell posix`), action `warn`,
  findings `raw_ip_url` and `schemeless_to_sink`, both MEDIUM (raw IP evidence `0.0.0.1`).
  The warn reached `_unattended_deny` for the single-query context
  (`approvals.single_query_mode` default `deny`), which blocks a warn exactly like a block.
  Dangerous-pattern detection did not fire. `security.tirith_fail_open: false` played no part:
  Tirith ran and answered.
- Why the worker built that command: the skill told it to use "the exact invocation the
  operator approved", which was that terminal command. No typed path and no recovery rule
  existed, so the scanner rejection ended the card.

## What is in place

| Piece | Where | Role |
|---|---|---|
| `dev_test_run` tool | runtime `tools/dev_test_tool.py` (patch 0004) | Typed arguments only: `profile`, `packages` (inside the profile's approved patterns), `run` (test regex, no spaces or quotes), `race` (default true), `short`, `vet`, `timeout_seconds` (60–3600). No URL, environment or go flag can be passed. Visible only to the dispatcher-owned worker of a card (not delegate children, cron or other sessions) and only once the registry is installed. |
| Trusted runner | kit `hermes/libexec/hermes-devtest-runner` → `/usr/local/libexec/hermes-devtest-runner` (root:root 0755) | Re-validates everything, binds the run to the live claim and lease, builds the clean environment and the sandbox, runs `go test -json -count=1 -p=1 [-race] [-short] [-run=…] -timeout=…s <packages>` (and `go vet <packages>` on request), counts and scrubs. |
| Profile registry | kit `hermes/devtest/profiles.example.json` → `/usr/local/etc/hermes-devtest/profiles.json` (root:root 0644) | The approved target: checkout, module, source roots, package patterns, test container, and the database (`127.0.0.1:55443/matching_test`, user `postgres`, `sslmode=disable`, `MATCHING_TEST_DATABASE_URL`, credential file path). Pins the runner's SHA-256. |
| Guards | runtime `tools/approval.py`, `agent/file_safety.py` (patch 0004) | Additive only, see below. |
| Recovery policy | runtime (rejection text + `kanban_block` refusal), `workflow.py` code card step 7, `kanban-workflow` skill, `SOUL.md`, coder `SOUL.md` | A rejected command with a registered equivalent is retried once through `dev_test_run`, then work continues. |

### Every call, in order

1. Tool: worker context (`HERMES_KANBAN_TASK`/`RUN_ID` in the dispatcher-owned worker), typed
   argument validation.
2. Tool: registry trust (regular file, not a symlink, owned by root, no group/other write,
   every parent directory root-owned and not group/other writable), runner trust (same) and
   `sha256(runner) == helper_sha256`; interpreter `/usr/bin/python3` root-owned. Any
   difference fails closed with the reason; nothing runs.
3. Tool: the exact fixed invocation
   (`/usr/bin/python3 -I -S /usr/local/libexec/hermes-devtest-runner run --task … --run-id … --profile … --package … --race --timeout …`)
   goes through `check_all_command_guards` (hardline floor, dangerous patterns, Tirith). It
   carries no URL, so Tirith allows it; nothing is filtered or allowlisted.
4. Tool: the runner starts with the environment `{PATH=/usr/bin:/bin, LANG=C.UTF-8}`, `cwd=/`,
   stdin closed.
5. Runner: arguments re-parsed (`allow_abbrev=False`, unknown options refused: `-exec`,
   `-toolexec`, `-overlay`, `-modfile`, `-ldflags`, … cannot be expressed), registry re-validated
   (database host must be `127.0.0.1`, database `*_test`, `sslmode=disable`, the URL variable
   outside `PG*`/`GO*`, no unknown keys), go toolchain root-owned.
6. Runner: binding. Its parent process must be the worker pid the board records for the
   running claim of `--task` with `current_run_id == --run-id` (unexpired claim, run row
   `running`), and the repository lease of the profile checkout (`repo_lease.py` format, read
   without running git) must name the same task, run, pid, pid start time and host, unexpired.
   The same invocation from the worker's terminal (parent: bash) is refused.
7. Runner: credential file metadata (regular, no symlink, owner = the runner uid, mode 0600,
   one link, a matching `host:port:db:user` entry). The runner reads it only to learn what to
   scrub; nothing about it is printed.
8. Runner: clean environment built from nothing (`HOME`/`TMPDIR` in a per-run private dir,
   `GOENV=off`, `GOFLAGS=-mod=readonly`, `GOTOOLCHAIN=local`, `GOPROXY=off`, `GOSUMDB=off`,
   `GOWORK=off`, `CGO_ENABLED=1`, dedicated `GOCACHE`, read-only module cache, `PGPASSFILE` and
   the password-free URL). No `PG*`, cloud, proxy, service or Hermes variable reaches the tests.
9. Runner: sandbox (below), run, parse `go test -json`, scrub, print one JSON object:
   `ok`, `exit_code`, `timed_out`, `tests {passed, failed, skipped}`, `skipped_tests` with
   reasons, `failed_tests` with output, `packages`, `failed_packages`, `build_output`, `vet`,
   `no_tests_ran`, `argv`, `database` (URL and file path only), `sandbox`.

Exit codes: 0 passed, 1 tests/vet failed or timed out, 2 refused, 3 sandbox unavailable.

### Sandbox (Landlock ABI ≥ 6 + seccomp; no root, no user namespace)

Unprivileged user namespaces are restricted by AppArmor on this host
(`kernel.apparmor_restrict_unprivileged_userns=1`): `bwrap` fails (`setting up uid map:
Permission denied`; with `--unshare-net`, `loopback: Failed RTM_NEWADDR`), so mount and
network namespaces are not available without relaxing a host security setting. Landlock
(ABI 8 here) needs neither, so the runner uses it:

- Files: read+exec `/usr`, `/lib*`, `/bin`, `/sbin`, the Go root; read `/etc`, `/proc`, `/sys`,
  the module cache; read each source file of the profile's source roots individually (every
  `.env`/`.env.*` except `.env.example|sample|template` is left out, `.git` too, symlinks are
  never granted); read the credential file; read/write only the per-run temp dir and the
  dedicated build cache; `/dev/null|zero|full|random|urandom`. Everything else (`~/.hermes`,
  `~/.ssh`, `~/.config`, other repositories, the checkout's own `.git` and `.env`) is denied,
  and the checkout is read-only (no planted hooks or sources).
- Processes: Landlock scopes signals and abstract UNIX sockets to the sandbox and denies
  ptrace-mode access (`/proc/<pid>/environ`, `mem`) to processes outside it, so the worker's
  environment is not readable.
- Network: TCP connect only to the database port and the ephemeral port range (httptest
  servers), TCP bind only to port 0; seccomp allows `socket()` only for `AF_INET`/`AF_INET6`
  stream sockets (no UNIX sockets such as `docker.sock`, no UDP/DNS, raw, netlink) and refuses
  io_uring and x32 syscalls; foreign architectures are killed.
- `RLIMIT_CORE=0`, `no_new_privs`, own session; killed as a group on timeout.

No sandbox (Landlock missing or below ABI 6, not x86_64, any rule failing) means exit 3 and no
test run; there is no unsandboxed fallback.

### Guards (additive)

- Every mode, including yolo: a terminal command that references a registered credential file
  or its directory (`cat`, `base64`, `ls`, `$PGPASSFILE` expansion, `~`/`$HOME` forms), or that
  runs `docker|podman exec|inspect|cp|export|commit|attach|top|diff` on the registered test
  container, is blocked. A bare `PGPASSFILE=<path>` assignment is not a read and falls through
  to the normal gates. `read_file`/`search_files` refuse the credential file and its directory.
- Unattended deny mode (cron, `-q`, unattended platforms): a database URL or `psql`-family
  `-h/--host/host=`/`PGHOST=` target other than loopback, and a password written into a URL,
  are refused. Before this change a hostname-based production URL passed every guard in `-q`.
- Unchanged: Tirith enabled and fail-closed, `approvals.mode: smart`, single-query/cron/
  unattended modes `deny`, hardline floor, `approvals.deny`, `command_allowlist`.

### Recovery

An unattended rejection of a `go test`/`go vet` command that matches a registered profile (its
URL variable, credential path, `host:port` or `/dbname`) ends with
`RECOVERY: an approved equivalent exists … Call dev_test_run(profile="validate-matching", …)`.
Until the worker calls `dev_test_run` (any outcome counts as tried), `kanban_block` refuses with
the same pointer. The code card (step 7), the skill and both SOULs say the same: read the
rejection, make the approved call once as a new request, verify, continue; never retry, wrap,
encode or script the rejected command; with no approved equivalent, block naming the command,
the finding and the missing capability.

## Threat limits (same-UID, cooperative)

- The tests are the checkout's own code and must authenticate, so they can read the test
  database password. Output scrubbing removes the password (plain and URL-encoded), URL
  userinfo passwords and `password=` values; it stops accidental prints, not deliberate
  encoding. The password is a local test-database credential.
- Landlock's network rules are port-based: a malicious test can still connect to any host on
  the database port or an ephemeral-range port and send what it can read (the source tree,
  the test password). It cannot read `~/.hermes`, `~/.ssh`, other config, other repositories
  or other processes' environments, cannot use UNIX or UDP sockets, and cannot write outside
  its temp dir and build cache.
- Binding and registry trust are evidence against accidents and confused agents, not against
  a malicious process of the same uid: such a process could write the board or the lease file
  or ptrace the worker. The root-owned runner and registry cannot be replaced by the uid;
  the runtime verifies the digest before every call.
- The guards match command text. Obfuscated references (globs, variables built in pieces) can
  evade them; the model is told never to try, and the sandbox, not the guard, is what keeps
  host secrets away from test code.
- Stronger isolation (network namespace, separate uid, container without the docker socket)
  needs a root-level change: an AppArmor `userns` profile for `bwrap` or a dedicated test user
  with a sudo rule. That is an operator decision, not part of this change.

## Install (operator, once; needs the sudo password)

```bash
cd /opt/repos/claude-code-autopilot
bash scripts/install-hermes-devtest.sh plan       # validates the profile, prints the digest it pins
bash scripts/install-hermes-devtest.sh install    # sudo install root-owned runner + profile file
bash scripts/install-hermes-devtest.sh status     # trust + pinned digest == installed runner
make hermes-runtime-patch-status                  # 0001..0004 applied
```

Kanban workers are new `hermes chat -q` processes, so they load patch 0004 when they start;
no gateway restart is needed for them. After any runner change: review, then `install` again
(the old pin makes every call fail closed until then).

## Smoke (from the claimed worker of a card, after `preflight`)

```
dev_test_run(profile="validate-matching", packages=["./..."], race=true, vet=true)
```

Expected: `runner_exit` 0 or 1 with real counts; refusal text naming the failed check
otherwise (`no repository lease` → run `preflight`; `not running under this worker` → not the
claimed worker). From a terminal, the same runner invocation is refused (parent is not the
worker).

## Tests

- Kit: `make test-devtest-runner` — synthetic module and canary credential under the real
  Landlock/seccomp sandbox (race build, counts, skip reasons, scrubbing, `.env`/host secret/
  parent environ/UNIX socket/unapproved port/signal denied, database port reachable, checkout
  read-only), refusals for injected flags, production targets, untrusted config, credential
  metadata, claim/lease binding, missing sandbox; invocation exactly as the runtime does it,
  and the same invocation through a shell refused.
- Runtime: `tests/tools/test_dev_test_tool.py` — the real single-query gate with the operator's
  `security`/`approvals` config and the real Tirith binary: the run-370 command is rejected
  with the exact findings plus the recovery directive; the fixed runner invocation and ordinary
  Go/dev commands pass; credential extraction (also under yolo), container env reads,
  production connections and genuinely unsafe commands stay blocked; the real terminal tool
  never executes a blocked credential read; digest/trust/worker-context refusals; empty
  environment; `kanban_block` recovery refusal.
