# QA target: capability_workflow.py command-line contract

Coordinator-written handoff for a functional QA pass over this source checkout (no product code changes).
Source: scripts/capability_workflow.py, function main().

- `validate` prints {"ok": true, ...} and exits 0 when the analysis passes its contract.
- `validate` and `render` print {"ok": false, "errors": [...]} and exit 1 when the analysis or a supplied input fails validation.
- A file that cannot be read or parsed (for example duplicate JSON keys) is refused with exit 2 and a message on stderr.
- `render` never overwrites: an existing output directory is refused with exit 2 before anything is written.
- No network, shell or repository write happens; only the fresh output directory of render is created.
- A missing required argument is a usage error: exit 2, usage on stderr, nothing on stdout.
