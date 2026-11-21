---
name: browseros-neo-browser
description: "Prefer the BrowserOS neo MCP browser for web browsing when it is connected."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [browser, browseros, neo, mcp]
    category: browser
    requires_toolsets: [mcp-browseros_neo]
---

# BrowserOS neo as the preferred browser

Hermes leaves this skill out of the system-prompt skills index unless the
`browseros_neo` MCP server's tools are registered (an operator ran
`scripts/browseros-neo.sh hermes-enable --approve-activation`;
docs/browseros-neo.md). It can still be listed or opened explicitly when Neo
is not connected; if `mcp__browseros_neo__*` tools are not in your tool list,
ignore this skill. It is a preference, not a switch and not enforced: Hermes'
native browser tools stay available and unchanged.

## Choosing a browser

1. The user's words win. If they name a browser, a tool, or ask for their own
   signed-in browser (`/browser connect`), use that and do not use Neo.
2. Otherwise, for ordinary browsing (open a page, read it, click, fill a
   non-secret form, screenshot), use the `mcp__browseros_neo__*` tools. The
   operator watches this browser through its noVNC viewer.
3. When Neo's tools error or time out, use Hermes' native browser tools
   instead and say once that Neo was unavailable. Do not retry Neo in a loop.
   Never fall back to the user's signed-in browser, `/browser connect`, or
   any other session with an account signed in, unless the user asks for it.
4. When Neo is paused (the operator's `pause`; its tools vanish or refuse),
   stop the task that needed Neo and tell the user. Do not continue it in
   another browser to get around the pause.

## Privacy and accounts

- Neo is for public pages and disposable test accounts only. Never type,
  paste or ask for a password, one-time code, card number or other secret in
  Neo, and never call its credential or payment tools (they are excluded).
- Neo keeps every tool call's arguments and a screenshot for 7 days. Do not
  enter personal data the user would not want stored there.
- Do not move a task between browsers to reuse a login: a session signed in
  in one browser is not carried to the other. If a page needs a sign-in, stop
  and ask the user; the human signs in through the viewer, after the
  operator's `pause` and `handoff` steps.
- A paused Neo is the operator cutting the agent off. Never reach Neo's ports
  any other way (terminal, curl, another MCP entry) to get around a pause.
- Write-capable Neo tools ask for approval (`trust: untrusted`); do not try
  to avoid the prompt.
