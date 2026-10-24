# Reviewed plan and one-command apply

`make knowledge-classify` writes a **reviewed plan** next to its JSON report.
You edit the plan, then `make knowledge-apply PLAN=<plan> APPROVE=1` does the
whole ingestion in one run. Books are stored **once** in a canonical store and
every profile that uses a book holds a tiny reference instead of a copy.

```bash
make knowledge-inventory                                  # read-only: what is new
make knowledge-classify                                   # proposals + plan-<report>.json (nothing assigned)
PLAN=data/knowledge-staging/reports/plan-classify-<time>.json
make knowledge-apply PLAN=$PLAN DRY_RUN=1                 # report: ready / pending review / pending profile approval / pending error
make knowledge-apply PLAN=$PLAN DRY_RUN=1 APPROVE=1 APPROVE_ALL=1 [SKIP_ERRORS=1]   # preview the bulk approval; prints APPROVE_ALL=<plan id>
make knowledge-apply PLAN=$PLAN APPROVE=1 APPROVE_ALL=<plan id> [SKIP_ERRORS=1]     # one command: approve, create profiles, store books once, reference, install, learn
make knowledge-learn-status
```

`/mnt/books` is only read. `APPROVE=1` and `APPROVE_ALL` count only on the make
command line. Editing the plan by hand still works (`APPROVE=1` alone applies it
exactly as written).

## Dry-run states

`DRY_RUN=1` on an unedited classifier plan is a report, never a wall of errors.
Every item lands in one state:

| state | meaning | what moves it |
|---|---|---|
| `ready` | `assign` to an existing profile or an approved new profile, `skip`, `existing` | nothing; it runs on apply |
| `pending review` | `review` with a concrete existing-profile proposal (`proposed_profile`), e.g. low confidence | `APPROVE_ALL=1`, or set `decision` by hand |
| `pending profile approval` | item tied to a `new_profiles` proposal that is not approved, or whose spec is incomplete | `APPROVE_ALL=1` when the spec is complete; otherwise fill the spec (or regenerate the plan) |
| `pending error` | classifier error, no target, source that could not be hashed | `SKIP_ERRORS=1` (skip this run) or decide it by hand |
| `pending stale` | `review` that still carries a retired reason from an earlier release (`high-risk-content:*`, `high-risk-profile:*`, `model-risk:*`, `suspicious-text`) | regenerate the plan with `make knowledge-classify`, or decide it by hand; never `APPROVE_ALL` or `SKIP_ERRORS` |

Only structural problems are errors (exit 1, nothing written): stale source
hashes, unknown decisions or actions, unsafe names, malformed JSON, a hand-set
profile that does not exist. Pending items make an actual apply refuse (exit 1,
nothing written) and print the exact next command. Dry-run prints one line per
pending item; `VERBOSE=1` adds review reasons, missing spec fields and sources.

## Bulk approval (`APPROVE_ALL`)

`APPROVE_ALL=1` is the operator's explicit acceptance of the classifier's
concrete proposals. It works on a copy of the plan; the file is never edited.

* clean `assign` stays `assign`; `skip` and `existing` stay as written;
* `pending review` with an existing-profile proposal becomes `assign` to it;
* `pending stale` (a risk label or `suspicious-text` from an earlier release)
  is left alone: the classifier no longer judges topics or gates on
  instruction-like book text, so the plan is regenerated rather than
  re-interpreted (see "Plans from earlier releases" in
  [knowledge-classify.md](knowledge-classify.md));
* `pending profile approval` with a **complete** proposal becomes `assign` and
  the proposal becomes approved (the profile is created). An incomplete
  proposal stays pending: nothing is guessed;
* `pending error` stays pending and blocks the apply unless `SKIP_ERRORS=1`
  turns those items into `skip` for this run.

Preview with `DRY_RUN=1`. The preview prints `APPROVE_ALL=<plan id>` (the
16-hex content hash of the plan file); passing that id to the apply binds the
approval to exactly the previewed file and refuses if the file changed since
(`APPROVE_ALL=1` approves the file as it is now). The apply writes
`data/knowledge-canonical/apply/<plan id>/approval.json`: plan id, file sha256,
policy flags, every decision the policy changed, the new profiles it approved
and `approval_digest`, a digest of the whole approved decision set (also in
`report.json`). Re-running the same command is idempotent and keeps one receipt.

