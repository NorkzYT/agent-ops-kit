# Cron job recipes

Hermes has a built-in scheduler; jobs post their output to Discord. Create
jobs from a terminal or by asking the bot ("every weekday at 9am, ...").

```bash
# Nightly test run, failures to the home channel
hermes cron create "0 2 * * *" \
  "Run the test suite in $REPOS_DIR/<repo>. Report pass/fail counts and failing test names." \
  --name nightly-tests --deliver discord

# Weekly dependency audit
hermes cron create "0 9 * * 1" \
  "In $REPOS_DIR/<repo> run npm audit --audit-level=high (or pip audit). Summarise high/critical findings with fixes." \
  --name dep-audit-weekly --deliver discord

# Daily heartbeat (git status, quick tests, disk) — only report problems
hermes cron create "*/30 8-23 * * *" \
  "Health check for $REPOS_DIR/<repo>: git status --short, quick test run, df -h /. Reply only if something needs attention." \
  --name heartbeat --deliver discord

# Super Productivity: read today's tasks, propose a plan, wait for approval
hermes cron create "daily at 8am" \
  "Read my Super Productivity tasks (see the super-productivity skill). Post the list with a proposed plan for each and ask for approval before doing anything. Never start work without an explicit yes, and never deviate from the approved plan." \
  --name sp-morning-plan --deliver discord

# One-shot check-in used when you promise to report back
hermes cron create "in 30m" \
  "Check the status of the task you were last working on in this repo and report progress." \
  --name recheck --deliver discord
```

Manage: `hermes cron list`, `hermes cron pause <id>`, `hermes cron run <id>`,
`hermes cron remove <id>`.
