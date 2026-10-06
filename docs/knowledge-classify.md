# AI-assisted book classification

`make knowledge-inventory` lists library books that no profile's book list
includes. `make knowledge-classify` asks a model which existing specialist
profile each of those books fits. It shows the model's proposal, confidence
and reason, and the checks that flagged it for review. It assigns a book only
when you pass `APPLY=1`.

The model is treated as unreliable. Fixed rules, not the model, decide what
can be assigned without your approval.

## Fast path

```bash
make knowledge-inventory                       # read-only: what is new, missing or unlisted
make knowledge-classify                        # DRY-RUN: proposals + report, no book list changes
make knowledge-classify SOURCE='edu/Some Book' # one uncategorized book only
make knowledge-classify APPLY=1                # assign proposals that passed every check
make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVE='herbalism=edu/Some Book'
                                               # assign one mapping you approved (no model call)
make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVALS=data/knowledge-staging/reports/<run>.approvals.txt
make knowledge-refresh NAME=<profile>          # sync into the installed profile
# then in `hermes -p <profile> chat`:  /learn ~/.hermes/profiles/<profile>/knowledge
```

`APPLY=1` counts only on the make command line. An exported `APPLY` shell
variable is ignored. The script is `scripts/knowledge-classify.py`. Run it
with `--help` to see every flag.

## What one run does

For each uncategorized source (the same list `make knowledge-inventory` prints;
at most `LIMIT`, default 20, per run):

1. **Quarantine copy-convert.** Text books are copied and EPUB/PDF books are
   converted by the `document-converter` skill into
   `data/knowledge-staging/sources/<collection>/…`. That folder sits outside
   `data/knowledge`, so a quarantined book never syncs into a profile. The
   library is only read.
2. **Bounded evidence.** The model receives the path metadata (library path,
   folder name, title, up to 10 file names, file types, total size) and a
   sanitized excerpt: a heading outline plus one sample taken past the front
   matter. The excerpt is 3000 characters by default and never more than 12000
   (`--excerpt-chars`). Links, URLs, e-mail addresses and control characters
   are stripped. Whole books are never sent.
3. **Privacy gate (fail-closed).** The model-router plugin's deterministic
   detectors (`#private`/`#local` tags, SSN, card numbers, IBAN/ABA, keys,
   secrets, credentials, `MODEL_ROUTER_PRIVATE_TERMS`/`_PATHS`) and
   `hermes/knowledge-private.txt` decide what may leave the host:

   | Finding | Classifier | Sent |
   |---------|-----------|------|
   | Path listed in `knowledge-private.txt`, or private metadata | local model if configured, else **skipped** | to the cloud: nothing |
   | Private excerpt, clean metadata | local model if configured, else CLIProxyAPI | to the cloud: metadata only |
   | Clean | the selected classifier | metadata + excerpt |

   The exact outbound payload is scanned once more right before a cloud call.
   A scanner error counts as private. "Local" means an `OLLAMA_BASE_URL` on a
   loopback address, or on a host you list in `KNOWLEDGE_LOCAL_HOSTS` (for
   example an Ollama on another of your machines over Tailscale). A model on
   any other host is not trusted with private text.
4. **Classify.** The model gets one OpenAI-compatible chat call. By default that
   is CLIProxyAPI at `http://127.0.0.1:${CLIPROXY_PORT}/v1` with
   `gpt-5.6-luna`, the cheapest routine tier (Opus is not needed). It must
   return a single JSON object:
   `{"profile", "confidence", "rationale", "risk", "topics"}`. The book
   evidence is wrapped and labeled as untrusted data.
5. **Checks.** A source gets `needs_review` when any of these holds:

   | Reason | Cause |
   |--------|-------|
   | `classifier-malformed` | Not JSON, a profile not in the list, confidence outside 0–1, no rationale |
   | `classifier-refused` | The model declined |
   | `classifier-error` | Proxy unreachable or HTTP error (exit 3) |
   | `no-profile` | The model answered `none` |
   | `low-confidence` | Confidence below `--threshold` (default 0.75) |
   | `high-risk-profile:<p>` | Proposal is `health-fitness`, `herbalism`, `red-team`, `security` or `security-reviewer` |
   | `high-risk-content:<cat>` | Title or file names match, or the text repeatedly matches, medical, offensive-security or weapons terms |
   | `model-risk:<cat>` | The model itself flagged medical, offensive-security, weapons, self-harm, legal or other risk |
   | `suspicious-text` | The evidence contains prompt-injection phrasing |
   | `private-source`, `private-excerpt`, `metadata-only`, `no-text` | Less evidence, or private content |

6. **Report.** Every source appears on stdout with its status, profile,
   confidence, classifier, how much was sent, privacy labels, rationale, review
   reasons and a ready-to-run approve command. The full JSON report and an
   approvals template are written to `data/knowledge-staging/reports/`.

## Statuses and actions

| Status | Meaning |
|--------|---------|
| `proposed` | Passed every check; `APPLY=1` assigns it |
| `needs-review` | Has a proposal (or a bad answer) that one or more checks flagged; only an explicit approval assigns it |
| `approved` | You approved it with `APPROVE`/`APPROVALS`; the model is not called |
| `skipped` | Nothing was sent (private with no local model, or `CLASSIFIER=none`) |
| `error` | Unsafe input, unreachable classifier, or not an uncategorized source |

Actions: `would-assign` (dry-run), `assigned`, `already-listed`, `refused`,
`failed`, `none`.

## Apply

