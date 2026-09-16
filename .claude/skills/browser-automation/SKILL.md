---
name: browser-automation
description: How browser work happens in this kit (Hermes browser tools / Browser Use) and the rules for it
---

# Browser automation

Browser work is done by **Hermes** through its browser tools (Browser Use
backend: `browser_navigate`, `browser_snapshot`, `browser_click`,
`browser_type`, `browser_scroll`, `browser_vision`, `browser_exec`). Claude Code
sessions do not drive a browser directly; when a coding task needs UI
verification, either:

- ask the Hermes orchestrator (it delegates coding to you and verifies in the browser), or
- run the project's own E2E tooling (Playwright, Cypress) from the terminal.

If a browser CLI is installed locally (`agent-browser`, `browser-use`), the
`guard_browser.py` hook still blocks checkout/payment URLs and password typing.

## Rules (apply to any browser session)

- **Logins are the operator's job.** Wait for them to sign in and pass MFA; never type passwords or 2FA codes from chat. Reuse the logged-in browser profile (`browser.use_real_profile` or a dedicated profile) instead.
- **Read-only on external sites** unless the task says otherwise. No purchases, no form submissions to billing pages.
- **Evidence:** screenshot before and after every meaningful action.
- **Re-snapshot after navigation.** Element refs go stale.
- **Pace:** 2-5 s between page loads; 30 s page timeout, then report.
- **Local services:** with a local Chromium, `http://localhost:<port>` works. With Browser Use Cloud, the page runs remotely; expose the service (Tailscale) or switch to local for that task.

## Common patterns

- **Visual check of a UI change:** navigate, snapshot, screenshot, compare with the expected state, report the diff in words.
- **Data verification:** load stored data, open the live page, extract values with a snapshot or `browser_vision`, diff, fix the scraper, run tests, commit on a branch.
- **API discovery:** walk the flow while capturing network traffic, list endpoints and auth headers in `context.md`, test GET calls only.
- **Extension testing:** run headed (`browser.headed: true` in Hermes config) and load the unpacked extension; check its injected UI via snapshot.
