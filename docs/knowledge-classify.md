# AI-assisted book classification

`make knowledge-inventory` lists library books that no profile's book list
includes. `make knowledge-classify` first checks each of them by content
against what is already staged in `data/knowledge/<profile>`; a book whose text
is already there (under any file name or path) is reported as
`already-imported` with its owner profiles and left alone. For the genuinely
new books it asks a model which existing specialist profile each fits. It shows
the model's proposal, confidence and reason, and the checks that flagged it for
review. It assigns a book only when you pass `APPLY=1`.

The model is treated as unreliable. Fixed rules, not the model, decide what
can be assigned without your approval.

## Fast path

```bash
make knowledge-inventory                       # read-only: new, already imported, missing sources
make knowledge-classify                        # DRY-RUN: proposals for new books + report, no book list changes
make knowledge-classify SOURCE='edu/Some Book' # one uncategorized book only
make knowledge-classify APPLY=1                # assign proposals that passed every check
make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVE='herbalism=edu/Some Book'
                                               # assign one mapping you approved (no model call)
make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVALS=data/knowledge-staging/reports/<run>.approvals.txt
make knowledge-refresh NAME=<profile>          # sync into the installed profile
# then in `hermes -p <profile> chat`:  /learn ~/.hermes/profiles/<profile>/knowledge
make knowledge-reconcile                       # PROPOSAL: library sources for staged files without one
make knowledge-reconcile APPLY=1 APPROVE='<profile>=<library path>'   # append that one line
```

`APPLY=1` counts only on the make command line. An exported `APPLY` shell
variable is ignored. The script is `scripts/knowledge-classify.py`. Run it
with `--help` to see every flag.

## What one run does

For each library path that no book list includes:

0. **Content check (no model, no quarantine).** The source's text is
   fingerprinted (bounded, normalized: converter front matter, Markdown markup,
   case, punctuation and whitespace are dropped; EPUB/PDF text comes from the
   converter in a temp folder that is deleted afterwards) and compared with
   every staged `.md`/`.txt` under `data/knowledge/<profile>`. With `exact` or
   `strong` evidence (resemblance ≥ 0.85) for every book file, the source is
   `already-imported`: the report lists every owner profile, the staged file,
   the evidence and the book-list line it came through, and nothing else
   happens (no model call, no quarantine copy, no book-list change, no
   re-ingest, also with `APPLY=1`). Several owners are all listed with
   `needs_review: multiple-owners` and no profile is chosen; link or review
   them with `make knowledge-reconcile`. A source whose files are only partly
   staged continues below but always gets `needs_review: partially-imported`;
   one whose text could not be checked (conversion failed or off, a scanned
   PDF: the check never runs OCR) gets `needs_review: content-unchecked`.
   File names are never evidence: a different book with the same file name,
   another volume of a series or a revised edition is classified normally.

The remaining sources (at most `LIMIT`, default 20, per run) go through:

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
3. **Privacy gate.** `hermes/knowledge-private.txt` and the model-router
   plugin's deterministic detectors (`#private`/`#local` tags, SSN, card
   numbers, IBAN/ABA, keys, secrets, credentials,
   `MODEL_ROUTER_PRIVATE_TERMS`/`_PATHS`) decide what may leave the host.

   **Book-library exemption.** You have authorized the configured CLIProxyAPI
   model to classify the books in your library. When `BOOKS_ROOT` is exactly
   that library (`KNOWLEDGE_LIBRARY_EXEMPT_ROOT`, default `/mnt/books`) and the
   classifier is `cliproxy`, detector hits in a book's metadata or excerpt do
   not keep it local: a book that quotes an example card number, an SSN or a
   key gets the same bounded, sanitized evidence as any other book. The report
   says `book-library exemption used (detector hits: …)` and keeps the labels,
   and the JSON report records `library_exemption: true` per source and for
   the run. A path in `knowledge-private.txt` is never exempt.

   | Finding | Exemption active | Exemption off |
   |---------|------------------|---------------|
   | Path listed in `knowledge-private.txt` | local model if configured, else **skipped**; nothing sent to the cloud | same |
   | Detector hit in the metadata (title, file names, path) | CLIProxyAPI: metadata + excerpt | local model if configured, else **skipped** |
   | Detector hit in the excerpt only | CLIProxyAPI: metadata + excerpt | local model if configured, else CLIProxyAPI with metadata only |
   | Clean | CLIProxyAPI: metadata + excerpt | the selected classifier |

   The exemption is off when `BOOKS_ROOT` is any other folder (also a parent
   or child of the library), with `CLASSIFIER=local`/`none`, with
   `KNOWLEDGE_LIBRARY_EXEMPT_ROOT=off`, or for one run with
   `--no-library-exemption`. It lives only in this script: the model-router
   plugin, its detectors and the routing of your normal tasks are unchanged.
   To keep a book local, list its path in `knowledge-private.txt`.

   The exact outbound payload is scanned once more right before a cloud call
   (with the exemption, everything except the book evidence: instructions and
   profile descriptions). A scanner error counts as private. "Local" means an `OLLAMA_BASE_URL` on a
   loopback address, or on a host you list in `KNOWLEDGE_LOCAL_HOSTS` (for
   example an Ollama on another of your machines over Tailscale). A model on
   any other host is not trusted with private text.