With `APPLY=1`, a source is assigned when it is `proposed`, or when you
approved it. An approval is your decision and overrides `needs_review`,
including for high-risk profiles. Assigning a source:

1. re-validates the path (inside the library, no `..`, no symlinks, still
   uncategorized) and that the profile is book-backed;
2. **appends** one line to `hermes/profiles/<profile>/knowledge-sources.txt`
   (existing lines are never rewritten);
3. stages that one book into `data/knowledge/<profile>` with
   `scripts/knowledge-stage.sh` (`--no-stage` skips this).

It does not sync into `~/.hermes`, run `/learn` or delete anything. Review
`git diff hermes/profiles`, then run `make knowledge-refresh NAME=<profile>` and
`/learn`.

Approvals use the format `<profile>=<library path>`, one per line in a file
(`#` comments allowed). The approvals template from each run lists every
proposal commented out. Uncomment the lines you accept and change the profile
where needed.

## Settings

| Make / flag | Env | Default | Purpose |
|-------------|-----|---------|---------|
| `APPLY=1` / `--apply` | — | off | Assign (otherwise dry-run) |
| `APPROVE=` / `--approve` | — | — | One `<profile>=<source>` approval |
| `APPROVALS=` / `--approvals` | — | — | File of approvals |
| `APPROVED_ONLY=1` / `--approved-only` | — | off | Approvals only: no staging, no model calls |
| `SOURCE=` / positional | — | all uncategorized | Limit the run to one source |
| `CLASSIFIER=` / `--classifier` | `KNOWLEDGE_CLASSIFIER` | `cliproxy` | `cliproxy`, `local` (Ollama only) or `none` (stage and report, no model) |
| `--model` | `KNOWLEDGE_CLASSIFY_MODEL` | `gpt-5.6-luna` | CLIProxyAPI model |
| `METADATA_ONLY=1` / `--metadata-only` | — | off | Never send an excerpt |
| `--excerpt-chars` | `KNOWLEDGE_EXCERPT_CHARS` | 3000 | Excerpt size, 0–12000 |
| `--threshold` | `KNOWLEDGE_CLASSIFY_THRESHOLD` | 0.75 | Minimum confidence to apply without review |
| `LIMIT=` / `--limit` | `KNOWLEDGE_CLASSIFY_LIMIT` | 20 | Sources classified per run |
| — | `KNOWLEDGE_HIGH_RISK_PROFILES` | see above | Comma list of profiles that always need approval |
| — | `KNOWLEDGE_PRIVATE` | `hermes/knowledge-private.txt` | Library paths that never go to the cloud |
| — | `KNOWLEDGE_STAGING` | `data/knowledge-staging` | Quarantine; must not overlap the library or `KNOWLEDGE_ROOT` |
| — | `KNOWLEDGE_CLASSIFY_BASE_URL` | `http://127.0.0.1:$CLIPROXY_PORT/v1` | Cloud classifier endpoint |
| — | `OLLAMA_BASE_URL`, `OLLAMA_CHAT_MODEL` | `.env` | Local classifier (loopback, or a `KNOWLEDGE_LOCAL_HOSTS` host) |
| — | `KNOWLEDGE_LOCAL_HOSTS` | empty | Comma list of non-loopback hosts you trust as local, e.g. a tailnet Ollama IP |
| — | `BOOKS_ROOT`, `KNOWLEDGE_ROOT`, `PROFILES_DIR`, `KNOWLEDGE_IGNORE`, `DOC2MD` | as in `knowledge-refresh` | Paths |

The CLIProxyAPI key, port and the Ollama and private-term settings come from the
process environment first, then from `.env` through `scripts/envval.sh` (an
allowlist of keys). The key is sent only as the `Authorization` header and is
never printed.

Candidate profiles are folders under `hermes/profiles/` with a
`knowledge-sources.txt` (except `_template`). To make a profile a candidate,
give it a book list first.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every source was handled (`needs_review` is a normal outcome) |
| `3` | A source could not be classified (bad answer, refusal, outage) or assigned |
| `1` | Usage or safety error: unsafe path, unknown profile, conflicting approvals, staging overlaps the library or `KNOWLEDGE_ROOT` |

## Discord prompt template

Paste this to Hermes in Discord after adding books. Fill in the brackets.

```
I added books under /mnt/books. Classify them in the agent-ops-kit repo (dry-run first):
1. Run `make knowledge-inventory` and post the Uncategorized section as it is.
2. Run `make knowledge-classify` [SOURCE='<library path>']. Do not pass APPLY. Post, per book:
   status, proposed profile, confidence, needs_review reasons and rationale, plus the report path and exit code.
3. Do not edit any knowledge-sources.txt and do not touch /mnt/books. Wait for my approval.
4. When I reply with mappings like `herbalism=edu/<book>`, run exactly:
   make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVE='<profile>=<library path>'
   once per mapping, then post `git diff hermes/profiles` and the exit codes.
   [Or, only if I say "apply the clean proposals": make knowledge-classify APPLY=1]
5. Run `make knowledge-refresh NAME=<profile>` for each assigned profile and post the report table.
6. Do not run /learn. List the /learn command for every `updated` profile.
```

## Tests

`make test-knowledge-classify` runs `scripts/tests/test_knowledge_classify.py`
offline. It uses a synthetic read-only library, temp profile seeds, a stub
converter, and stub OpenAI-compatible servers for the cloud and local models.
It covers the dry-run default, bounded evidence, private content never
reaching the cloud stub, malformed, refused, low-confidence and high-risk
answers, apply-only-when-approved, unsafe paths, the Make target and an
unchanged library.
