# Reviewed plan and one-command apply

`make knowledge-classify` writes a **reviewed plan** next to its JSON report.
You edit the plan, then `make knowledge-apply PLAN=<plan> APPROVE=1` does the
whole ingestion in one run. Books are stored **once** in a canonical store and
every profile that uses a book holds a tiny reference instead of a copy.

```bash
make knowledge-inventory                                  # read-only: what is new
make knowledge-classify                                   # proposals + plan-<report>.json (nothing assigned)
$EDITOR data/knowledge-staging/reports/plan-classify-<time>.json
make knowledge-apply PLAN=data/knowledge-staging/reports/plan-classify-<time>.json DRY_RUN=1
make knowledge-apply PLAN=data/knowledge-staging/reports/plan-classify-<time>.json APPROVE=1
make knowledge-learn-status
```

`/mnt/books` is only read. `APPROVE=1` counts only on the make command line.

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

When no profile fits, the classifier may add a proposal under `new_profiles`
(`approved: false`, with the model's one-line purpose). Nothing is created by
classify. To create it, fill every field and approve it:

```json
{"name": "amazon-selling", "approved": true,
 "purpose": "Amazon marketplace selling: listings, FBA logistics, PPC and account health.",
 "in_scope": ["Amazon Seller Central operations", "FBA, listings, advertising"],
 "out_of_scope": ["general business strategy (business profile)", "tax or legal advice"],
 "parent": "business", "overlap": ["business", "offers-sales"],
 "boundaries": ["no purchases or account actions without an explicit ask"]}
```

and set the item's `profile` to that name with `decision: assign`. Names must
match `^[a-z0-9][a-z0-9-]{0,62}$`; a name that already exists is refused (assign
to the existing profile instead). A new profile with no assigned source is
refused too. The profile is created from `hermes/profiles/_template/SOUL.md`
with a Scope section from the spec, an empty book list and
`knowledge-assignments.json` holding the spec; it is a classification candidate
from the next run on.

## What apply does

`scripts/knowledge-apply.py` runs these steps in order; each is idempotent, so
re-running the same plan resumes and verifies rather than repeats:

1. **validate** (nothing written on any refusal): every item decided; assigned
   profiles exist or are approved specs; names safe; every assigned source still
   hashes to `source_sha256` (stale hash -> refuse: re-run classify); unknown
   decisions, actions, plan versions or malformed JSON -> refuse; no `APPROVE=1`
   -> refuse after printing the plan.
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

`make test-knowledge-apply` (`scripts/tests/test_knowledge_apply.py`): refusal
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
