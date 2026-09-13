# Hermes

Hermes is the only controller in this stack. It lives in Discord, keeps memory
in Honcho, drives the browser, runs cron jobs, and delegates coding to Claude
subagents. The upstream docs are at
<https://hermes-agent.nousresearch.com/docs>; this page covers what the kit
configures and why.

## Files the kit writes

| Path | Source | Purpose |
|------|--------|---------|
| `~/.hermes/config.yaml` | `hermes/config.yaml.tmpl` | models, toolsets, browser, memory, Discord, Kanban |
| `~/.hermes/.env` | `.env` | secrets referenced from config as `${VAR}` |
| `~/.hermes/honcho.json` | `hermes/honcho.json.tmpl` | self-hosted Honcho endpoint, peer and workspace |
| `~/.hermes/SOUL.md` | `hermes/SOUL.md` | the orchestrator's persona and rules |
| `~/.hermes/memories/USER.md` | `hermes/USER.md` | seeded once, then maintained by Hermes |
| `~/.hermes/skills/agent-ops-kit/*` | `hermes/skills/*` | `autonomous-engineering`, `discord-dev-bridge` |

Edit `.env` and re-run `make hermes-install`, or edit `~/.hermes/config.yaml`
directly and `hermes gateway restart`.

## Models

```yaml
model:                       # the orchestrator
  provider: custom
  default: gpt-5.6-sol       # HERMES_MODEL in .env
  base_url: http://127.0.0.1:8317/v1
  api_key: ${CLIPROXY_API_KEY}

delegation:                  # subagents from delegate_task
  provider: custom
  model: opus                # HERMES_CODING_MODEL: opus | sonnet | haiku | exact id
  base_url: http://127.0.0.1:3456/v1
  api_key: ${CLAUDE_MAX_PROXY_API_KEY}
  api_mode: chat_completions
```

The orchestrator thinks on the ChatGPT subscription. Anything it delegates
(coding, long research) runs on Claude Max through claude-max-proxy. To use a
specific Claude model such as Fable, set `HERMES_CODING_MODEL` to the id shown
by `make models`.

Why not Hermes's own Anthropic OAuth? Its docs state it only works on Claude
Max **with purchased extra usage credits**. The proxy uses the plan's included
allowance instead.

## Toolsets

`platform_toolsets` in the template lists what the bot may use from the CLI and
from Discord: web, search, terminal, file, browser, vision, skills, todo,
memory, session_search, cronjob, code_execution, delegation, clarify, kanban,
plus `discord` and `discord_admin` on Discord. Remove a name to take a
capability away. `terminal.cwd` is `REPOS_DIR` so relative paths mean repos.

## Discord

Set in `.env`: `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS` (your user id;
empty means nobody), `DISCORD_HOME_CHANNEL` (where cron and Kanban results
go), and optionally `DISCORD_FREE_RESPONSE_CHANNELS` for channels where the
bot answers without an @mention.

Behaviour set by the template: reply only when mentioned in server channels,
open a thread per mention, keep replying inside that thread, react while
working, one session per user in shared channels.

```bash
hermes gateway status        # running?
hermes gateway restart
make hermes-logs             # tail ~/.hermes/logs/gateway.log
```

Short operator commands (`!ship`, `!test`, `!review`, `!status`) come from the
`discord-dev-bridge` skill.

## Delegation

`delegate_task` spawns an isolated subagent with its own context. The
orchestrator uses it for every coding task: repo path, exact goal, acceptance
criteria, and a request for a report with files changed and test output. The
`coder` profile's `SOUL.md` is the pipeline those subagents follow (triage,
plan, implement, verify, commit on a branch, report). Limits in the template:
four concurrent children, depth two, one hour per child.

## Cron

Hermes schedules jobs itself and delivers output to Discord. Recipes live in
`hermes/cron-jobs.md`: nightly tests, weekly dependency audit, a heartbeat that
only speaks when something is wrong, and the Super Productivity morning plan
that asks for approval before starting work.

```bash
hermes cron list
hermes cron create "0 2 * * *" "Run the test suite in /opt/repos/app and report failures." --name nightly --deliver discord
```

The orchestrator's rule: never promise "I'll check back in X" without creating
a job first and quoting its id.

## Verification on stop

`agent.verify_on_stop: true` makes Hermes re-check its own claim of "done"
before replying. Keep it on for unattended work.

## Useful commands

```bash
hermes chat                  # terminal session
hermes doctor                # environment check
hermes memory status         # Honcho connected?
hermes profile list          # specialist profiles
hermes kanban watch          # live board
hermes tools                 # what is enabled
hermes update                # upgrade Hermes
```
