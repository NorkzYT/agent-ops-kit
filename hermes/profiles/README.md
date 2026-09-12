# Hermes profiles

Each directory here is a **specialist agent** in the Custom-GPT sense: a name,
its own instructions (`SOUL.md`), its own knowledge, its own memory, and, if you
want, its own Discord bot. Create one with:

```bash
make hermes-profile NAME=marketing
```

That runs `scripts/hermes-profile.sh`, which:

1. creates `~/.hermes/profiles/<name>/` with `hermes profile create <name> --clone`
   (same model/proxy settings as the orchestrator);
2. installs this directory's `SOUL.md` as the profile's instructions;
3. creates `~/.hermes/profiles/<name>/knowledge/` for books and notes;
4. writes the profile's `.env` (proxy key, optional Discord token).

## Giving a profile knowledge

Drop `.md` or `.txt` files (a book per file, chapters as headings) into
`~/.hermes/profiles/<name>/knowledge/`, then in that profile's chat:

```
/learn ~/.hermes/profiles/<name>/knowledge
```

Hermes writes a knowledge-base skill: a lean `SKILL.md` plus one distilled
reference file per topic, loaded on demand. Re-running `/learn` folds new
material into the existing skill instead of duplicating it. A 400-page book
does not sit in every prompt; only the chapter the question needs does.

## Talking to a profile

- `marketing chat` from a terminal (launcher created by Hermes)
- `hermes -p marketing chat`
- its own Discord bot: put a second bot token in
  `~/.hermes/profiles/marketing/.env` and run `marketing gateway install`

## Teams

Profiles collaborate through the shared Kanban board. See `../teams/` for
ready-made recipes and `docs/profiles-and-teams.md` for the walkthrough.

## Included profiles

| Profile | Purpose |
|---------|---------|
| `coder` | Engineering worker: triage, plan, implement, verify, commit, report |
| `marketing` | Growth and positioning specialist (formerly the CrewAI growth crew) |
| `strategy` | Business strategy and decision analysis |
| `security` | Security review and authorised penetration testing |
| `research` | Deep research and synthesis |
| `windows-operator` | Computer-use worker that runs inside the Windows VM |
| `_template` | Copy this to start a new specialist (herbalism, support, ...) |