4. **Classify.** The model gets one OpenAI-compatible chat call. By default that
   is CLIProxyAPI at `http://127.0.0.1:${CLIPROXY_PORT}/v1` with
   `gpt-5.6-luna`, the cheapest routine tier (Opus is not needed). Each
   candidate profile is described by its purpose (the `You are …` line of its
   `SOUL.md`) and its current corpus: book-list size, staged file count and up
   to six book titles from its book list. It must return a single JSON object:
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
   | `partially-imported`, `multiple-owners` | Some of its text is already staged; or it is staged in several profiles |
| `content-unchecked` | Its text could not be compared with staged knowledge, so it might already be imported |
| `private-source`, `private-excerpt`, `metadata-only`, `no-text` | Less evidence, or private content (a path in `knowledge-private.txt`, or a detector hit with the exemption off) |

6. **Report.** Every source appears on stdout with its status, profile,
   confidence, classifier, how much was sent, privacy labels (and whether the
   book-library exemption was used), rationale, review
   reasons and a ready-to-run approve command. The full JSON report and an
   approvals template are written to `data/knowledge-staging/reports/`.

## Statuses and actions

| Status | Meaning |
|--------|---------|
| `already-imported` | Its text is already staged; owners are listed, nothing is done (an approval into an owner profile is refused) |
| `proposed` | Passed every check; `APPLY=1` assigns it |
| `needs-review` | Has a proposal (or a bad answer) that one or more checks flagged; only an explicit approval assigns it |
| `approved` | You approved it with `APPROVE`/`APPROVALS`; the model is not called |
| `skipped` | Nothing was sent (in `knowledge-private.txt` or, with the exemption off, private, and no local model; or `CLASSIFIER=none`) |
| `error` | Unsafe input, unreachable classifier, or not an uncategorized source |

Actions: `would-assign` (dry-run), `assigned`, `already-listed`, `refused`,
`failed`, `none`.

## Apply

With `APPLY=1`, a source is assigned when it is `proposed`, or when you
approved it. An approval is your decision and overrides `needs_review`,
including for high-risk profiles. Assigning a source:

1. re-validates the path (inside the library, no `..`, no symlinks, still
   uncategorized), that the profile is book-backed and, for an approval, that
   the profile does not already stage the same text (that would re-ingest it);
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
| — | `KNOWLEDGE_PRIVATE` | `hermes/knowledge-private.txt` | Library paths that never go to the cloud (also under the exemption) |
| `--no-library-exemption` | `KNOWLEDGE_LIBRARY_EXEMPT_ROOT` | `/mnt/books` | The book library whose books CLIProxyAPI may classify despite detector hits; `off` disables |
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

## Reconcile legacy staged knowledge

`make knowledge-reconcile` is proposal-only by default. It reports:

- **Staged without a manifest source**: files under `data/knowledge/<profile>`
  that no line of that profile's book list stages from an existing library
  path (hand-converted copies, renamed or removed library paths). For each it
  lists every library file whose text matches with `exact` or `strong`
  evidence, whether a refresh would update that same staged file or stage a
  second copy, and a ready-to-run approve command. With several candidates it
  says so and chooses none.
- **Already imported, source not linked**: library books no book list includes
  whose text is already staged, with every owner. When the owners' staged files
  already come from a book-list line, nothing needs linking; the report prints
  the `hermes/knowledge-ignore.txt` line that stops listing the copy.

