# Trusted Go/PostgreSQL dev tests for Kanban workers (`dev_test_run`)

> **Status (2026-11-02): NOT INSTALLED, DISABLED, DESIGN REJECTED.** An independent review of
> the candidate in kit commit 3e84caa / runtime d15a70ddc8 found that its same-UID Landlock
> design does not meet the mandatory network and credential isolation requirements. The
> candidate is kept in the kit with its tractable defects fixed and regression-tested, but it
> **fails closed everywhere**: the runner refuses every profile whose `isolation.mode` is
> `same-uid-landlock` (the only implemented mode), the installer refuses to plan or install
> such a profile, `status` exits non-zero, the runtime tool is hidden and emits no `RECOVERY:`
> directive for a rejected profile, and workers are told to block naming the missing
> capability. Nothing below is available to a worker until the approved design in
> [Approved design (operator approval required)](#approved-design-operator-approval-required)
> is provisioned, implemented and approved. Validate card `t_12595b96` stays blocked on this.

## Review findings and what was done (kit 3e84caa → this change)

| # | Finding | Status |
|---|---------|--------|
| 1 | Tests run as the uid that owns the credential file and the docker socket group; text guards are not a boundary | **Unresolved by design** → fail-closed gate; needs the broker/identity in the approved design |
| 2 | `postgres` superuser lets a test escape via `COPY TO PROGRAM`, `pg_read_server_files` | **Unresolved** → approved design requires a verified non-superuser role; no DB change made |
| 3 | TCP rules granted the whole ephemeral range (any host) and were described as "loopback" | **Fixed**: database port only; `sandbox.network_limit` says any host may be reached on it |
| 4 | seccomp left `socket()` protocol unchecked (MPTCP/SCTP) | **Fixed**: protocol 0 or `IPPROTO_TCP` only; negative test with real syscalls (SCTP, MPTCP, UDP, UNIX, raw, netlink → `EACCES`) |
| 5 | Source grants by path (TOCTOU), hardlink aliases of `.env` granted | **Fixed**: verified `O_PATH|O_NOFOLLOW` descriptors (regular, `st_nlink == 1`, dev/ino checked) held until the rule is added |
| 6 | Test/package names and other keys were not scrubbed; truncation before scrubbing | **Fixed**: raw streams scrubbed before parsing, then every key and value of the result (`scrub_object`); encoded leaks remain unsolvable → gate |
| 7 | Unbounded `communicate()` after timeout, escaped `setsid` descendants, no parent-death | **Fixed (bounded, not contained)**: `PR_SET_PDEATHSIG`, group kill, `KILL_GRACE`-bounded drain, vet budget from the remaining time; escaped descendants reported, not contained |
| 8 | Container never verified; whole `pgpass` granted | **Partly fixed**: per-run file with only the registered entry; endpoint identity still unverified (needs the broker) |
| 9 | Malformed lease crashed the runner; TTL unbounded; board never compared | **Fixed**: typed lease fields, TTL 60..7200 s, future-skew refused, `board` compared (config `board`, default `default`) |
| 10 | `/proc` readable (other processes' `cmdline`) | **Unresolved**: Go toolchain needs `/proc`; needs a pid namespace |
| 11 | Zero matched tests returned `ok: true` | **Fixed**: zero tests → `ok: false`, exit 1, `error` names the filter |
| 12 | Malformed `dev_test_run` call cleared the recovery state; state global | **Fixed (runtime f04d4adc80)**: per (task, run) state; cleared only by an actual runner start or a recorded precondition refusal |
| 13 | `database_target_block` missed `?host=`, multi-host, `PGHOSTADDR`, `PGSERVICE` | **Fixed (additive text guard, not a boundary)** |
| 14 | Shared build cache across profiles | **Fixed**: per-profile cache directory |
| 15 | Installer `status` exited 0 when missing/untrusted | **Fixed**: non-zero; `plan`/`install` print `REJECTED` and exit 1 for a rejected design |

## Approved design (operator approval required)

Nothing here exists yet. Provisioning needs root and cannot run unattended: `sudo -n -l` on
this host grants only `NOPASSWD: /usr/local/libexec/hermes-validate-cwd-scan ""`; unprivileged
user namespaces are blocked by AppArmor (not to be relaxed). The operator performs these steps
from an independent privileged shell after reviewing them; none of them is executed by Hermes.

1. **Credential broker identity.** A dedicated system user (e.g. `hermes-devtest-broker`) owns
   the test credential; `polymath-dev` (and therefore every worker, delegate and terminal) has
   no read access to it. The broker is a small root-installed service (`systemd` unit, UNIX
   socket with peer-credential check) that, given `(task, run_id, profile)`, verifies the live
   board claim and repository lease itself and then starts the test process. The password is
   never handed to the worker uid in any form.
2. **Test identity.** A second dedicated user (e.g. `hermes-devtest-runner`, not in `docker`,
   `sudo` or `adm`) runs the tests. It gets the credential through a file descriptor the
   broker opens (`PGPASSFILE` on a memfd / private tmpfs), never via a path in the worker home.
3. **Database role.** A `matching_test`-only role (`NOSUPERUSER NOCREATEROLE NOCREATEDB
   NOREPLICATION NOBYPASSRLS`, no membership in `pg_read_server_files`, `pg_write_server_files`,
   `pg_execute_server_program` or any `pg_*` role) with grants limited to the test schema. The
   broker refuses to start when `SELECT rolsuper, ...` for the role shows anything else. No
   change to the existing container or its `postgres` superuser until the operator approves it.
4. **Endpoint identity.** The broker verifies that `127.0.0.1:55443` is published by the
   registered container (`docker inspect` run by root or by a user in `docker` that is *not*
   the worker) and pins the container id for the run.
5. **Network namespace.** Tests run in their own network namespace (root `ip netns` or a
   `systemd` unit with `PrivateNetwork=` plus a veth/port-forward to the test service only).
   Reachable: the test database and the sandbox's own loopback (`httptest` servers). Nothing
   else; no DNS, no docker socket, no host loopback.
6. **Source snapshot.** The tests build from a read-only snapshot (`git archive` of the claimed
   checkout at the lease's recorded HEAD, or a bind-mount read-only) so a concurrently writing
   process cannot alter sources mid-run; `.env*` excluded as today.
7. **Process containment.** A transient `systemd` scope/cgroup per run (`systemd-run --scope`
   or `--property=...`), `KillMode=control-group`, `RuntimeMaxSec=` = the requested timeout
   plus vet budget, pid namespace. Timeout kills the whole cgroup; nothing escapes with `setsid`.
8. **Registry.** `/usr/local/etc/hermes-devtest/profiles.json` root-owned with
   `isolation.mode: credential-broker-netns`, pinned runner digest, and the broker socket path.
   The runtime tool and the runner refuse any other mode (already enforced).
9. **Verification before resume.** Kit tests with synthetic credentials under the real
   broker; an installer `status` exiting 0; then a real Kanban smoke from a claimed worker of a
   throwaway card (`dev_test_run(profile="validate-matching", run="^TestNoSuch$")` must return
   `ok: false` with `matched no tests`, then a real run with counts). Only after that smoke is
   `t_12595b96` resumed.

Approval scope requested from the operator: the two system users, the systemd unit/socket, the
sudoers-free root installation of broker + runner + registry, the database role and schema
grants, the network namespace plumbing, and the docker inspect right for the broker. No change
to Tirith, approval modes, hardline/deny rules, AppArmor, sysctls or the existing worker uid.

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
- Network: TCP connect only to the database port (port-based: any host on that port is
  reachable, loopback is not confined; `httptest` servers on ephemeral ports are therefore not
  reachable in this design), TCP bind only to port 0; seccomp allows `socket()` only for
  `AF_INET`/`AF_INET6` `SOCK_STREAM` with protocol 0 or `IPPROTO_TCP` (no UNIX sockets such as
  `docker.sock`, no UDP/DNS, raw, netlink, MPTCP or SCTP) and refuses io_uring and x32
  syscalls; foreign architectures are killed.
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
- These limits are why the design is rejected and disabled; the approved design above
  removes them with root-provisioned identities and namespaces. That is an operator decision.

## Install (operator, once; needs the sudo password) — currently refused

`plan` and `install` exit 1 with `REJECTED` for the shipped profile (`isolation.mode:
same-uid-landlock`); `status` exits non-zero while nothing trusted is installed. The commands
stay documented for the approved design.

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

## Smoke (from the claimed worker of a card, after `preflight`) — not available until approved

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
