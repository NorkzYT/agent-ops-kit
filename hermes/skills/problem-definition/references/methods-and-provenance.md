# Methods and provenance

Authorship: original Agent-Ops-Kit procedure and wording, licensed with the
kit (GPL-3.0). No third-party text, prompt, template, scenario data or code is
copied, translated or paraphrased closely.

## General methods named

These are widely taught analytical techniques; the skill uses its own
procedure and field names for them.

- **SCQA** (situation, complication, question, answer) as a framing device.
  Here the answer is always a *candidate* and the complication must be
  evidenced.
- **Why tree** (a branched form of "five whys"). Here the depth is never fixed
  and every link carries an evidence label.
- **MECE-style decomposition** (mutually exclusive, collectively exhaustive).
  Here overlaps and omitted cases are recorded instead of forced away, and
  interacting causes are stated as interactions.
- **Customer/process causal mapping** from leading problems to an outcome
  metric, with labeled, falsifiable edges.
- **Measurable hypotheses** with named semantic fields instead of positional
  X/Y/Z letters.

## Inspiration references (concepts only)

Recorded in `hermes/capabilities/upstreams.lock.json` as `reference-only`:

- RO-PNA `pna-builders` @ e4237f11ad1359f3e8da722f56e37962a69c0ce4 — the public
  workshop sequence (SCQA, five whys, logic tree, TDCC, XYZ, one-page PRD). **No
  license is declared**: nothing from it is reused. Its chatbot can generate
  simulated results for a workshop game; this skill forbids that for real work.
- SoloSquad @ 197ddcd1032fdaf2dd58ee37d075da232bdd1db3 (MIT) — architecture
  lessons only. Its confidence gate (≥60) and minimum-approach rules are its
  own choices and are not adopted.

## Hypothesis label conventions

The two sources letter their XYZ hypotheses differently (RO-PNA: X = share of
a population, Y = behavior, Z = reason; SoloSquad: "X% of Y will Z" with the
reason separate). `capability_workflow.hypothesis_from_xyz()` maps either
convention to the same semantic fields and refuses to infer the window,
measurement, counter-metric or failure rule.
