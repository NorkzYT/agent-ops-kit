# Discord → Kanban → workers → review → Discord

The standard path for work that needs the Mac worker, the Windows worker, or
more than one lane. Every step is a card on the Hermes Kanban board, thus the
work survives restarts and has an audit trail.

```
Discord request
  └─ orchestrator runs the kanban-workflow skill (one command)
       ├─ [mac] <title>  → mac-worker      (terminal on the Mac over SSH)  ─┐
       ├─ [win] <title>  → windows-worker  (terminal on Windows over SSH)  ─┤ each ends in
       │                                                                     │ kanban_request_review → reviewer
       └─ Report: <title> → default (orchestrator), waits for every lane ◄─┘
            runs the review gate, writes the report, completes
            → gateway notifier wakes the orchestrator in the same Discord thread → final report
```

Hermes `--parent` means "waits for". The request card has the lane cards as
parents, thus it starts only after every lane is done.

| Piece | Where |
|-------|-------|
| Intake, gate, status script | `hermes/skills/kanban-workflow/scripts/workflow.py` |
| Orchestrator instructions | `hermes/skills/kanban-workflow/SKILL.md`, `hermes/SOUL.md` |
| Lane and reviewer prompts | `hermes/profiles/{mac-worker,windows-worker,reviewer}/SOUL.md` |
| Discord shortcut | `!work <mac\|windows\|both> <task>` in `discord-dev-bridge` |

## Why lanes use SSH

The Hermes board is single-host: `~/.hermes/kanban.db` is local SQLite and the
dispatcher spawns workers on this machine. A lane profile thus runs here, and
its terminal and file tools run on the worker through Hermes's SSH terminal
backend. Kanban tools stay in the host process and always reach the board.

A card assigned to the host's `windows-operator` profile also runs here, with a
local terminal. GUI work for the Windows VM still goes to that VM's own Hermes
(mention its bot); see [windows-vm-worker.md](windows-vm-worker.md).

## One-time setup

### 1. Worker machines

Each worker gets a dedicated account with no personal logins, reachable over
Tailscale, with `git` and the build tools. Sign in to GitHub (or the repo
host) yourself as that account; workers never type credentials.

Mac:

```bash
# System Settings → General → Sharing → Remote Login: on, for the worker account only
mkdir -p ~/hermes-work
```

Windows (elevated PowerShell): OpenSSH Server, plus Git for Windows as the SSH
default shell, because the Hermes SSH backend runs `bash -c`.

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell -Value 'C:\Program Files\Git\bin\bash.exe' -PropertyType String -Force
```

From this host, put the host's public key in the worker account's
`authorized_keys` and check the login:

```bash
ssh -i ~/.ssh/id_ed25519 hermes@mac-worker.tailnet 'bash -lc "uname -a; git --version"'
ssh -i ~/.ssh/id_ed25519 hermes@win-worker.tailnet 'bash -lc "uname -a; git --version"'
```

### 2. Profiles on this host

```bash
make hermes-profile NAME=reviewer
PROFILE_SSH_HOST=mac-worker.tailnet PROFILE_SSH_USER=hermes PROFILE_SSH_KEY=$HOME/.ssh/id_ed25519 \
  make hermes-profile NAME=mac-worker
PROFILE_SSH_HOST=win-worker.tailnet PROFILE_SSH_USER=hermes PROFILE_SSH_KEY=$HOME/.ssh/id_ed25519 \
  make hermes-profile NAME=windows-worker
hermes -p mac-worker config get terminal.backend       # ssh
hermes -p windows-worker config get terminal.backend   # ssh
```

`PROFILE_SSH_PORT` and `PROFILE_SSH_CWD` (default `~`) are optional. Lane
profiles are clones of the default profile; keep `terminal.credential_files`
empty on them, because the SSH backend copies listed credential files to the
worker.

### 3. Install the skill and prompts

```bash
make hermes-config-apply     # installs the kanban-workflow skill and the new SOUL.md; no restart
make hermes-restart          # from your own terminal, never from inside Hermes
```

Check without creating anything:

```bash
python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake \
  --title "smoke" --lane mac --lane windows --key smoke-1 --dry-run
```

## Daily use

In Discord:

```
!work both Fix the installer path bug in velvet and run the Windows and macOS test suites
```

or plain words ("run this on the Mac and Windows workers"). The orchestrator
replies with the card ids. You then see, in the same thread:

- `⏸ … blocked: human-approval: …` when a lane needs you (login, MFA, admin
  prompt, a merge). Finish the step, then `hermes kanban unblock <id>`.
- Review lines as lanes hand off to the reviewer, and `🛑 … requested changes`
  when the reviewer returns one.
- The final report when every lane passed review.

From a terminal:

```bash
make workflow-status ID=<request card id>   # request card and its lanes
make workflow-gate ID=<request card id>     # exit 0 iff every lane passed independent review
hermes kanban watch                          # live events
hermes kanban diagnostics                    # stranded or stuck cards
```

## Review gate

A lane card passes only when it is `done`, its last `kanban_request_review` was
followed by a completion, no unanswered change request sits in between, and the
completing run came from a profile other than the implementer. The request card
runs `workflow.py gate` first and blocks with `review-gate: …` when any lane
fails; the report never claims unreviewed work as done.

While a lane sits in `review`, `hermes kanban reopen-review <id> --reason "…"`
sends it back to its worker. `done` is final on the board: a lane completed
without review cannot be reopened, only re-run (see Troubleshooting).

## Human gates

These hold on every lane, in the card body and in the profile prompts:

- No merge, deploy, release or publish. The report lists them under "Needs your approval".
- No passwords, tokens, MFA codes or keys typed by a worker. The lane blocks with `human-approval:` and waits for you.
- No Discord messages from workers. Only the board's notifier and the orchestrator post.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `kanban-workflow: profile 'mac-worker' does not exist` | lane profile missing | step 2 above |
| `terminal.backend is 'local', not 'ssh'` | lane would run on this host | re-run step 2 with `PROFILE_SSH_HOST`/`PROFILE_SSH_USER` |
| `no idempotency key` | intake ran outside a Discord turn | pass `--key <unique>` |
| lane card sits in `ready` | gateway down, or `max_in_progress` reached | `hermes gateway status`, `hermes kanban diagnostics` |
| lane fails at once with an SSH error | worker offline or key refused | the `ssh … uname -a` check in step 1 |
| request card `blocked: review-gate: …` | a lane was completed without independent review | review that lane's branch yourself, or re-run it: a new intake for that lane with `--key <old key>-r2`; then `hermes kanban archive <old request id>` |
| no report in Discord | intake ran without a Discord origin | `hermes kanban notify-list <request id>`; subscribe with `hermes kanban notify-subscribe <id> --platform discord --chat-id <id> --delivery-mode notify+wake` |

Tests: `make test-kanban-workflow` (fake runner, plus the real `hermes kanban`
CLI on a temporary board when Hermes is installed; never the live board).
