# Capability registry (Milestone A: source only)

A small, versioned catalog of reusable methods and their contracts, so Hephaestus
can pick the smallest useful set of capabilities for a task. It is **data**: it
does not schedule, dispatch, install, execute, open network connections, or grant
tools or permissions. Specialist selection stays in the `model-router` plugin;
work state stays in Kanban; code changes stay on the existing coordinator → Claude
route.

**Status:** source implemented and tested in a development checkout. Nothing is
installed, nothing is runtime-accepted, and no company isolation is enforced.

## Files

| Path | What it is |
|------|------------|
| `hermes/capabilities/registry.json` | 16 namespaced capability records (`kit.`, `pm.`, `gstack.`, `existing.`) |
| `hermes/capabilities/upstreams.lock.json` | Exact revisions, per-file hashes, license/notice scope, update/rollback procedure; hashes copied from the verified 2026-11-04 research lock |
| `hermes/capabilities/schemas/` | Draft 2020-12 schemas: registry, upstreams-lock, task-envelope, artifact, evidence, capability-result, selection, plan |
| `hermes/companies/schema.json`, `hermes/projects/schema.json` | Company/project manifest contracts; `examples/` holds two synthetic tenants (`example-a`, `example-b`) |
| `scripts/capability_registry.py` | Pure library + read-only CLI: strict JSON, validation, readiness, planning |
| `scripts/capability-plan.py` | Read-only plan emitter |
| `scripts/capability_workflow.py`, `scripts/capability_design.py`, `scripts/capability_methods.py` | Workflow validator/renderer; design helpers (contrast, static HTML/CSS/token audit, offline preview builder); gstack method helpers (redaction gate, dependency cycles, PR type, unified-diff and command-capture parsing) |
| `scripts/requirements-capabilities.txt` | `jsonschema>=4.10.3,<5` (Ubuntu: `python3-jsonschema`). Without it validation fails closed |
| `scripts/capability_selection.py`, `scripts/capability-select.py` | D: current-task selection through the model-router seam (`--live` or `--fixture`), read-only |
| `hermes/capabilities/routing-recipes.json`, `hermes/capabilities/evals/routing/fixture-cases.json` | D: smallest-set recipes (classifier examples + evaluation expectations); FIXTURE routing cases |
| `scripts/capability_compat.py` | D: read-only installed-Hermes skill compatibility (source parsed as text) |
| `scripts/capability_activation.py`, `hermes/capabilities/activation/manifest.json` | E: exact selected-file manifest, bundle, rehearsal-only apply/rollback ([capability-activation.md](capability-activation.md)) |

## Commands

```bash
make capability-validate                       # registry, lock, schemas, synthetic manifests
python3 scripts/capability_registry.py list    # milestone, lifecycle, computed "defined"
python3 scripts/capability_registry.py readiness --id existing.engineering-path
python3 scripts/capability_registry.py verify-lock --research-lock <research>/upstreams.lock.json
make capability-plan TASK=task.json CAPS="pm.lean-prd" FORMAT=summary
make capability-select TASK=task.json TEXT=prompt.txt FIXTURE=hermes/capabilities/evals/routing/fixture-cases.json
make capability-select TASK=task.json TEXT=prompt.txt LIVE=1     # configured classifier; needs its key
make capability-compat                                            # read-only, installed Hermes
make capability-activation-check                                  # manifest drift
make test-capabilities
```

`capability-plan.py` exits 0 for a printed plan (`planned`, `needs_input`,
`blocked`), 2 for invalid input, 3 for a refused task.

## Rules the code enforces

- **Strict input.** Bounded size, duplicate keys, NaN/Infinity and huge integers
  rejected; symlinks (file or parent), hard links and non-regular files refused;
  `additionalProperties: false` everywhere, so an unknown field such as
  `permissions_granted`, `readiness`, `runtime_accepted` or `boundary_accepted`
  is a schema error.
- **Sources.** Pins must equal the reviewed revisions. Every adapted path must be a
  `selected` lock entry; its `content_digest` is recomputed from the lock. PM Skills
  `pm-ai-shipping` moved stubs (3 skills, 5 commands) stay `excluded-moved-stub`.
  RO-PNA and SoloSquad are reference-only: they can be cited as inspiration, never
  adapted. Each adapted source needs its LICENSE recorded. `auto_update` is `false`
  by schema.
