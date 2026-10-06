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
3. creates `~/.hermes/profiles/<name>/knowledge/` and syncs the profile's book
   corpus into it from the shared knowledge root (see below);
4. writes the profile's `.env` (proxy key, optional Discord token).

## Giving a profile knowledge

Put `.md` or `.txt` files (a book per file, chapters as headings) under the
shared knowledge root, one folder per profile. EPUB and PDF books convert with
`make knowledge-convert` (see `docs/document-conversion.md`):

```
data/knowledge/<name>/**/*.md
```

`make hermes-profile NAME=<name>` copies that folder into
`~/.hermes/profiles/<name>/knowledge/` every run. The copy is additive: it adds
and refreshes files and never deletes the source or unrelated destination files.
Point it at an SMB/CIFS mount with `KNOWLEDGE_ROOT`, and prune stale files with
`KNOWLEDGE_SYNC_MODE=mirror`:

```bash
make hermes-profile NAME=marketing KNOWLEDGE_ROOT=/mnt/books/knowledge
```

Repo-tracked seed files in a profile's own `hermes/profiles/<name>/knowledge/`
still copy first; the shared knowledge root is authoritative and refreshes any
file it also provides. Books stay out of Git (`data/` is gitignored). See
`data/knowledge/README.md` for the folder convention and recommended groups.

Then build the profile's knowledge-base skill in that profile's chat:

```
/learn ~/.hermes/profiles/<name>/knowledge
```

Hermes writes a knowledge-base skill: a lean `SKILL.md` plus one distilled
reference file per topic, loaded on demand. Re-running `/learn` folds new
material into the existing skill instead of duplicating it. A 400-page book
does not sit in every prompt; only the chapter the question needs does.

## Book lists

A profile's `knowledge-sources.txt` lists the books it learns from, one path
per line relative to the library root (`/mnt/books` by default). Build the
profile's corpus from it, then sync and learn:

```bash
make knowledge-stage NAME=philosophy     # copies .md/.txt, converts EPUB/PDF
make hermes-profile NAME=philosophy      # syncs data/knowledge/philosophy
# then in `philosophy chat`:  /learn ~/.hermes/profiles/philosophy/knowledge
```

`knowledge-stage` only reads the library and writes under `data/knowledge/`
(gitignored). EPUB/PDF conversion uses the `document-converter` skill; set
`KNOWLEDGE_CONVERT=0` to copy text books only.

After adding books to the library, `make knowledge-inventory` lists books in
no book list, and `make knowledge-refresh` stages and syncs every profile
and reports which ones need `/learn`. See `docs/knowledge-refresh.md`.

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
| `business` | Operations, finance and market analysis (analysis only, no transactions) |
| `security` | Security review and authorised penetration testing |
| `research` | Deep research and synthesis |
| `offers-sales` | Offer design, pricing, lead generation and sales funnels (drafts only) |
| `leadership-teamwork` | Team design, delegation, feedback, conflict and hiring advice |
| `negotiation-communication` | Negotiation plans, hard conversations and message drafts |
| `software-engineering` | Architecture, review standards and testing strategy (advisory, read-only) |
| `herbalism` | Botanical knowledge, educational only (not medical advice) |
| `health-fitness` | Training, nutrition and habit plans, educational only (not medical advice) |
| `martial-arts` | Grappling technique study, conditioning and injury-aware progression |
| `survival` | Lawful, low-risk preparedness and emergency guidance |
| `philosophy` | Philosophy, mental models and decision reflection |
| `windows-operator` | Computer-use worker that runs inside the Windows VM |
| `_template` | Copy this to start a new specialist (support, legal, ...) |
