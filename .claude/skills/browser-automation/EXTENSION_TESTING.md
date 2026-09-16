# Testing browser extensions

> Loading and checking an unpacked extension with the Hermes browser tools
> (local Chromium only; Browser Use Cloud cannot load unpacked extensions).

## Setup

1. Build the extension to a directory (for a no-build MV3 extension that is the
   repo itself).
2. In `~/.hermes/config.yaml` run headed and point Chromium at the unpacked
   extension:

   ```yaml
   browser:
     backend: browser-use
     headed: true
     extra_chromium_args:
       - --load-extension=/opt/repos/<extension>
       - --disable-extensions-except=/opt/repos/<extension>
   ```

3. Restart the gateway (`hermes gateway restart`) or start a new `hermes chat`.

## Check the extension loaded

- `browser_navigate` to `chrome://extensions`, `browser_snapshot`, confirm the
  extension name and version appear and no "Errors" button is shown.
- Open a page the extension targets, `browser_snapshot`, and look for the
  injected UI by its accessible name or id.
- `browser_exec` can read `document.querySelector('#<injected-root>')` to
  assert presence and state.

## Iterating

After a code change: rebuild, reload from `chrome://extensions` (the "reload"
control next to the extension) via `browser_click`, re-snapshot the target
page. Report before/after screenshots (`browser_vision`) with the diff in words.

## Popup and service worker

- Popup pages open as `chrome-extension://<id>/popup.html`; navigate there
  directly once the id is known from `chrome://extensions`.
- Service-worker logs: `chrome://extensions` -> the extension -> "Inspect
  views: service worker". Read the console with `browser_exec` on that page
  or ask the operator to open DevTools when a headed window is available.

## Boundaries

Extension testing is read-mostly. Do not install extensions from the Web
Store, change Chrome policies, or sign in to accounts as part of a test run.
