# Refreshing profile knowledge after adding books

Use this after you drop new books into the library (`/mnt/books` by default).
One command stages and syncs every book-backed profile and tells you what
changed. It never moves, edits or deletes a book, never deletes staged
knowledge, and never decides which profile a new book belongs to. For
model-proposed profiles, use `make knowledge-classify`
([knowledge-classify.md](knowledge-classify.md)): it is dry-run by default and
assigns only with `APPLY=1` or your explicit approval.

## Fast path

From the repo root:

```bash
make knowledge-inventory                  # read-only: what is new, missing or unlisted
make knowledge-classify                   # optional: AI proposals per book (dry-run)
# add each new book to the right profile's book list (lines are printed for you),
# or: make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVE='<profile>=<library path>'
make knowledge-refresh                    # stage + sync every profile with a book list
make knowledge-refresh NAME=strategy      # or only one profile
```

Then, for each profile the report marks `updated`, open its chat and learn:

```
hermes -p strategy chat
/learn ~/.hermes/profiles/strategy/knowledge
```

`/learn` is a model turn and sends the book text to that profile's model, thus
the refresh prints the command and leaves it to you. Re-running `/learn` merges
new material into the profile's existing knowledge skill.

## What the refresh does

For each profile with `hermes/profiles/<name>/knowledge-sources.txt`:

1. `scripts/knowledge-stage.sh` builds `data/knowledge/<name>` from the book
   list. Text is copied; EPUB/PDF is converted. Unchanged books are skipped.
2. `scripts/knowledge-sync.sh` copies that corpus into
   `~/.hermes/profiles/<name>/knowledge` (additive, no pruning) when the
   profile is installed. Profiles that are not installed are staged only; the
   report gives the `make hermes-profile NAME=<name>` command.
3. File hashes before and after decide the status.

## Reading the report

```
PROFILE                      STATUS         NOTES
strategy                     updated        2 file(s) new/changed in the profile
philosophy                   current
survival                     not-installed  0 staged file(s) new/changed; install with: make hermes-profile NAME=survival
```

| Status | Meaning | Action |
|--------|---------|--------|
| `updated` | New or changed files reached the profile | Run `/learn` in that profile |
| `current` | Nothing changed | None |
| `not-installed` | Staged under `data/knowledge/`, no Hermes profile yet | `make hermes-profile NAME=<name>` |
| `error` | Bad book list or sync failure; the note names the cause | Fix the line, rerun |

**Missing from the library** lists book-list lines with no matching path under
the library. Fix the spelling or remove the line.

**Uncategorized** lists library books that no book list includes, at the
smallest unlisted path (a new file inside a listed folder is not reported,
since the folder already covers it). Each comes with the exact line to append:

```
  - edu/The Code Breaker - Jennifer Doudna, Gene Editing, and the Future of the Human Race
      printf '%s\n' 'edu/The Code Breaker - Jennifer Doudna, Gene Editing, and the Future of the Human Race' >> hermes/profiles/<profile>/knowledge-sources.txt
```

Replace `<profile>` with the profile you choose. The inventory does not
guess; `make knowledge-classify` can propose one (see
[knowledge-classify.md](knowledge-classify.md)).
To keep a book out of every profile on purpose, add its line to
`hermes/knowledge-ignore.txt` instead.

Logs for each profile's stage and sync run go to a temp folder; the report
prints its path.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every selected profile staged cleanly (uncategorized books do not fail the run) |
| `3` | A listed book is missing or could not be converted (see the log) |
| `1` | Usage or safety error (bad profile name, unsafe book-list path, library overlap) |

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `BOOKS_ROOT` | `/mnt/books` | Book library, read-only |
| `KNOWLEDGE_ROOT` | `data/knowledge` | Staged corpora, never pruned |
| `HERMES_HOME` | `.env` value, else `~/.hermes` | Where installed profiles live |
| `KNOWLEDGE_IGNORE` | `hermes/knowledge-ignore.txt` | Library paths left out on purpose |
| `KNOWLEDGE_CONVERT` | `1` | `0` copies text books only |

Stale files from books you removed from a book list stay in
`data/knowledge/<name>` and the profile. Delete them by hand, or sync with
`KNOWLEDGE_SYNC_MODE=mirror make hermes-profile NAME=<name>`.

## Discord prompt template

Paste this to Hermes in Discord after adding books. Fill in the brackets. To
have the model propose the mapping first, use the template in
[knowledge-classify.md](knowledge-classify.md#discord-prompt-template).

```
I added books under /mnt/books. Refresh profile knowledge in the agent-ops-kit repo:
1. Run `make knowledge-inventory` and post the Missing and Uncategorized sections as they are.
2. Do not add books to any knowledge-sources.txt yourself. Wait for my mapping.
3. Mapping: [edu/<book folder> -> <profile>, ...]. Append exactly these lines with the printed printf commands and show `git diff hermes/profiles`.
4. Run `make knowledge-refresh` [NAME=<profile>] and post the report table and exit code.
5. Do not run /learn and do not touch /mnt/books. List the /learn command for every `updated` profile.
```

Then run `/learn` in each listed profile yourself.
