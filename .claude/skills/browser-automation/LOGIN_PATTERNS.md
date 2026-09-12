# Login patterns

> How authenticated browser sessions work with the Hermes browser tools
> (Browser Use backend). Applies to local Chromium and Browser Use Cloud.

## Rule zero

The operator logs in. The agent never types a password or a 2FA code, whether
it came from chat, a file or memory. `guard_browser.py` blocks `type|fill`
commands that mention `password` for the same reason.

## Pattern 1: persistent local profile (default)

With no `BROWSER_USE_API_KEY`, Hermes drives a local Chromium with its own
profile directory, so cookies survive between sessions.

1. Agent: `browser_navigate` to the login page, then `browser_snapshot` and
   report "login required".
2. Operator: run the same task with `browser.headed: true` once (or open the
   profile in a window) and sign in, including MFA.
3. Agent: `browser_navigate` to the target page, `browser_snapshot`, confirm no
   login form is visible, continue.

Sessions expire. When a snapshot shows a login form again, stop and ask; do
not try to "work around" it.

## Pattern 2: Browser Use Cloud

Cloud sessions run on Browser Use's infrastructure. Use the Browser Use
dashboard's saved-profile feature to keep a logged-in profile, and reference
that profile in the task. The agent still never enters credentials.

## Pattern 3: the operator's real browser

For sites that fight automation, set `browser.use_real_profile: true` in
`~/.hermes/config.yaml` to reuse the operator's own Chrome profile. Only do
this on a machine dedicated to the agent (the Windows VM, not a laptop with
personal accounts open).

## Checking login state without guessing

```
browser_snapshot          # accessibility tree: look for "Sign in"/"Log in" controls
browser_vision            # screenshot when the tree is ambiguous (SPA overlays)
```

Report the state in one line ("logged in as <account>" or "login form
visible") before doing anything else on the site.
