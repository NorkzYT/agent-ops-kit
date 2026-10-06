# Learning profile knowledge headless (`make knowledge-learn`)

`make knowledge-refresh` puts books into a profile's `knowledge/` folder. That
is **raw** knowledge: the profile can open the files, but it has not learned
them. A profile is ready only after `/learn` writes its on-demand knowledge
skill, `<profile>-knowledge`: a lean `SKILL.md` index plus one reference file
per chapter, which loads only when a question needs it.

A whole corpus in one `/learn` turn is too long for one run. A 6 MB strategy
corpus timed out at 420 s and wrote no skill. `make knowledge-learn` splits the
work into one bounded Hermes run per file and records each result, so you can
stop and restart it at any point.

## Fast path

```bash
make knowledge-learn-status                         # read-only: raw vs. learned, every book-backed profile
make knowledge-learn NAME=strategy DRY_RUN=1        # plan + the exact hermes commands; writes nothing
make knowledge-learn NAME=strategy LIMIT=1          # learn one book, then check the result
make knowledge-learn NAME=strategy                  # the rest; rerun any time to resume
make knowledge-learn NAME=all JOBS=2                # every profile, two profiles at a time
```

A run over every profile takes hours. Start it detached and check on it with
the status target:

```bash
nohup make knowledge-learn NAME=all JOBS=2 > data/knowledge-learn-all.log 2>&1 &
make knowledge-learn-status
```

Options: `TIMEOUT` (hard limit per file, seconds, default 1800), `MAX_TURNS`
(default 200), `LIMIT` (files per profile this run), `JOBS` (profiles in
parallel, default 1), `DRY_RUN=1`, `FORCE=1` (re-learn learned files),
`RETRY=1` (retry blocked files). Set `HERMES_HOME` when Hermes is not in
`~/.hermes`.

## What one run does

For each file under `~/.hermes/profiles/<p>/knowledge` that is not learned:

1. Builds the official `/learn` prompt with the installed Hermes'
   `agent.learn_prompt.build_learn_prompt` and writes it to
   `knowledge-learn/prompts/<slug>.txt`. The request names one source file,
   the exact skill `<p>-knowledge` (category `knowledge`) and the reference
   file prefix `references/<slug>-`. It tells Hermes to extend the existing
   skill, to continue after chapters that an interrupted run already saved, and
   to treat `knowledge/` as read-only.
2. Runs
   `hermes -p <p> chat --query-file <prompt> --oneshot -Q --max-turns N --run-budget B -t file,skills,todo --source tool`.
   `--query-file` keeps quotes and `$(...)` out of the shell. Single-query
   mode does not expand slash commands, so `-q '/learn …'` would send the text
   as is and never reach the `/learn` prompt. The toolsets exclude terminal,
   web and delegation.
3. Enforces the timeout. `--run-budget` stops the agent 120 s (or 25 %) early
   so it can save its work. At `TIMEOUT` the runner kills the whole process
   group.
4. Checks the result. A file counts as **learned** only if Hermes exited 0,
   the skill changed during the run, and the skill has `references/<slug>-*.md`
   files or lists the source in `SKILL.md`. Each other outcome leaves the file
   pending: `timeout`, `failed`, `no-skill`, `unverified`.
5. Checks the source. If its sha256 changed during the run, the runner marks
   the file `source-modified` and stops that profile.

A file is **blocked** after 3 failed attempts in a row and is skipped until
`RETRY=1`. A timeout that saved new reference files counts as progress, not a
failure. A large book can take several runs and is never blocked for that.

## Idempotency and resume

State is keyed by file path and sha256 in
`~/.hermes/profiles/<p>/knowledge-learn/state.json` and is written after each
file. A rerun:

- skips learned files whose content has not changed;
- re-learns a file whose content changed (`changed`);
- runs pending, timed-out and failed files again;
- treats every file as pending if `<p>-knowledge` was removed.

Ctrl-C or `SIGTERM` stops the runner and kills the Hermes process group. The
file in progress stays pending, no attempt is counted, and chapters already
saved remain in the skill.

Only a skill named exactly `<p>-knowledge` counts. Other skills and hidden
backup folders do not. A per-profile lock (`knowledge-learn/.lock`) skips a
profile that another run is already learning. Profiles run in parallel with
`JOBS`, but the files of one profile always run one at a time, because they
edit the same skill.

## Files per profile

```
~/.hermes/profiles/<p>/knowledge/                 raw books (input, read-only)
~/.hermes/profiles/<p>/skills/knowledge/<p>-knowledge/   the learned skill (written by Hermes)
~/.hermes/profiles/<p>/knowledge-learn/
  state.json        per-file sha256, status, attempts, reference count, log path
  runs.jsonl        one line per attempt
  logs/<UTC>-<slug>.log   command, Hermes output, result line
  prompts/<slug>.txt      the exact prompt sent
  .lock
```

## Reading the status

```
PROFILE      STATUS        RAW  LEARNED  TODO  BLOCKED  SKILL / NOTES
strategy     partial  10f/6424K       3     7        0  skills/knowledge/strategy-knowledge
marketing    needs-learn 5f/1461K     0     5        0  (no marketing-knowledge)
```

| Status | Meaning |
|--------|---------|
| `ready` | `<p>-knowledge` exists and every synced file is learned at its current content |
| `partial` | Some files are learned, or the skill exists, but files are still pending, changed or blocked |
| `needs-learn` | Raw files are synced, nothing is learned yet |
| `no-knowledge` | Installed, no `.md`/`.txt` in `knowledge/` (run `make knowledge-refresh`) |
| `not-installed` | No profile home (run `make hermes-profile NAME=<p>`) |

`RAW` counts synced files and their size. `LEARNED` counts files distilled into
the skill. `running now` means a learn run holds the lock. The specialist
router's `ready` ([specialist-routing.md](specialist-routing.md)) only means a
knowledge skill exists. This status is stricter.

## Safety

- The runner reads books only to hash them. It never writes, moves or
  deletes anything under `knowledge/` or the library.
- It deletes nothing: no skill, state, log or prompt file.
- `--status` and `DRY_RUN=1` write nothing and start no Hermes run.
- `/learn` sends book text to the profile's configured model. Check the
  profile's model before you learn private material.
- It does not restart the gateway, does not read `.env`, and does not install
  or enable plugins.

## Tests

```bash
make test-knowledge-learn
```

The tests are offline. They use a temporary `HERMES_HOME`, a stub `hermes`
(learn, fail, hang, no-op, partial-then-hang, tamper) and a stub
`agent.learn_prompt`.
