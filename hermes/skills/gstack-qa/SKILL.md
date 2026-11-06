---
name: gstack-qa
description: Use for functional QA with captured, replayable evidence.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [qa, testing, audit, evidence]
    category: software-development
    requires_toolsets: [file]
---

# Functional QA, audit only (adapted from gstack)

Charter a QA pass over a target (a change handoff, spec, engineering test plan
or UI evidence), record each probe's exact command and expectation, and report
outcomes that come only from captured runs. You (the coordinator) run the
probes with the kit's existing approved runners and capture each one; you
then write one analysis JSON. `scripts/capability_workflow.py` re-reads every
capture by digest, recomputes pass/fail from its exit status and renders
`qa-report.md` and `qa-charter.json`. This skill never fixes or commits.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
see `references/gstack-license.md` and `references/method-map.md`). The
gstack browser and its setup, cookie import, the fix loop, commits, test
bootstrap, telemetry and update checks do not run.

## When to use

Use when a change or spec needs functional QA with captured, replayable evidence.

## Capture format

A command capture is a file tagged `command-capture` in the evidence pack
(kind `artifact`, real, with digest):

```text
$ <exact probe command, run from the repository root>
--- stdout
...
--- stderr
...
exit=<status>
```

Browser outcomes need supplied PNG/JPEG screenshots tagged `browser-capture`.
Synthetic fixtures are never QA evidence.

## Procedure

1. **Mode:** full (every documented contract gets a row: pass, fail,
   blocked, not run or not applicable with a reason), quick (success plus the
   highest-risk edge; the rest not run) or regression (needs the prior
   `qa-report` as a reviewed artifact).
2. **Charter.** For each contract — success, invalid input, auth, CLI
   process, state transitions, timeout, retry, idempotency, concurrency,
   partial failure — record the probe, the expectation, the expected exit and
   the target text it comes from (verbatim).
3. **Run and capture** with existing approved tools (no new harness). A probe
   you did not run is `not-run`, never a pass. Expected rejections may pass.
4. **Bugs.** Every failed probe is reproduced by a bug (steps, expected,
   actual); every bug cites a failed probe. Leads you could not reproduce are
   findings with calibrated confidence, not bugs.
5. **Validate and render** with `scripts/capability_workflow.py validate|render`.

## Contract and example

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/qa`.
- Real sample: `hermes/capabilities/evals/capability-cli-qa/` — functional QA of this
  checkout's `capability_workflow.py` CLI with four real captures; the test suite
  replays them.

## Limits

No product code, test or TODO file is edited. Functional coverage is reported
as contract outcomes, never as a score. Repairs go to the existing
engineering path.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