## The plan

`plan-<report>.json` (`plan_version: 1`) has one item per source and a list of
new-profile proposals:

| field | meaning |
|---|---|
| `source`, `source_id`, `source_sha256` | library path and its **content** identity (sha256 over the book files; two library copies of the same bytes share one id) |
| `status`, `proposed_profile`, `confidence`, `rationale`, `risk`, `review_reasons`, `owners` | what classify found (read-only context) |
| `decision` | `assign`, `skip`, `existing` are terminal; `review` blocks apply |
| `profile` | target profile for `assign` (existing, or an approved entry in `new_profiles`) |
| `purpose`, `chapters`, `tags` | profile-specific metadata, kept in `knowledge-assignments.json` and the reference |
| `secondary_reason` | required when the same source is assigned to a second profile (one canonical profile per book) |

Defaults: guardrail-clean proposals start as `assign`; `needs-review`,
`no-profile` and errors start as `review`; text already staged somewhere starts
as `existing`. Every `review` must become `assign`, `skip` or `existing` by hand.

### New profiles

When no profile fits, the classifier adds a complete proposal under
`new_profiles` (`approved: false`): `purpose`, `in_scope`, `out_of_scope`,
`parent` (an existing profile or `none`), `overlap`, `boundaries`, `sources`
and `spec_status` (`complete`, or `incomplete:<fields>` when the model's answers
did not fill a field). The spec comes from the classification answer, completed
by at most one bounded follow-up call per proposal name (name, purpose, book
titles and the profile list; never book text). Nothing is created by classify.
Review and edit the spec, then either `APPROVE_ALL=1` (approves every complete
proposal) or approve it by hand:

```json
{"name": "amazon-selling", "approved": true,
 "purpose": "Amazon marketplace selling: listings, FBA logistics, PPC and account health.",
 "in_scope": ["Amazon Seller Central operations", "FBA, listings, advertising"],
 "out_of_scope": ["general business strategy (business profile)", "tax or legal advice"],
 "parent": "business", "overlap": ["business", "offers-sales"],
 "boundaries": ["no purchases or account actions without an explicit ask"]}
```

and set the item's `profile` to that name with `decision: assign`. Plans written
before complete specs existed carry proposals with empty lists and no
`spec_status`; dry-run reports them as pending and says to regenerate the plan
(`make knowledge-classify`) or fill the fields in by hand. Names must
match `^[a-z0-9][a-z0-9-]{0,62}$`; a name that already exists is refused (assign
to the existing profile instead). A new profile with no assigned source is
refused too. The profile is created from `hermes/profiles/_template/SOUL.md`
with a Scope section from the spec, an empty book list and
`knowledge-assignments.json` holding the spec; it is a classification candidate
from the next run on.

## What apply does

`scripts/knowledge-apply.py` runs these steps in order; each is idempotent, so
re-running the same plan resumes and verifies rather than repeats:

1. **validate** (nothing written on any refusal): resolve the states (and the
   `APPROVE_ALL` / `SKIP_ERRORS` policy) on a copy of the plan; every executable
   item decided; assigned profiles exist or are approved, complete specs; names
   safe; every assigned source still hashes to `source_sha256` (stale hash ->
   refuse: re-run classify); unknown decisions, actions, plan versions or
   malformed JSON -> refuse; pending items -> refuse with the next command; no
   `APPROVE=1` -> refuse after printing the summary. Then the approval receipt.
2. **profiles**: create approved new profiles under `hermes/profiles/`.
3. **assignments**: append the line to `knowledge-sources.txt` (never
   rewritten) and upsert `hermes/profiles/<p>/knowledge-assignments.json`
   (`source`, `source_id`, `purpose`, `chapters`, `tags`, `secondary_reason`,
   `plan_id`).