```bash
make knowledge-reconcile                                   # proposal + report, nothing changes
make knowledge-reconcile SCOPE=edu                         # search only part of the library
make knowledge-reconcile APPROVE='<profile>=<library path>'  # check an approval, still nothing changes
make knowledge-reconcile APPLY=1 APPROVE='<profile>=<library path>'
make knowledge-reconcile APPLY=1 APPROVALS=data/knowledge-staging/reports/<run>.approvals.txt
```

`APPLY=1` without an approval is refused. An approval is applied only when
every book file at that library path matches, by content, a staged file of that
profile that has no source; anything weaker is `refused` (exit 3). Applying
appends that one line to the profile's book list. It does not stage, sync,
delete duplicate staged data, run `/learn` or write to the library. Prefer the
candidate marked "refresh updates this same file"; another candidate makes the
next `make knowledge-refresh` stage a second copy, which you would remove by
hand. The JSON report and an approvals template go to
`data/knowledge-staging/reports/reconcile-<time>.*`.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every source was handled (`needs_review` is a normal outcome) |
| `3` | A source could not be classified (bad answer, refusal, outage) or assigned |
| `1` | Usage or safety error: unsafe path, unknown profile, conflicting approvals, staging overlaps the library or `KNOWLEDGE_ROOT` |

## Discord prompt template

Paste this to Hermes in Discord after adding books. Fill in the brackets. Every
change happens in a second message, after you approve the proposal.

```
I added books under /mnt/books. Propose, do not change anything yet (agent-ops-kit repo):
1. Run `make knowledge-inventory` and post the Missing manifest sources, Staged without a manifest source,
   Already imported and Uncategorized sections as they are.
2. Run `make knowledge-classify` [SOURCE='<library path>']. Do not pass APPLY. Post, per book:
   status, owners (for already-imported), proposed profile, confidence, needs_review reasons and rationale,
   whether the book-library exemption was used, plus the report path and exit code. Books from /mnt/books
   may go to the configured CLIProxyAPI model even when their text looks private; only paths in
   hermes/knowledge-private.txt stay local. This applies to book classification only, not to other tasks. Already-imported books are not to be classified, assigned or re-ingested.
3. If anything is staged without a manifest source or flagged multiple-owners, run `make knowledge-reconcile`
   (no APPLY) and post each staged file with its candidates, evidence and approve command.
4. Do not edit any knowledge-sources.txt or knowledge-ignore.txt, do not touch /mnt/books or delete
   anything under data/knowledge. Wait for my approval.
```

After you reply with approvals:

```
Apply exactly these approvals in the agent-ops-kit repo, nothing else:
- classification: for each `<profile>=<library path>` I listed, run
  make knowledge-classify APPLY=1 APPROVED_ONLY=1 APPROVE='<profile>=<library path>'
  [or, only if I say "apply the clean proposals": make knowledge-classify APPLY=1]
- legacy links: for each `link <profile>=<library path>` I listed, run
  make knowledge-reconcile APPLY=1 APPROVE='<profile>=<library path>'
Post `git diff hermes/profiles` and every exit code. Then run `make knowledge-refresh NAME=<profile>` for each
changed profile and post the report table. Do not run /learn; list the /learn command for every `updated` profile.
```

## Tests

`make test-knowledge-classify` runs `scripts/tests/test_knowledge_classify.py`
and `scripts/tests/test_knowledge_match.py` offline. They use a synthetic
read-only library, temp profile seeds, a stub converter, and stub
OpenAI-compatible servers for the cloud and local models. They cover the
dry-run default, bounded evidence, private content never reaching the cloud
stub outside the book-library exemption, a library book with card-number-like
text reaching the cloud stub under the exemption while a `knowledge-private.txt`
path stays local or skipped, the exemption being off for any other root, the
local classifier and `--no-library-exemption`, the model-router detectors still
flagging that text, malformed, refused, low-confidence and high-risk answers,
apply-only-when-approved, unsafe paths, the Make targets and an unchanged
library; plus the content matcher against near collisions (series volume,
revised edition, same file name), EPUB/PDF/text copies of staged text reported
`already-imported` with no model call, quarantine copy, book-list change or
re-ingest (also with `APPLY=1`), every owner listed for multi-profile copies,
profile purpose and corpus context in the prompt, and reconcile being
proposal-only with apply limited to explicit, content-backed approvals.
