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
make knowledge-classify                        # DRY-RUN: proposals + plan + Markdown review + HTML workspace
make knowledge-classify SOURCE='edu/Some Book' # one uncategorized book only
make knowledge-classify MODEL=gpt-5.6-sol EFFORT=high     # another CLIProxyAPI model / reasoning effort
# review: read <report>.review.md, or open <report>.review.html offline, edit decisions there and
# export the revised plan (or edit plan-<report>.json directly); then one command applies it:
make knowledge-apply PLAN=data/knowledge-staging/reports/plan-<report>.json DRY_RUN=1
make knowledge-apply PLAN=data/knowledge-staging/reports/plan-<report>.json APPROVE=1
make knowledge-learn-status
make knowledge-reconcile                       # PROPOSAL: library sources for staged files without one
make knowledge-reconcile APPLY=1 APPROVE='<profile>=<library path>'   # append that one line
```

Legacy path (profile-local copy, no plan; still works):

```bash
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
   `gpt-6.1-terra` at reasoning effort `medium`, the default Terra model (the
   v1 benchmark chose `gpt-5.6-terra/medium`; Opus is not needed). Each
   candidate profile is described by its purpose (the `You are …` line of its
   `SOUL.md`) and its current corpus: book-list size, staged file count and up
   to six book titles from its book list. It must return a single JSON object:
   `{"profile", "confidence", "rationale", "topics"}`. The book evidence is
   wrapped and labeled as untrusted data: it is sanitized, bounded and
   delimited, and is never treated as instructions. The prompt asks for fit only: the
   subject matter is never a reason to withhold or lower a match, and a
   legacy `risk` field in an answer is ignored, never stored.
5. **Checks.** A source gets `needs_review` when any of these holds:

   | Reason | Cause |
   |--------|-------|
   | `classifier-malformed` | Not JSON, a profile not in the list, confidence outside 0–1, no rationale |
   | `classifier-refused` | The model declined |
   | `classifier-error` | Proxy unreachable or HTTP error (exit 3) |
   | `no-profile` | The model answered `none` |
   | `low-confidence` | Confidence below `--threshold` (default 0.75) |
   | `partially-imported`, `multiple-owners` | Some of its text is already staged; or it is staged in several profiles |
| `content-unchecked` | Its text could not be compared with staged knowledge, so it might already be imported |
| `private-source`, `private-excerpt`, `metadata-only`, `no-text` | Less evidence, or private content (a path in `knowledge-private.txt`, or a detector hit with the exemption off) |

   No reason is a risk label. There is no high-risk profile list, no medical,
   security or weapons term matching and no model risk field: a confident
   `herbalism` or `red-team` proposal is a clean `assign` like any other, and
   the classifier never prints, stores or gates on a risk judgement. Earlier
   releases wrote `high-risk-profile:*`, `high-risk-content:*` and
   `model-risk:*`; see "Plans from earlier releases" below for what happens to
   a plan that still carries one.

   Instruction-like text in a curated book is not a reason either. AI and
   security books quote prompts ("ignore previous instructions", JSON with a
   `profile` field...); that text is content, classified like any other book.
   It stays untrusted data: sanitized, bounded to the excerpt, inside the
   evidence delimiters (which the book cannot close), and the verdict is the
   strictly validated model answer, never JSON found in the book. Privacy
   controls, the outbound payload check and source path/hash checks are
   unchanged. Earlier releases flagged it as `suspicious-text`; see "Plans
   from earlier releases".

6. **Report.** Every source appears on stdout with its status, profile,
   confidence, classifier (model and effort, the model the proxy reports it
   served, latency), how much was sent, privacy labels (and whether the
   book-library exemption was used), rationale and review reasons. Under
   `data/knowledge-staging/reports/` the run writes the JSON report, the
   reviewed plan `plan-<report>.json`, a Markdown review `<report>.review.md`,
   an offline HTML workspace `<report>.review.html` and the legacy approvals
   template; stdout ends with their paths and the exact next commands
   (reviewed-plan flow first, legacy `APPLY=1` labelled separately). See
   "Review artifacts" below.

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

