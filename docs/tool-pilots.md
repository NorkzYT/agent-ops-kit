# Tool pilots on a fresh machine

One path to get the project-local pilots working from a fresh checkout:
the UI audit (axe-core, Lighthouse CI, Impeccable detector) and the
HyperFrames marketing-video lane. Everything installs into
`tools/<tool>/node_modules` from the tracked lockfiles. Nothing global, no
`npx`, no install scripts, no hooks, no Hermes or Claude config, no restarts.

## 1. Check prerequisites (downloads nothing)

```bash
make tool-pilots-check                    # or TOOLS=ui-audit / TOOLS=hyperframes
```

| Needed | For | Override |
|---|---|---|
| Node 22+ and npm on `PATH` | both | |
| Google Chrome | both | `UI_AUDIT_CHROME`, `MARKETING_CHROME` (default `/usr/bin/google-chrome`) |
| FFmpeg on `PATH` | HyperFrames renders | |
| python3 | `make test-rtk-pilot` | |

Exit 0 when prerequisites are present and any installed deps match the pins;
1 when something is missing or a pin does not match (stale pins are fixed by
the install below).

## 2. Install the pinned deps (explicit opt-in)

```bash
TOOL_PILOTS_ALLOW_DOWNLOADS=1 make tool-pilots-install
```

This downloads from `https://registry.npmjs.org/` (`npm ci --ignore-scripts`,
lockfile integrity checked) and refuses with exit 2 without the opt-in. npm's
download cache is `$HOME/.npm` unless `npm_config_cache` is set. Only missing
prerequisites (Node, npm, Chrome, FFmpeg) stop the install; missing or stale
pinned deps do not, since `npm ci` replaces `node_modules` from the lockfile.
The full check (prerequisites and pins) runs again at the end and sets the
exit code. Re-running is safe.

## 3. Audit your own pages

Build your site to static files first, then:

```bash
make ui-audit-project PROJECT=/abs/path/to/dist PAGES=index.html,pricing/index.html OUT=./ui-audit-out
```

- The pilot serves only `PROJECT` on `127.0.0.1` (ephemeral port) and
  navigates only to the listed pages. That restricts the **target URL**, not
  what the page loads: other `localhost` URLs are refused as targets, but a
  page's own subresources (images, scripts, fetches) to `127.0.0.1` on any
  port are not blocked, so a page can reach other local services. The axe
  report lists them as `otherLoopbackRequests`.
- `PROJECT` must be an absolute directory. Refused: `/`, your home directory
  or any parent of it, hidden directories under home (`~/.ssh`, `~/.config`,
  ...), system trees (`/etc`, `/proc`, `/usr`, ...).
- Pages must be existing `.html` files inside `PROJECT`; no hidden segments,
  `..`, query strings or symlinks that leave the directory.
- The server returns 404 for hidden files (`.env`, `.git/...`) and for any
  type other than HTML/CSS/JS/images/fonts (no JSON, databases or keys).
- Each run writes into a new directory `OUT/<run-id>/` (default `OUT` is
  `tools/ui-audit-pilot/reports/project-<name>-<hash>`, so two projects with
  the same basename never share it). Nothing already in `OUT` is deleted or
  overwritten; report files are created exclusively and a collision is an
  error. `OUT` is refused if it is `/`, your home or a parent of it, a hidden
  directory under home, a system tree, a symlink, an existing file, inside
  `PROJECT`, or `PROJECT` itself / a parent of it. LHCI uploads to the
  filesystem only.
- Exit (`bash scripts/ui-audit-project.sh`, same variables): 1 if a tool
  errored or a guard refused, else 2 if axe or Lighthouse found failures,
  else 0. Through `make` any failure exits 2; the script's code shows as
  `Error 1` / `Error 2`. Impeccable findings are advisory.
- Requests the page makes to non-loopback hosts go to a dead proxy (in the
  axe/Impeccable Chrome, non-127.0.0.1 names also fail to resolve) and are
  listed in the axe report (`nonLoopbackRequests`) and as a warning. This is
  not network isolation: the tools run with normal host networking.

Limits and what a clean scan does not mean: `tools/ui-audit-pilot/README.md`.
Videos: `docs/marketing-video.md` (HyperFrames is the default; rights gates run
before every render).

## Not covered by this setup

- **RTK: no-go.** The pinned v1.2.1 OSS binary (`tools/rtk-pilot/rtk.lock.json`)
  SIGILLs on the dev VM. A local build of the same tag runs, but the replay
  exits 1: `rtk summary cat` drops the skip reason from an all-skipped pytest
  log. Producer-exit and guard parity were not tested; no install or hook is
  approved. See `tools/rtk-pilot/README.md`.
- **Runtime activation.** Nothing here installs a Hermes skill, hook or
  profile, or changes the running gateway.
- **Network isolation.** There is none: Chrome, LHCI and the Impeccable
  engine run with normal host networking. The proxy flag only redirects
  proxied non-loopback browser requests; loopback subresources are allowed
  (see the UI audit README).
- Single-page apps that need a dev server or client routing: audit the built
  output; dev-server URLs are out of scope.
