# Method map (PM Skills @ 2a12295a7717569baa7be181b4cfebe3ca38a6bb → Hermes)

| Upstream file | Used for | Hermes change |
|---|---|---|
| `pm-product-discovery/commands/discover.md` | step order: context → ideas → assumptions → prioritization → experiments → plan; two checkpoints | rewritten as an explicit workflow; checkpoints answered only by supplied evidence; no plugin command expansion; plan saved only as validated draft artifacts |
| `brainstorm-ideas-existing`, `brainstorm-ideas-new` | product/design/engineering ideation, top ideas with reasons | the trio is a validated requirement; idea status proposed/carried/declined |
| `identify-assumptions-existing` | four risk areas, devil's advocate | category set enforced for `existing` |
| `identify-assumptions-new` | eight risk categories incl. go-to-market | category set enforced for `new`; one go-to-market assumption required |
| `prioritize-assumptions` | impact × risk matrix and its four actions | matrix enforced; test assumptions ranked 1..n |
| `brainstorm-experiments-existing` | prototype, fake door, spike, A/B (with mitigation), Wizard of Oz, behavioral survey | method list enforced; A/B and fake door need risk mitigation |
| `brainstorm-experiments-new` | XYZ hypothesis, pretotypes, skin in the game | semantic hypothesis fields instead of X/Y/Z letters; viability tests need pre-order or concierge |
| `opportunity-solution-tree` | outcome → opportunities → solutions → experiments | opportunities framed as customer needs and cited |

Disabled upstream behaviors: Claude plugin command expansion, web search
inside discovery, saving files to the user's workspace, "offer next steps"
that start other commands. Not adapted (not in the verified evidence bundle):
`interview-script`, `summarize-interview`. Moved `pm-ai-shipping` stubs are
excluded by the lock and are not methods.
