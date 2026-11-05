# Method map (PM Skills @ 2a12295a7717569baa7be181b4cfebe3ca38a6bb → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `pm-execution/commands/write-prd.md` | accept any input form; ask at most three questions (problem, users, metric); lean by default; non-goals and specific metrics | rewritten as a validated workflow; questions may not ask for facts the evidence holds; "just write it" gaps become assumptions/unknowns |
| `pm-execution/skills/create-prd/SKILL.md` | eight sections, lean length guide, segments by job, 6-part value proposition, 7.4 assumptions, release in relative time, "don't invent" | section contract enforced; contacts left out when unknown; value proposition per segment and feature coverage checked; criteria traced to evidence or a chosen requirement |

Disabled upstream behaviors: Claude plugin command expansion, web research
inside the PRD step (public research is a separate capability), saving files
to the user's workspace, follow-up offers that start other commands,
automatic implementation or issue publication. Not adapted (not in the
verified evidence bundle): `pre-mortem`.
