# Book-classification model benchmark

`make knowledge-benchmark` compares CLIProxyAPI models and reasoning efforts on
the production book-classification prompt. It is the evidence behind the
`MODEL=` / `EFFORT=` defaults of `make knowledge-classify`.

## What it measures

Every cell (model × effort) receives **identical requests**: the classifier's
own system prompt, the same profile catalog (purpose, corpus counts, titles)
and the same bounded, sanitized evidence per book (`scripts/knowledge-classify.py`
`build_evidence` / `build_messages`, precomputed once into a catalog). Only the
request's `model` and top-level `reasoning_effort` differ. Answers go through
the production parser (`parse_verdict`), so malformed, refused and
unknown-profile answers are counted exactly as the classifier would.

Per cell: exact and lenient profile fit (Wilson 95% intervals), new-profile
family fit, false `none` answers, malformed/refused/unknown-profile counts,
calibration (mean confidence when right vs wrong, Brier score), latency
p50/p90/max, completion and reasoning tokens when the proxy returns usage,
requested vs served model, repeat consistency and failure rate (timeouts and
HTTP errors are recorded, never invented, and excluded from fit scores). The
recommendation ranks cells by lenient fit, then exact fit, then failure rate,
then latency, and states the margin to the runner-up and to the baseline cell,
the classifier's current default `gpt-6.1-terra/medium` (v1 results below
benchmarked `gpt-5.6-terra`; 6.1 has not been re-benchmarked yet).

## Rubric

`rubric-v1.json` encodes the operator's review of the real plan
`plan-classify-20261023-090324.json` (67 books: 45 assign, 13 review, 9
existing, 10 profile proposals) and the accompanying ChatGPT review. It covers
the 58 classified sources; the 9 already-imported books are excluded because
duplicate detection is a deterministic content check, not a model decision.
Titles are the library folder names with personalised purchase watermarks
removed; sources are matched to the library by content hash (`source_id`), so
the rubric contains no excerpts and no personal names. Each source carries a
stratum (`clear-existing`, `ambiguous-existing`, `new-profile`, `security`,
`psychology`, `ai`, `business-marketing`, `business-strategy`,
`low-confidence`, `suspicious-text`), the expected answer and a note on the
judgement; `sample: true` marks a 36-source stratified subset for cheaper runs.

Subjective choices (also listed in the rubric):

- psychology consolidates analytical-psychology (same family → both exact);
- security books expect a consolidated security family (ai-security,
  cloud-security, cloud-application-security and security all map to it);
  red-team is lenient only; the existing `security` / `security-reviewer`
  profiles have no book list and so are not classifier candidates;
- AI context / media / ML books may score exact under one broad AI family;
  software-engineering stays exact or acceptable for the engineering-flavoured ones;
- linux-systems-devops is an acceptable family for Linux / Kubernetes / cloud
  operations books, with software-engineering still acceptable;
- biotech-science is conditional: only the biotech family is exact and nothing
  else is lenient;
- `suspicious-text` is an integrity review of the production run, not a model
  score; the benchmark scores profile fit only. (Historical: the classifier no
  longer writes that reason; the stratum name is kept so v1 scores stay
  comparable.)

## Running it

```bash
make knowledge-benchmark                                  # all 58 sources, luna/terra/sol × medium/high, 2 repeats
make knowledge-benchmark SOURCES=stratified REPEATS=1     # the 36-source subset, one pass
make knowledge-benchmark MODELS=gpt-6.1-terra EFFORTS=medium OUT=data/knowledge-staging/benchmarks/smoke
python3 scripts/knowledge-benchmark.py score --rubric docs/benchmarks/knowledge-classify/rubric-v1.json \
    --results <out>/results.json --out <out>/rescored
```

The catalog (`<out>/catalog.json`, or `CATALOG=<file>` to reuse one) carries the
bounded excerpts and stays in the gitignored staging area. `results.json`
carries request metadata and sanitized answers only; `scores.json` and
`report.md` are the aggregates. Nothing under `hermes/profiles`,
`data/knowledge` or `data/knowledge-canonical` is read for writing or written.

## Results

See `results-v1-2026-10-23.md` in this folder for the run that set the current
default, with its limitations.