## Reviewed plan (recommended)

Every run also writes `plan-<report>.json`: one editable item per source with
its content hash, the proposal and a `decision` (`assign` for guardrail-clean
proposals, `existing` for text already staged, `review` otherwise), plus any
new-profile proposal the model made (`new_profiles`, `approved: false`; nothing
is created by classify). Decide every item, approve any new profile spec, then
one command assigns, stores each book once, writes references, installs and
learns:

```bash
make knowledge-apply PLAN=plan-<report>.json DRY_RUN=1
make knowledge-apply PLAN=plan-<report>.json APPROVE=1
```

See [knowledge-apply.md](knowledge-apply.md).

### Review artifacts

Next to the report and plan every run writes two derived, read-only views
(`scripts/knowledge_review.py`; the plan JSON stays the authority):

- `<report>.review.md` — totals, assignments per profile, every new-profile
  proposal with its spec, then every source in original order with status,
  decision, existing owners, proposed and selected profile, confidence, review
  reasons, the classifier's rationale and request metadata, the source id and
  any new-profile spec. **Classifier facts** (what the model and guardrails
  produced) are kept apart from **Review guidance** (deterministic hints
  derived from the status and flags: "accept if scope matches", "resolve the
  review flags", "existing book with several owners"...). An item that still
  carries `suspicious-text` (an older plan) is pointed at regeneration or a
  hand decision. New-profile proposals carry the consolidation defaults
  below.
- `<report>.review.html` — a self-contained decision workspace that works
  offline (no external JavaScript, CSS or fonts; no book text; no secrets):
  search, filters by status / decision / profile, a readable panel per item
  and per new-profile proposal, client-side editing of each item's decision
  and profile and each proposal's `approved` flag and spec fields, and
  **Export revised plan** which downloads a plan JSON that is a copy of the
  original with only those fields changed (schema, `source_id`, `source_sha256`
  and every other field are preserved verbatim; invalid decisions or names
  are refused). Save it next to the original (or over it) and run
  `make knowledge-apply PLAN=<file> DRY_RUN=1`. Hostile titles or rationales
  cannot execute: data is embedded as `\u`-escaped JSON and rendered through
  text nodes only.

`python3 scripts/knowledge_review.py <report.json>` re-renders both files from
an existing report and its plan.

### Taxonomy: broad parents first (enforced on every run)

Rerun the same command and the proposals come back consolidated:

```bash
make knowledge-classify LIMIT=100
```

Two layers enforce the policy; neither creates a profile.

**1. Prompt policy.** The classifier is told to choose an existing broad
profile whenever it can reasonably own the book (the subtopic goes into the
answer's `tags`), to answer `none` only when no existing profile fits, to
propose only broad parent-level families (`psychology`, `blue-team-security`;
never `depth-psychology` or `blue-team-incident-response`), to put
subcategories in `tags` rather than in the name, and never to propose several
overlapping new profiles.

**2. Deterministic consolidation** (`scripts/knowledge_taxonomy.py`), applied
after parsing and before the plan, report and review are written. Each
proposed new-profile name is resolved in this order:

| Rule | Example | Result |
|------|---------|--------|
| `family-alias`: listed as an alias in the family map | `depth-psychology` | folds into `psychology` |
| `existing-profile`: `<existing profile>-<child>` | `red-team-web-apps` | assigned to `red-team` |
| `family-prefix`: `<family>-<child>` | `psychology-cognitive` | folds into `psychology` |
| `sibling-prefix`: `<another proposal in this run>-<child>` | `machine-learning-ai` next to `machine-learning` | folds into `machine-learning` |
| none of the above | `biotechnology-genetics` alone | preserved verbatim |

Only dash-separated children fold (`businesslike-writing` is not a child of
`business`); `_template` never captures anything. Folded items keep their
spec fields, point at the canonical name (`profile`, `new_profile_suggestion`,
`new-profile-proposal:<canonical>`), record the original under `taxonomy`
(`proposed`, `canonical`, `rule`) and the review reason
`taxonomy-consolidated:<proposed>-><canonical>`, and carry the narrower
subject as plan `tags`. The plan then holds **one proposal per family** with
`aliases` (the names folded into it), `tags` (their subjects plus the
family's own), the union of every member's spec fields (the first complete
value wins; `overlap`, `tags`, `aliases` are unioned) and a `parent` default
from the family map when the model gave none. Spec completion runs once per
canonical family, never per alias.

When the canonical family **already exists** as a profile (book-backed or
installed under `hermes/profiles/`), the item is assigned to it instead of a
new proposal: `profile` is set, `new_profile` cleared, review reason
`taxonomy-existing-profile:<proposed>-><existing>`. The decision stays
`review`: a deterministic remap is confirmed by the operator, never applied
by `APPLY=1` or `APPROVE_ALL`.

The family map is explicit configuration in
[`hermes/knowledge-taxonomy.json`](../hermes/knowledge-taxonomy.json)
(`families.<canonical>.aliases|tags|parent`); `KNOWLEDGE_TAXONOMY=<path>`
points at another file, `KNOWLEDGE_TAXONOMY=off` keeps only the structural
rules (existing-profile and sibling prefixes). A missing or invalid file is
an error, never a silent default. The committed map covers `psychology`
(`depth-psychology`, `psychology-mental-health`, `psychology-personality`,
`psychology-psychotherapy`, ...) and `blue-team-security`
(`blue-team-incident-response`, `blue-team-security-operations`, ...).

Every artifact says what happened: the plan and the report carry `taxonomy`
(`applied`, `taxonomy_file`, `families.<canonical>.{existing, aliases,
sources, tags, rules}`), the terminal summary prints
`taxonomy: consolidation applied` with `<canonical> <- <aliases>` lines, and
the Markdown review and HTML workspace list the aliases grouped under each
canonical proposal ("Consolidation applied"). Consolidation is idempotent: a
rerun with the same answers yields the same plan. No reason it writes is a
risk label, and it never reads book text.

Defaults when reviewing what is left (unchanged):

* prefer a broad parent profile (an existing one, or the one new broad
  profile per family) and keep the narrower subject as item `tags` or as
  aliases in the parent's `in_scope`;
* do not create a narrow sibling profile on the strength of one book;
* if two canonical proposals still overlap, approve at most one, point the
  other items at it (`profile`, decision `assign`), and leave the rest
  `approved: false` (an unapproved proposal is never created). `APPROVE_ALL`
  approves any complete proposal a `review` item still points at, so decide
  those items first;
* a single-book topic with no fitting parent can wait: `skip` it this run
  rather than add a profile.

### Plans from earlier releases

Earlier releases labelled medical, security and weapons topics
(`high-risk-profile:*`, `high-risk-content:*`, `model-risk:*` review reasons)
and gated them behind an approval, and flagged instruction-like book text as
`suspicious-text`. None of these reasons exists any more and a new run never
writes them (`knowledge_plan.RETIRED_REASONS`, `LEGACY_RISK_PREFIXES`). Nothing on disk is edited retroactively:

* books already assigned (book lists, `knowledge-assignments.json`, canonical
  copies, learned skills) stay as they are; nothing needs to be redone;
* a plan item the operator already decided (`assign`, `skip`, `existing`)
  applies as written; an old label in its `review_reasons` is history only;
* a plan item still at `review` that carries one of those reasons is reported
  as **pending stale** by `make knowledge-apply ... DRY_RUN=1`. `APPROVE_ALL`
  and `SKIP_ERRORS` leave it alone and an apply refuses while it is there,
  because the gate that produced it is gone and its proposal was never judged
  without it.

Re-classification is only required for those stale items. Workflow:

```bash
make knowledge-apply PLAN=plan-<old>.json DRY_RUN=1   # lists "pending stale" items, if any
make knowledge-classify                                # fresh plan: the stale books are still uncategorized,
                                                       # so they are classified again, without those reasons
make knowledge-apply PLAN=plan-<new>.json DRY_RUN=1
make knowledge-apply PLAN=plan-<new>.json APPROVE=1 [APPROVE_ALL=1]
```

Books already assigned through the old plan are `already-imported` in the
new run and are not sent to the model again. Alternatively, set each stale
item's `decision` by hand (`assign` with a `profile`, `skip` or `existing`)
and re-run the apply; the old plan is never rewritten by a tool.

## Apply (legacy, profile-local copy)

With `APPLY=1`, a source is assigned when it is `proposed`, or when you
approved it. An approval is your decision and overrides `needs_review`.
Assigning a source:

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

### One canonical profile per book

Each book belongs to one profile by default. An approval for a book whose text
is already staged in another profile is `refused` (exit 3) with the command to
use instead. A second (secondary) profile is allowed only by explicit approval,
and only when it serves a substantial, recurring, independent purpose, not
because the book is merely relevant:

```bash
make knowledge-classify APPLY=1 APPROVED_ONLY=1 SECONDARY='<profile>=<library path>' \
  SECONDARY_REASON='<the recurring purpose this profile has for the book>'
```

`SECONDARY_REASON` is required (at least 20 characters) and is recorded in the
JSON report and printed with the item. Secondary approvals are command-line
only, never read from an approvals file. Books already in several book lists
are reported by `make knowledge-reconcile` and never changed or removed; which
one stays canonical is your decision, made by editing the book lists yourself.

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
| `SECONDARY=` / `--secondary` | — | — | One `<profile>=<source>` approval as a second owner of a book another profile has |
| `SECONDARY_REASON=` / `--secondary-reason` | — | — | Required with `SECONDARY`: the substantial, recurring, independent purpose |
| `SOURCE=` / positional | — | all uncategorized | Limit the run to one source |
| `CLASSIFIER=` / `--classifier` | `KNOWLEDGE_CLASSIFIER` | `cliproxy` | `cliproxy`, `local` (Ollama only) or `none` (stage and report, no model) |
| `MODEL=` / `--model` | `KNOWLEDGE_CLASSIFY_MODEL` | `gpt-6.1-terra` | CLIProxyAPI model (v1 benchmark chose `gpt-5.6-terra`: see "Choosing the model and effort") |
| `EFFORT=` / `--effort` | `KNOWLEDGE_CLASSIFY_EFFORT` | `medium` | CLIProxyAPI reasoning effort, sent as top-level `reasoning_effort` on every classification and spec-completion request (`medium` or `high`; `minimal`/`low` accepted for compatibility). Not sent to the local model, whose options are its own; the report says so (`effort_note`) |
| `METADATA_ONLY=1` / `--metadata-only` | — | off | Never send an excerpt |
| `--excerpt-chars` | `KNOWLEDGE_EXCERPT_CHARS` | 3000 | Excerpt size, 0–12000 |
| `--threshold` | `KNOWLEDGE_CLASSIFY_THRESHOLD` | 0.75 | Minimum confidence to apply without review |
| `LIMIT=` / `--limit` | `KNOWLEDGE_CLASSIFY_LIMIT` | 20 | Sources classified per run |
| — | `KNOWLEDGE_TAXONOMY` | `hermes/knowledge-taxonomy.json` | Family map for proposal consolidation (see "Taxonomy"); `off` keeps only the structural rules |
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

Each item records `model_requested`, `effort_requested`, `model_served` (the
model name the proxy returned), `usage` (prompt / completion / reasoning
tokens when the proxy returns them) and `latency_s`; the run header records
`model`, `effort` and `models_served`. No key or URL is written.

### Choosing the model and effort

`make knowledge-benchmark` compares CLIProxyAPI models and efforts on the exact
production prompt and evidence against a versioned, operator-reviewed rubric
(`docs/benchmarks/knowledge-classify/`). It never writes a plan, a book list or
a profile; results go under `OUT` (default: a timestamped folder in the
gitignored staging area). See
[benchmarks/knowledge-classify/README.md](benchmarks/knowledge-classify/README.md)
for the rubric, the scoring and the latest results. The v1 results chose
`gpt-5.6-terra/medium`; the default moved to `gpt-6.1-terra` with the kit's
Terra default and has not been re-benchmarked yet.

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
- **Listed in several book lists**: book-list lines more than one profile's
  list covers. Policy is one canonical profile per book; these are reported for
  your decision and never changed or removed.

```bash
make knowledge-reconcile                                   # proposal + report, nothing changes
make knowledge-reconcile SCOPE=edu                         # search only part of the library
make knowledge-reconcile APPROVE='<profile>=<library path>'  # check an approval, still nothing changes
make knowledge-reconcile APPLY=1 APPROVE='<profile>=<library path>'
make knowledge-reconcile APPLY=1 APPROVALS=data/knowledge-staging/reports/<run>.approvals.txt
```

`APPLY=1` without an approval is refused. An approval is applied only when
every book file at that library path matches, by content, a staged file of that
profile that has no source; anything weaker is `refused` (exit 3). When another
profile already lists that path or stages its text, the approval is also
refused unless given as `SECONDARY='<profile>=<library path>'` with
`SECONDARY_REASON` (see One canonical profile per book). Applying
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

`make test-knowledge-classify` runs `scripts/tests/test_knowledge_classify.py`,
`scripts/tests/test_knowledge_review.py` (Markdown review and HTML workspace:
escaping of hostile titles and rationales, no external assets, no book text,
and the export function run in node when installed, proving only decision /
profile / approval / spec fields change), `scripts/tests/test_knowledge_benchmark.py`
(rubric validation, catalog from a synthetic library by content hash, identical
prompts per cell, recorded failures, scorer metrics, Make target) and
`scripts/tests/test_knowledge_match.py` and `scripts/tests/test_knowledge_taxonomy.py`
(family map loading and validation, resolution rules, one proposal per family,
source remapping, existing-profile preference, unknown-family preservation,
idempotence, spec enrichment and the prompt policy text) offline. The classify
tests also prove the taxonomy layer end to end (plan, report, terminal summary,
Markdown and HTML review metadata, `KNOWLEDGE_TAXONOMY` override and `off`) and
that `--effort` / `EFFORT=` / `KNOWLEDGE_CLASSIFY_EFFORT` reach every cloud
request as `reasoning_effort` (and never the local model), that the requested
model, effort, served model, token usage and latency are recorded without
secrets, and that the review artifacts are written and announced. They use a synthetic
read-only library, temp profile seeds, a stub converter, and stub
OpenAI-compatible servers for the cloud and local models. They cover the
dry-run default, bounded evidence, private content never reaching the cloud
stub outside the book-library exemption, a library book with card-number-like
text reaching the cloud stub under the exemption while a `knowledge-private.txt`
path stays local or skipped, the exemption being off for any other root, the
local classifier and `--no-library-exemption`, the model-router detectors still
flagging that text, malformed, refused and low-confidence answers, no risk
label for medical or security topics (a confident `herbalism` proposal is a
clean assign; the old `KNOWLEDGE_HIGH_RISK_PROFILES` knob is ignored),
apply-only-when-approved, unsafe paths, the Make targets and an unchanged
library; plus the content matcher against near collisions (series volume,
revised edition, same file name), EPUB/PDF/text copies of staged text reported
`already-imported` with no model call, quarantine copy, book-list change or
re-ingest (also with `APPLY=1`), every owner listed for multi-profile copies,
profile purpose and corpus context in the prompt, and reconcile being
proposal-only with apply limited to explicit, content-backed approvals.

## New-profile proposals are complete specs

When no profile fits, the classifier returns a complete proposal (`purpose`,
`in_scope`, `out_of_scope`, `parent` or `none`, `overlap`, `boundaries`), validated
field by field (unknown parent/overlap names are dropped, lists are bounded). A
proposal the first answers left incomplete gets one bounded completion call per
name (name, purpose, book titles, profile list; never book text) on the backend
that classified its items. The plan records `spec_status` (`complete` or
`incomplete:<fields>`); it is a proposal (`approved: false`) until
`make knowledge-apply ... APPROVE_ALL=1` or a hand approval. See
[knowledge-apply.md](knowledge-apply.md). Overlapping proposals are folded
into one broad family per run before the plan is written (see "Taxonomy:
broad parents first"); the proposal records `aliases` and `tags`.