- **Names.** Adapted skills use `pm-`/`gstack-` prefixes; nothing may shadow a
  reserved bare name (`review`, `qa`, `ship`, `retro`, `learn`, …) or an existing
  kit skill.
- **Side effects.** Audits (`gstack.review`, `gstack.qa`, `gstack.design-review`)
  have writer `none` and cannot declare source changes; source changes require the
  existing Claude route and a commit/source approval.
- **Readiness.** Eight separate dimensions: `defined`, `installed`,
  `learned_current`, `tools_available`, `authorized`, `runtime_accepted`,
  `executed`, `outcome_verified`. `defined` is computed (planned = no;
  source-implemented or existing = implementation digest matches). Everything else is `unknown` unless an injected
  observer reads it. Evidence files given as input stay `reported`; fixture
  observations stay `fixture`; `authorized`/`runtime_accepted` count only from an
  observer class in `TRUSTED_PROTECTED_OBSERVERS`, which is empty until a
  protected runtime is accepted. A `yes` above a non-`yes` prerequisite becomes
  `inconsistent`.
- **Evidence.** Only an observed, clean, non-empty run passes. Skips, zero counts,
  missing/nonzero exit and fixture/reported/simulated records never do. A
  `completed` result needs passing checks and no pending approval; observed claims
  need a primary source and can never cite a synthetic fixture.
- **Planning.** Closed IDs only (unknown → invalid). Required inputs resolve, in
  input-kind preference order, to an accepted current artifact the task references
  (`input_artifacts`) or an in-plan stage, then any other accepted current
  artifact of this company/project, else the declared producer, else
  `needs_input`. `handoff_to` is never added automatically. A selected stage is
  `reuse-existing` only for an accepted current output the task references, from
  that capability (or with no recorded producer), in its default mode; an
  unreferenced older output never replaces a selected stage. A plan of reused
  stages only is never `executable_now`. Artifacts are stale when a source
  digest, transitive source or producer version changed, or when the producer is
  no longer in the registry. `produced_by` records the producer's
  `implementation_digest` (and `source_digest` for adapted capabilities); a
  recorded digest that differs from the registry is `invalid` (changed without a
  version bump) and a producer record without them is `legacy-unverified`; neither
  is reused. Artifacts with no producer (requester-supplied) have nothing to bind.
  Because every source-implemented capability shares `scripts/capability_workflow.py`
  and its helpers, any edit there invalidates their artifacts. These digests are
  recorded provenance from the index writer, not proof of what ran.
  `capability_registry.py stamp-provenance --index FILE` prints an index with the
  registry's digests for producers whose id and version match (a maintainer
  assertion; it writes nothing) — rerun it for the eval `inputs/artifacts.json`
  fixtures after regenerating implementation digests. No implementation path may
  embed a registry digest (tested), so stamps never feed back into a digest.
  Synthetic artifacts never satisfy a real task.