4. **canonical**: copy `.md/.txt` or convert EPUB/PDF (document-converter)
   **once** into `$KNOWLEDGE_CANONICAL/sources/<source_id>/` (default
   `data/knowledge-canonical`, gitignored) with a `source.json` (files, hashes,
   every library path that maps to it). Present and verified copies are reused.
5. **references**: one `data/knowledge/<p>/refs/<name>.ref.md` per canonical
   file: front matter with `source`, `source_id`, `canonical_root`,
   `canonical_rel`, `sha256`, `profile`, `purpose`, `chapters`, `tags`. No book
   text, no symlinks.
6. **install**: installed profiles get `knowledge-sync.sh`; new ones
   `hermes-profile.sh` (creates the Hermes profile and syncs). `INSTALL=0` stops
   before this step.
7. **learn**: `scripts/knowledge-learn.py <profiles>`; a reference is learned
   from the canonical file after the path (inside the canonical root, regular
   file) and sha256 validate. Invalid references are reported, never learned.
   `LEARN=0` skips this step.
8. **report**: `data/knowledge-canonical/apply/<plan id>/report.json` and
   `checkpoint.json`.

Exit 0 when complete; 1 when refused; 3 when a step failed. On failure the run
stops, keeps finished steps, and prints `Resume: make knowledge-apply PLAN=...
APPROVE=1`. A changed plan file is a different plan id with its own checkpoint.

## Shared sources

Many profiles may reference one canonical copy. The second owner of a book
needs `secondary_reason` in its item (substantial, recurring, independent
purpose). Removing an assignment (delete the book-list line and the entry in
`knowledge-assignments.json`, then `knowledge-refresh` with
`KNOWLEDGE_SYNC_MODE=mirror`) never deletes the canonical copy; there is no
garbage collection.

## Compatibility with profile-local corpora

* `knowledge-stage.sh` / `knowledge-refresh` skip manifest entries that
  `knowledge-assignments.json` marks as canonical and count them as
  "referenced, not copied"; other entries stage as before.
* Legacy full copies under `data/knowledge/<p>` keep working and keep being
  learned. Nothing migrates or deletes them. The apply report lists
  `legacy_duplicates` (profile-local files whose bytes equal a canonical file)
  and `make knowledge-legacy-report PLAN=<any plan>` prints the same list
  read-only. Remove a duplicate by hand after its reference has learned.
* The content matcher ignores `refs/*.ref.md`, so references never count as
  staged book text.
* `hermes-profile.sh` honours `PROFILES_DIR`, `HERMES_HOME` and `HERMES_BIN`
  (tests use a stub; defaults unchanged).

## Tests

`make test-knowledge-apply` (`scripts/tests/test_knowledge_apply.py` and
`test_knowledge_approve_all.py`). Bulk approval: an unedited plan dry-runs with
exit 0, the four states and no write; `VERBOSE=1`; an apply with pending items
refuses before any write; stale hashes stay errors; `APPROVE_ALL` preview shows
the changes and the bound plan id; incomplete specs and errors are never
guessed; `SKIP_ERRORS=1` applies everything (low-confidence proposal accepted,
profile created from a complete spec, receipt with digest, idempotent re-run);
legacy risk labels and `suspicious-text` make a `review` item pending stale, untouched by
`APPROVE_ALL`/`SKIP_ERRORS`, while a hand-decided item applies;
a changed file is refused under the bound id; hand edits and terminal decisions
survive; `resolve_plan` semantics. Classifier side
(`test_knowledge_classify.py`): complete specs in the plan, one bounded
completion call per proposal, off-schema completion stays pending. The
apply tests also cover: refusal
without approval, unresolved review items, stale hashes, bad decisions /
profile names / plan versions / actions, unapproved or incomplete new-profile
specs; dry-run writes nothing; the full run (assign, canonical once, references,
sync and create through a stub hermes, learn on the canonical path, report);
identical bytes under two paths -> one canonical copy, many references;
approved profile creation and candidate listing; failed learn -> exit 3 + resume
-> idempotent rerun; canonical copies survive removed assignments; legacy copies
still learn and are only reported; `knowledge-stage.sh` skips canonical
entries; references are tiny and resolve through validation. All offline;
`/mnt/books`, real profiles and `~/.hermes` are never touched.
