# Browser Use

Hermes's browser tools (`browser_navigate`, `browser_snapshot`, `browser_click`,
`browser_type`, `browser_scroll`, `browser_vision`, `browser_exec`) run on the
Browser Use backend. The kit enables it in `config.yaml`:

```yaml
browser:
  backend: browser-use
  headed: false
  allow_private_urls: true      # localhost and LAN dashboards
```

## Local or Cloud

| | Local Chromium (default) | Browser Use Cloud |
|---|---|---|
| Needs | nothing; Hermes installs Chromium on first use | `BROWSER_USE_API_KEY` in `.env` |
| Where the page runs | on the host | on Browser Use infrastructure |
| Reaches `localhost` | yes | no (expose via Tailscale) |
| Stealth, residential proxies | no | yes |
| Cost | none | Browser Use plan |

Start local. Add a key from <https://browser-use.com> when a site blocks
automation or you need many parallel sessions. No separate always-on browser
agent is used; Hermes is the controller.

## Logins

You log in, the agent works. For a site that needs a session, run one headed
session (`browser.headed: true`, restart the gateway), sign in including MFA,
then set it back. The profile persists. Hermes never types passwords from chat,
and the Claude Code hook `guard_browser.py` blocks it as well.

## Rules the agents follow

From `.claude/skills/browser-automation/SKILL.md` and the orchestrator's
`SOUL.md`:

- Read-only on external sites unless the task says otherwise; no purchases or
  billing forms.
- Screenshot before and after every meaningful action.
- Re-snapshot after navigation; element references go stale.
- Two to five seconds between page loads; report after a 30 s timeout.
- Local services: `http://localhost:<port>` works with local Chromium only.

## Windows worker

The worker in the VM runs headed (`headed: true`) so you can watch it and take
over a login. Everything else is the same configuration.