- **Artifact bytes.** `build_plan` decides from index-declared digests only, so
  every artifact `satisfied_by`, `reused_output` and `reused_artifacts` entry
  carries `content_check: "unverified"`. `capability-plan.py --artifact-root DIR`
  hashes every indexed artifact of the task's own company/project under DIR
  (other tenants' entries in a shared index are neither read nor named): all matching relabels them
  `verified`; any mismatch makes the plan `invalid` with no stages and no reuse
  decisions.
- **Tool needs vs side effects.** A tool that can spend (`image.generate-paid`)
  or write outside the task (`github.approved-write`) must be matched by the
  `spend` / `external-write` side effect, or validation fails. No shipped
  implementation calls either tool, so no capability lists them (paid mockups and
  issue filing stay next-action approvals, not capability tools).
- **Company boundary.** `company-private` tasks (and any private multi-company
  context) are refused: no protected company boundary is accepted. Project
  `approved_capability_ids` only narrows. Requester/destination must be authorized
  by the company manifest, and the task's project must be one the company lists.
  A referenced input artifact of another company/project, or of an incompatible
  data class, refuses the task. `stale_artifacts` lists only this
  company/project's artifacts. Every boundary reports `not-enforced`.

## Selection seam

`selection_from_picks()` turns the closed-ID picks a selector returns into a
selection record, dropping unknown, ineligible, duplicate and over-cap picks the
way the router's AI selection does; a failed selector selects nothing.

Milestone D connects it to the existing router. `specialists.route_text(...,
capabilities={id: purpose}, capability_guidance=[...])` offers the registry's
namespaced IDs to the SAME ai-mode classifier call that picks profiles; picks
return in `trace["capability_selection"]` (outcome `ok | failed | unconfigured |
skipped`, reason, dropped), never as routes. Without a catalog the router is
unchanged (same tests, same prompts). The router's own gates apply first:
current-task extraction, runtime envelopes, status/continuation turns, `#private`
(a cloud classifier is never called) and not-substantive turns all select
nothing. The answer parser matches namespaced IDs whole before the profile
tokenizer runs, so `gstack.qa` cannot select a profile named `qa`.
`capability_selection.select()` adds one gate, a company-private or
`egress_class: deny` envelope never reaches a cloud classifier (local and
fixture classifiers still run), then builds the selection with
`selector.decision_ref` = `router-live:<task hash>` or `router-fixture:<task
hash>`; fixture plans say FIXTURE in their provenance label.

Offline evaluation (`test_capability_selection.py`) runs seven natural-language
tasks, one per recipe, with coordinator-authored FIXTURE answers: the parser,
gates, closed catalog, schema and planner are real; model judgement is not.
Every case plans exactly its recipe's smallest set. A recipe lists a planned
capability only when nothing implemented can produce the outcome
(`kit.operational-analysis`, whose plan is `blocked`, never substituted); the
marketing recipe uses the existing marketing team alone rather than adding
planned `pm.positioning`. Known limitation kept on purpose: the router's existing status
gate treats a short question ending in `?` (≤ 200 chars) as not substantive, so
it selects nothing. The live classifier could not be exercised here
(`CLIPROXY_API_KEY` absent in the tool shell); `capability-select --live`
reports `unconfigured` and exits 4.

Integration status: the coordinator path (`capability-select.py` /
`capability_selection.select()`) and, opt-in, the live hook. With
`specialist_capability_root` and `specialist_capability_catalog_sha256` set,
`SpecialistHook.on_pre_llm_call` → `route()` offers the generated, pinned
`hermes/capabilities/routing-catalog.json` (same IDs, purposes and recipe
guidance as `select()`) to the same classifier call and appends the selected IDs
and their skills to the routing note as pending input. Unset, it is the
profile-only router, unchanged; an absent, malformed or drifted catalog offers
nothing. Source and isolated tests only (`test_specialist_capability_hook.py`,
fixture classifier answers through a fake HTTP opener): the installed
model-router has neither change and no live turn has selected a capability.

## Requirement coverage by milestone

| Requirement (request section) | Milestone A (this change) | Later |
|------|------|------|
| Registry files and named fields (§3) | All 14 fields per record; readiness computed, never stored | — |
| Model-router remains the selector; registry grants nothing (§3, §8) | Closed-ID selection contract, seam, no-grant plan | D: router seam over the capability catalog (source; live classifier unverified) |
| Product discovery new/existing; lean PRD (§4) | Contracts `pm.product-discovery`, `pm.lean-prd`; artifact reuse; no auto-edit | B: Hermes adapters + validated demos |
| Problem definition quick/causal/full (§5) | Contract `kit.problem-definition`; RO-PNA reference-only | B: original skill, schemas, PD-01..06 |
| Eight gstack methods (§6) | Contracts with disabled upstream behaviors; audit/fix split | C1 + C3: all eight are source-implemented method adapters (below); rehearsal is E |
| Design consultation/review, competitor research (§7) | Contracts and artifact kinds | C1: design consultation + review; C2: competitor research (all source-implemented, below) |
| Minimal routing recipes (§8) | Planner closure tests for bug, idea, feature, UI, competitor, marketing, zero-capability | D: seven recipes evaluated through the router with FIXTURE answers |
| Company/project manifests and ten boundaries (§9) | Schemas, two synthetic tenants, refusal of private execution | Secure-execution repair: enforcement |
| Readiness reporting (§10) | Eight dimensions, no file-existence inference | E: [knowledge-reference-migration-plan.md](knowledge-reference-migration-plan.md) (not applied) |
| Third-party governance (§11) | Lock with hashes/licenses/notices/update/rollback; per-capability governance | E: selected-file manifest, inert bundle, rehearsal and rollback ([capability-activation.md](capability-activation.md)) |
| Value tests (§12) | CAT/CMP/ROU/PD-06/PM contract tests at source level | B–D: workflow value tests |

## Milestone B: source-implemented product workflows

`kit.problem-definition`, `pm.product-discovery` and `pm.lean-prd` are
`source-implemented`: skills under `hermes/skills/{problem-definition,pm-product-discovery,pm-lean-prd}/`,
contracts in `schemas/workflow-outputs.schema.json`, and the validator/renderer
`scripts/capability_workflow.py` (`validate` / `render`; read-only except one
fresh output directory). Their implementation digests cover the skill files, the
workflow script and the two schemas, so editing any of them makes `defined`
`no` until the record is updated. The PM adaptations cite only the PM Skills
files verified for this change (interview-script, summarize-interview and
pre-mortem were dropped from `source.paths`) and carry the MIT notice.

Synthetic, coordinator-authored scenarios in `hermes/capabilities/evals/` chain
problem definition → discovery → lean PRD: each `inputs/` bundle was rendered
from the previous scenario (tests compare the bytes with a fresh render) and
accepted as a labeled fixture decision. A rendered output index carries its
whole source closure, so a later stage can verify the chain and an upstream
edit marks downstream artifacts stale. `make test-capabilities` runs them.

## Milestone C1: design consultation and read-only design review

`gstack.design-consultation` and `gstack.design-review` are `source-implemented`
method adapters (skills `hermes/skills/gstack-design-{consultation,review}/`,
contracts `design_consultation` / `design_review` in
`schemas/workflow-outputs.schema.json`, helpers in `scripts/capability_design.py`).
They cite only the gstack files in the pinned, hash-verified evidence bundle
(`design-consultation/SKILL.md`, `.../sections/proposal-and-preview.md`,
`design-review/SKILL.md`, `.../sections/craft-audit.md`; the unverified
`sections/manifest.json` entries were dropped from `source.paths`) and carry the
gstack MIT notice. No gstack script, binary, preamble, hook, browser, cookie,
telemetry, update check or outside voice runs.

- **Consultation** validates a coordinator-authored analysis against an accepted
  product brief (by digest): use scene → light/dark, memorable thing (answered
  with a source or pending), method type rules (banned/overused faces; fonts
  "verified" only by a supplied licence source that names them), twelve semantic
  colour tokens with computed WCAG contrast, spacing/radius/motion, screens with
  default/empty/loading/error (and permission-denied) states, a journey that
  reaches every screen, mobile-first breakpoints, WCAG 2.2 AA requirements that
  are never marked statically verified when they need a browser or assistive
  technology, and asset rights. Research phrasing needs supplied research.
  It renders `design-brief.md`, `design-package.md`, `design-tokens.json` and a
  self-contained `design-preview.html` (inline CSS, `default-src 'none'` CSP, no
  script, no remote asset or web font, every string escaped), then audits the
  preview statically and refuses it if a state, token or breakpoint is missing.
- **Review** re-reads each reviewed artifact by digest (draft or accepted) and
  runs static checks over markup, inline CSS and design tokens. Findings must
  cite a failing check, a verbatim excerpt or a supplied capture; every failing
  high/medium check is covered or waived. Browser screenshots,
  assistive-technology runs and usability sessions count only as real captured
  files in the evidence pack (screenshots must be PNG/JPEG); otherwise the report
  says "not run", hierarchy and performance are not graded and categories nothing
  looked at are "not assessed". The schema has no fix/patch field, patch-shaped
  recommendations are refused and the record's writer is `none`.

Scenarios (`hermes/capabilities/evals/`): `trial-guide-design` (light, completes)
and `billing-rota-design` (dark, four breakpoints, needs input) consume the
accepted lean PRDs of the Milestone B chains; `trial-guide-review` audits the
consultation's own preview and tokens; `queue-page-review` audits a supplied
synthetic page with real markup defects. All are synthetic and labeled.

## Milestone C2: evidence-backed competitor and market research

`pm.competitor-research` is `source-implemented`: skill
`hermes/skills/pm-competitor-research/`, contract `competitor_research` in
`schemas/workflow-outputs.schema.json`, source helpers in
`scripts/capability_research.py`. It cites only the two PM Skills files in the
verified evidence bundle (`pm-market-research/commands/competitive-analysis.md`,
`.../skills/competitor-analysis/SKILL.md`; `market-segments` was dropped) and
carries the MIT notice. The coordinator captures pages with the existing
approved public-read tools; the validator and renderer fetch nothing.

- **Quotes, not paraphrase.** Price, feature and positioning quotes must be
  verbatim text of the competitor's own captured page (host on its domain),
  one captured line each, read in order. A capture is either the full page or a
  derived excerpt (`> ` lines) with `snapshot_digest`, the SHA-256 of the full
  retrieved text (new optional evidence-pack field). `--snapshot-dir` re-checks
  every excerpt line and price placement against the full text by digest.
- **Prices.** Status `stated` / `contact-sales` / `unknown`; amount, currency,
  unit and billing cadence must be supported by the quoted words; a per-month
  price with no billing basis is `unstated` and needs a caveat; the plan name
  must sit with its price (no other heading between); a page listed as
  `not_available` (`pricing-<competitor>`) keeps its prices unknown; under the
  pricing lens every competitor gets a row.
- **Dates.** `as_of` equals the pack's; sources retrieved after it or more than
  `max_source_age_days` (≤ 120) before it are refused; `--today` refuses a
  comparison past its re-verify date (oldest cited retrieval + window).
- **Labels.** Observed facts only from real captures (synthetic stays
  synthetic), numbers only from cited excerpts or executed computations, market
  size/share/leadership and customer-research phrasing need cited sources,
  findings are `assumption`/`hypothesis` citing evidence, recommendations cite
  findings and the assumptions they depend on. Instruction-like text inside a
  source is listed in the report and refused as evidence; private terms never
  enter public queries.
- **Outputs.** `competitor-report.md` and `evidence-ledger.json` (per claim:
  class, quotes, URL, retrieval time, capture/snapshot digests).

Scenarios: `crew-scheduling-competitors` and `clinic-reminder-competitors` are
synthetic, coordinator-authored demos on invented `.example` vendors (a removed
price page, an injected instruction, EUR/GBP, needs-input vs completed).
`public-work-management-pricing` is a real public sample: Notion, Trello and
Asana pricing pages retrieved 2026-11-04T23:26:52Z, shipped only as attributed
derived excerpts (full snapshots referenced by SHA-256, not redistributed),
with a coordinator-authored analysis. Notion's per-seat prices are recorded
with billing cadence `unstated`; Asana Enterprise is contact-sales and
Enterprise+ / AI Studio paid tiers unknown. These are vendor statements, not
product tests. The offline suite checks the excerpts; the re-check against the
real snapshots is a coordinator CLI run, not a unit test.

## Milestone C3: the remaining six gstack method adapters

`gstack.office-hours`, `gstack.spec`, `gstack.plan-eng-review`,
`gstack.plan-design-review`, `gstack.review` and `gstack.qa` are
`source-implemented`; with C1 all eight requested gstack methods are adapters.
Skills `hermes/skills/gstack-{office-hours,spec,plan-eng-review,plan-design-review,review,qa}/`
(each with the gstack MIT notice and a method map), contracts `office_hours`,
`spec`, `plan_eng_review`, `plan_design_review`, `code_review` and `qa` in
`schemas/workflow-outputs.schema.json`, pure helpers in
`scripts/capability_methods.py`. Each record cites only the verified files of
the pinned evidence bundle (14 gstack files; unverified `sections/manifest.json`,
`checklist.md`, `scope.md`, `exploratory.md`, `browser-verify.md`,
`phase-2b-builder-brainstorm.md`, `plan-completion.md` and
`shared-code-reuse.md` were dropped). No gstack script, binary, preamble, hook,
browser, cookie import, outside model, issue filing, fix loop, commit or ship
step runs. `gstack.ship` is not in the registry.

One reusable contract carries the shared semantics: verbatim excerpts of the
reused or reviewed artifacts (re-read by digest), decisions with options and
a recommendation that only a supplied answer reading `<decision id>: <option id>`
settles, calibrated findings (no quote → confidence ≤ 5; 3–4 appendix only;
1–2 only speculative critical) whose accepted/rejected/deferred disposition
needs an answered decision, and a handoff that carries no approval. Reviews
(`plan-*`, `review`, `qa`) have no questions field: they take the current
artifacts as given and do not repeat discovery.

- **Office hours** routes forcing questions by product stage (pre-product
  Q1–Q3, has users Q2/Q4/Q5, paying Q4–Q6, infra Q2/Q4); each is answered,
  covered by a verbatim quote of an accepted problem/discovery artifact, or
  asked once. Interest, waitlists and surveys are opinion, not demand.
  Premises and the approach stay pending without a recorded answer. Builder
  mode runs none of the startup diagnostic.
- **Spec** traces every criterion to a backticked requirement ID of the reused
  accepted PRD (or to a statement), requires observable outcomes and a testing
  plan covering each criterion at its level, acyclic dependencies, rollback
  and out-of-scope for features. A bug needs only root cause and a regression
  criterion. Filing is `local-draft`; publication needs its approval; a
  secret-shaped string fails the redaction gate.
- **Plan engineering review**: complexity gate (8+ files or 2+ services needs
  a structure decision), four sections never skipped, failure modes (no test +
  no handling + silent = critical gap needing a critical finding; every
  failure mode tested, planned or accepted), a `test-plan.md` for QA,
  approval readiness computed.
- **Plan design review**: initial rating, seven rated passes (below 10 names
  the gap, 10 has no findings), a state table including partial and
  permission-denied, a journey bound to plan quotes, an AI-slop cap; mockups
  need the spend approval and are never generated.
- **Review** parses the exact diff: PR type from paths (checklist rule),
  category coverage per type, findings bound to the quoted new-file line,
  forced-critical classes, scope drift, no fixes, test results only from a
  captured log.
- **QA** recomputes pass/fail from supplied command captures (`$ command`,
  stdout, stderr, `exit=N`) or PNG/JPEG screenshots; full/quick/regression
  scale the charter; every failed probe needs a bug and every bug a failed
  probe; untested contracts are reported "not run"; no health score for
  functional coverage.

Scenarios: `trial-office-hours` (reuses the accepted trial discovery/problem
briefs), `support-billing-spec` (reuses the accepted billing lean PRD),
`support-billing-eng-review` (reviews the spec render; byte-compared),
`trial-guide-plan-review` (trial PRD, design brief and package),
`rota-export-code-review` (synthetic diff with shell injection, export
deletion and scope creep) — all synthetic and coordinator-authored — and
`capability-cli-qa`, a real functional QA pass over this checkout's
`capability_workflow.py` CLI with four captured runs (exit 0/1/2/2); the test
suite replays those probes and compares exit status. It reproduced no bug;
an unverified race between the output-directory check and `os.mkdir` is
recorded as a low-confidence lead, not a bug.

## Milestones D and E

- **Router seam and routing recipes:** see "Selection seam" above.
- **Compatibility:** `capability_compat.py` checked the skills against the
  installed Hermes rules read as text; it found 48 advisory issues (60-char
  index descriptions, author/license, "When to use"), now fixed, and no name
  collision. Details in [capability-activation.md](capability-activation.md).
- **Preview fix:** a focused skip link left the flow and covered the first nav
  links at 375 px, and the largest-step h1 took three lines. Focused, the link is
  now static (it pushes the header down), `main` takes programmatic focus
  (`tabindex="-1"`), and h1/h2 scale between adjacent type-scale steps. A headless
  Chrome harness copy at a 375 px viewport measured 2 overlapping nav links and a
  49 px three-line h1 before, 0 overlaps and a 39 px two-line h1 (h2 31 px) after.
  The design-review and plan-review fixture inputs were re-rendered (only the
  preview bytes changed) and the audit's reviewed-artifact digest rebound.
- **Activation proposal:** [capability-activation.md](capability-activation.md).
- **Knowledge references:** [knowledge-reference-migration-plan.md](knowledge-reference-migration-plan.md), not applied.

## Not claimed

No installation, no live profile change, no runtime acceptance, no executed
workflow, no company isolation and no independent review. The design preview
was rendered only in a scripted headless-Chrome harness copy (focus and heading
measurements); it was not run through assistive technology or shown to users.
No gstack method was executed as upstream runtime, and no issue was filed or fix applied by any adapter.
No competitor page was fetched by the validator or by tests, no live LLM
produced an analysis, and no comparison proves market demand or revenue.
Upstream file hashes come from the research lock, not a fresh fetch.
