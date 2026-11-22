# Mode Eval Harness

Measures the kit's **modes** against a corpus of tasks with objective pass/fail
criteria, so "which workflow is better?" becomes a scoreboard instead of an
opinion. This is the arbiter for the kit's workflow decisions — recorded
results live in `FINDINGS.md`.

## Run it

```bash
bash .claude/eval/run-eval.sh                      # default: mock-solve runner (no model, no cost)
bash .claude/eval/run-eval.sh --runner claude      # real: drives `claude -p` per cell
bash .claude/eval/run-eval.sh --modes "minimal native" --tasks py-evenodd
bash .claude/eval/test_eval.sh                      # E2E self-test (mock runners)
```

Output: a results TSV (`mode  task  runner  pass  seconds  tokens  runner_status`)
under `results/`, plus a scoreboard. `tokens=NA` means unmeasured (no/invalid usage),
never 0. `runner_status` is `ok`, `error`, `timeout`, `missing`, or `NA` (runner
wrote none, e.g. mocks); a failed runner never skips `verify.sh`. The real runner's
metrics also carry `runner_exit` and, when reported, `cache_read_tokens` /
`cache_creation_tokens` (kept out of `tokens`); non-empty model stderr is appended
to `<results>.tsv.stderr.log`.

```
MODE         PASS      AVG_S
autopilot    2/2       0.0
minimal      2/2       0.0
native       2/2       0.0
(overall)    6/6       0.0
```

## How a cell runs

For every `(mode, task)`: a fresh temp **workdir** is seeded from `tasks/<id>/seed/`,
the mode's overlay is written as `workdir/CLAUDE.md`, the **runner** acts on the
workdir, then `tasks/<id>/verify.sh` runs there (`exit 0` = pass). Time and (real
runner) token usage are recorded. Fixtures are never mutated.

> **Isolation:** synthetic tasks use temp-dir isolation. For a real-repo corpus
> (e.g. your application repo), swap per-cell setup to a `wt` worktree off a
> base commit — see `.claude/docs/worktrees.md`.
>
> **Host deps:** a task may declare required commands in a `requires` file —
> one per line; `a|b` means any-of (the py tasks declare `uv|python3`). Tasks
> with unmet requirements are skipped loudly, never recorded as failures.
> Python tasks run through `eval_python` in `lib.sh`, which prefers
> [uv](https://docs.astral.sh/uv/) (provisions an interpreter on demand) and
> falls back to system `python3`.

## Pieces

| Path | What |
|---|---|
| `modes/modes.tsv` | Registry: `mode <tab> overlay-file <tab> description`. |
| `modes/*.md` | Per-mode `CLAUDE.md` overlay. `minimal` = Karpathy minimalist; `autopilot` = full pipeline; `native` = platform-first. |
| `runners/mock-solve.sh` | Perfect agent (applies the reference solution) — for plumbing tests. |
| `runners/mock-noop.sh` | Failing agent (no change) — for plumbing tests. |
| `runners/claude.sh` | Real agent: `claude -p` against the task prompt; captures token usage. Gated on the `claude` CLI. |
| `tasks/<id>/` | `task.md` (prompt) · `seed/` (broken start) · `verify.sh` (objective check) · `solution/` (reference fix, used only by mock-solve). |
| `results/` | Run outputs (gitignored content). |

## Add a task

```
tasks/<id>/
  task.md            # the prompt the agent receives
  seed/...           # the broken starting files (copied into the workdir)
  verify.sh          # runs in the workdir; exit 0 = pass; keep it dependency-light
  solution/...       # reference fix (mock-solve copies this in; real runs never see it)
  requires           # optional: commands verify.sh needs, one per line (`a|b` = any-of)
```

## Add a mode

Add a `modes/<name>.md` overlay and a row to `modes/modes.tsv`.

## Status

Plumbing is verified by `test_eval.sh` (mock runners, deterministic). The real
`claude` runner is wired but not exercised here (needs the CLI + auth + budget).
Next: expand the corpus and run `--runner claude` to populate a real scoreboard.
