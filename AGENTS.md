# AGENTS.md — agent-ops-kit

> Follows the [AGENTS.md](https://agents.md/) open standard so any coding agent
> (Claude Code, Codex, Cursor, Gemini CLI, …) gets the same guidance on task start.

## Project overview

Two things live here:

1. **The stack.** `docker-compose.yml` (Honcho, Ollama, CLIProxyAPI,
   claude-max-proxy), `Makefile`, `scripts/` and `hermes/` install and configure
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
bash -n scripts/*.sh                          # after editing scripts
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
  and are referenced as `${VAR}`.
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

- Orchestrator: ChatGPT subscription through CLIProxyAPI (`HERMES_MODEL`).
- Coding subagents: Claude Max through claude-max-proxy (`HERMES_CODING_MODEL`,
  Opus by default; set an exact id for Fable).
- Honcho reasoning: CLIProxyAPI. Embeddings: local Ollama.
- No vendor API keys anywhere in the stack.
