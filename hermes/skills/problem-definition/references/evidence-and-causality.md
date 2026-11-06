# Evidence and causality rules (what the validator enforces)

| Rule | Rejected example |
|------|------------------|
| Observed statements cite real observation sources | an `observed` statement citing a synthetic fixture, or nothing |
| A synthetic task has no observed facts | a fixture fact labeled `observed` |
| Numbers come from sources or executed arithmetic | "median 40 hours" when the source says 31 |
| Computations re-execute | `60 / 300 * 100` stated as 25 |
| `not_available` topics stay unknown | a market size stated for a topic the pack lists as not supplied |
| Unknowns say how to resolve them | an unknown with no retrieval/measurement plan |
| Questions ask only for missing facts | asking about staffing when the pack covers staffing |
| Correlation is not causal proof | `experiment-supported` on an observational export |
| A missing cause is not a fact | an unknown statement labeled `observed-association` |
| Causal hypotheses are falsifiable | `causal-hypothesis` without a test |
| Branching causes declare their interaction | two root causes and no `interaction_assessment` |
| Quick stays quick | a quick brief with a why tree, or over 450 rendered words |
| Full mode explains skips | a skipped stage without a reason, or any skip when the full exercise was requested |
| Contact needs approval | a next action that contacts customers without `outreach` |

What passing does **not** prove: that the analysis is wise, that the
hypothesis is true, or that the decision will work. A reviewer still reads
the brief, and real outcomes are measured later.
