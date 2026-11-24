# AGENTS.md — agent-ops-kit

> Follows the [AGENTS.md](https://agents.md/) open standard so any coding agent
> (Claude Code, Codex, Cursor, Gemini CLI, …) gets the same guidance on task start.

## Project overview

Two things live here:

1. **The stack.** `docker-compose.yml` (Honcho, Ollama, CLIProxyAPI),
   `scripts/claude-max-proxy/` (the Claude proxy as a host systemd user
   service), `Makefile`, `scripts/` and `hermes/` install and configure
   a Hermes agent that runs from Discord. Hermes is installed on the host by
   `scripts/hermes-install.sh`; the compose file is the HTTP half.
2. **The Claude Code kit.** `.claude/` is a portable bundle (Python hooks,
   Markdown agents and skills, bash scripts) that `install.sh` copies into other
   repos. It is what the coding subagents follow.

There is no application build. Hooks are Python 3 stdlib only; scripts are
bash; agents, skills and profiles are Markdown with YAML frontmatter.

## Validate changes

```bash
bash .claude/extras/doctor.sh                 # kit structure, settings JSON, hook syntax
python3 .claude/scripts/test_guard.py         # bash guard allow/block behaviour
bash .claude/scripts/test_self_update.sh      # installer manifest replay
docker compose config -q                      # after editing docker-compose.yml (needs .env)
bash -n scripts/*.sh scripts/claude-max-proxy/*.sh   # after editing scripts
bash scripts/claude-max-proxy/install.sh --render-only   # unit + proxy.env render (needs .env)
make doctor                                   # on a host with the stack running
```

CI (`.github/workflows/ci.yml`) runs the same checks plus a YAML/JSON parse of
the Hermes templates and an offline render of `scripts/stack-init.sh`.

## Conventions

- **Smallest change that satisfies the task.** No drive-by refactors.
- **Scripts:** bash, `set -euo pipefail`, idempotent; share helpers through
  `scripts/lib.sh`. Templates use `__UPPER_SNAKE__` placeholders rendered by
  `render_template`.
- **Hooks:** read one JSON event from stdin, exit `0` to allow or `2` to block,
  keep them fast, no new dependencies.
- **Hermes config:** every key in `hermes/config.yaml.tmpl` must exist in the
  Hermes docs. Secrets never go in `config.yaml`; they go in `~/.hermes/.env`
  and are referenced as `${VAR}`. Apply `.env` changes with
  `make hermes-config-apply` (forced render, no restart), then
  `make hermes-restart` from the operator's terminal (docs/install.md).
- **Hermes source separation** (docs/hermes-source-management.md). Agent-Ops-Kit is the canonical home of Hermes config, prompts, skills, templates, infrastructure and reviewed runtime patches. The installed runtime (`~/.hermes/hermes-agent`, however reached: shell, direct file tools, symlinks or aliases) is read-only for diagnostics and is never a coding, test or patch workspace. Classify first: config/skill/supported extension point → kit only; native Hermes defect → narrow patch in a separate isolated checkout pinned to an exact upstream revision; privileged/infra change → reviewed kit artifacts, installed only with separate authorization. Never overwrite, reset, stash or fold unrelated dirty files into a patch. Review and authorized deployment are separate steps; never auto-apply patches or auto-restart the gateway (restarts follow the existing operator approval).
- **Docs:** short, one path per task, commands in fenced blocks. `README.md` is
  the fast path; detail lives in `docs/`.
- **Commits:** Conventional Commits (`feat(hermes): …`, `fix(compose): …`,
  `docs: …`). Feature branches, never `main`. No `Co-Authored-By` trailers; the
  generated `commit-msg` hook rejects them.

## Policy files, in order

1. `.claude/CLAUDE.md` — the constitution the coding worker follows
2. `hermes/SOUL.md` — the orchestrator's rules (delegation, boundaries, memory)
3. `hermes/profiles/coder/SOUL.md` — the pipeline every coding subagent runs

## Task completion protocol

For every fix or feature:

1. **Understand** — read the relevant files and the docs page they belong to.
2. **Change** — the smallest diff.
3. **Validate** — the commands above that apply.
4. **Confirm** — for stack changes, `make up && make doctor` on a real host;
   for Hermes config, `hermes doctor` and one real chat turn.
5. **Commit** — on a feature branch.
6. **Report** — what changed, what was verified, what was not.

Do not mark a task complete after code-only changes.

## Timed follow-ups

Never promise "I'll check back in X" unless a real scheduler job exists. In
Hermes: `hermes cron create "in 30m" "<task>" --deliver discord`, then quote
the job id. If scheduling fails, say so and ask to be pinged.

## Model routing

- Orchestrator: ChatGPT subscription through CLIProxyAPI (`HERMES_MODEL`,
  `gpt-6.1-terra` default: the configured starting model in
  `model.default`, used as-is when the router leaves a request alone).
  Task-scoped routing rewrites each request per task (current user task only, never
  history/tool output): status→terra low effort, ordinary work→terra medium,
  ambiguous/planning→terra, genuinely hard or a leading standalone `#sol`
  line→the newest eligible Sol (static `gpt-6.2-sol`); the newest eligible
  Astra (static `gpt-6.2-astra`) only with a leading standalone
  `#max`/`#astra` line in the current task. GPT model floor: every
  kit-managed GPT call is 6.1+ and Terra or higher; no below-floor fallback,
  stale overrides are refused (`scripts/model-floor-migrate.py` updates
  existing installs; `--check` gates `make hermes-restart` because auxiliary
  calls bypass the router seams; docs/model-routing.md, GPT model floor). Reasoning
  effort is rewritten per task up to the `agent.reasoning_effort` ceiling
  (`high`); auxiliary entries keep their own `reasoning_effort`.
- Coding subagents: Claude Max through claude-max-proxy (`HERMES_CODING_MODEL`,
  Opus by default; Fable only for genuinely difficult engineering). Claude
  effort is set per task: routine→medium, complex→high, unusually hard
  debugging/architecture→xhigh, `max` only with a leading `#max`/`#astra`
  line; capped by `coding_effort_ceiling` (default `max`), not the orchestrator
  ceiling; the proxy maps the label to the CLI thinking budget. A leading
  standalone `#opus`/`#fable` line forces that coding model for the request
  (coding-family seam only; refused on the orchestrator seam, never faked with a
  GPT alias; privacy takes precedence).
- Task routing + fail-closed privacy live in the `model-router` plugin
  (`hermes/plugins/model-router/`). A request whose current user turn carries
  `#privacy` (or the equivalent `#private`) never reaches a cloud provider —
  local Ollama or refuse. Only that marker counts (no content detection,
  `#local` inert, not sticky);
  earlier marked turns are withheld from later public requests. Needs
  `MODEL_ROUTER_PRIVACY=true`; `.env.example` currently ships the `false`
  opt-out (main-loop guard off; the aux guard and specialist router still
  keep marked turns off the cloud).
- Local Ollama roles (one endpoint, `OLLAMA_*_MODEL`): routine `qwen3.5:27b`
  (ordinary private turns, local specialist classifier; titles only with
  `AUX_BASE_URL`), agent `qwen3.8:27b`
  (difficult private turns, private coding, fallback agent), fallback
  `qwen3.6:27b` (one retry, then refuse). Private routing never falls back to
  cloud. `make local-route-status` / `local-route-readback` show the result.
- Native fallback chains (non-private only): orchestrator CLIProxyAPI →
  claude-max-proxy → Ollama agent → Ollama fallback; delegation
  claude-max-proxy → Ollama agent → Ollama fallback.
- Compression: CLIProxyAPI `gpt-6.1-terra`, no fallback
  (`AUX_COMPRESSION_*`; `AUX_COMPRESSION_ROUTE=aux` pins it to the title route).
  The plugin's aux guard refuses `#privacy`/`#private` payloads to it.
- Session titles: the main route (CLIProxyAPI, `HERMES_MODEL`); no `AUX_*` title vars.
- Honcho reasoning: CLIProxyAPI. Embeddings: local Ollama.
- No vendor API keys anywhere in the stack.
- Full reference: `docs/model-routing.md`. Verify: `make route-smoke`,
  `make test-router`.
- Specialist routing (pilot; plugin default off, `.env.example` on with the
  `cliproxy` classifier, `gpt-6.1-terra`): `specialist_routing` in the same
  plugin suggests 0-15 book-backed profiles per current task (15 is the hard
  cap; verified attachment signals count before it); private tasks never
  reach cloud-backed profiles; every profile is advisory, none is labelled
  high-risk. Dry-run:
  `TASK="..." make specialist-route`; live decisions: `make specialist-logs`.
  A bare uploaded prompt file routes on its verified content
  (`attachment-derived`). Activation: `make specialist-status`,
  `specialist-install` (no restart), `specialist-enable` (guarded restart, never
  from inside Hermes). Reference: `docs/specialist-routing.md`.
