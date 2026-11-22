# UI audit pilot (axe-core, Lighthouse CI, Impeccable detector)

A bounded, project-local pilot: three pinned tools run against owned static
fixtures, or your own static pages (`--project`), served from `127.0.0.1` on
an ephemeral port. Nothing is installed
globally, no installer scripts run, nothing is uploaded, and no hook or plugin
is added to Claude Code or Hermes. It produces local evidence to attach to a
design review (`hermes/skills/gstack-design-review`); it complements, and does
not replace, `.claude/docs/accessibility-testing.md`.

## Pinned packages

Checked with `npm view <pkg>@<ver> name version repository.url dist.integrity license`
on 2026-11-22; the lockfile carries the same integrity.

| Package | Version | Repository (matches research notes) | License | dist.integrity |
|---|---|---|---|---|
| `axe-core` | 4.14.1 | github.com/dequelabs/axe-core | MPL-2.0 | `sha512-FHfa+lmNjQZfJz1q8wKqIDwC6DExcHt/pkjNQb09a3NPjCKg2rhu5qb2S4r76ASyFNNtuFooJpZe37OgudVGtQ==` |
| `@lhci/cli` | 0.16.1 | github.com/GoogleChrome/lighthouse-ci | Apache-2.0 | `sha512-Vh3z9Xkcs4ULrl62NTF9a+c9HL9teyD+dq3TueG4p4oo9ypYbPBb3Ht4YvxHzOyr31rFaiuHqdphpnVZs5SXQw==` |
| `impeccable` (detector CLI shim) | 4.10.0 | github.com/pbakaus/impeccable | Apache-2.0 | `sha512-eEyW+lJiwwHa6mCQEYoILtA98aTfuiIz61oEFkupvvtfYx5/Uodee3xnrUwihcLw5/XxC7ei4VV0H4Xsh08oxw==` |
| `@impeccable/cli-linux-x64` (engine, optional dep) | 0.1.27 | github.com/pbakaus/impeccable | "SEE LICENSE IN LICENSE" | `sha512-gvJUVnuW11/FILcqeLZ1QpGssXHd4OdaExQCi9l/VM0egIN4yQciLeI7WSlDCwd6NC9TCrTcLAfbAkJadYCgZg==` |

Transitive: `lighthouse` 13.6.0 (pinned exactly by `@lhci/cli`). No package in
the lockfile declares an install script. The Impeccable engine binary's
sha256 (`41d66e94…09ccfd`) matches `skill/scripts/engine-checksums.txt` in the
pinned `impeccable` package; `run-impeccable` re-checks it on every run and
calls the binary directly, so the shim's download fallback is never reached.
No puppeteer: axe-core is injected over CDP using Node's built-in WebSocket.

## Install

`.npmrc` sets `ignore-scripts`, `save-exact`, `fund=false`, `audit=false`,
`update-notifier=false`.

```bash
cd tools/ui-audit-pilot
npm ci --ignore-scripts
```

Needs Node 22+ on `PATH` and Chrome at `/usr/bin/google-chrome`
(override with `UI_AUDIT_CHROME`; it also reaches LHCI as `--chromePath`).
`make tool-pilots-check` reports what is missing.

## Fixtures

| Page | Purpose |
|---|---|
| `fixtures/queue-bad.html` | Negative control. Seeded unlabeled input `#seeded-unlabeled`, low-contrast text `#seeded-low-contrast` (about 2.0:1), and text on a gradient `#seeded-gradient` that axe cannot decide |
| `fixtures/queue-clean.html` | Clean control: no seeded defects |
| `fixtures/queue-regression.html` | Clean page minus its meta description, the known Lighthouse regression |

## Run the tests

Real tools against the real fixtures; takes about 75 s.

```bash
npm test
```

## Run each audit

Each run writes into a new directory `reports/<run-id>/` (gitignored), or
`<--out>/<run-id>/`; existing reports are never deleted or overwritten
(`scripts/lib/output.mjs`). `--run-id` shares one directory between runners.

```bash
npm run audit:axe          # <run>/axe-bad.json, <run>/axe-clean.json
npm run audit:lhci         # <run>/lhci/<page>/upload/ (HTML + JSON + manifest)
npm run audit:impeccable   # <run>/impeccable.sarif, <run>/impeccable-rendered-<page>.json, <run>/impeccable-summary.json
```

Pick pages with `--page`, for example `node scripts/run-axe.mjs --page clean`.

Exit codes:

| Script | 0 | 2 | 1 |
|---|---|---|---|
| `run-axe` | no violations | violations found | scan error |
| `run-lhci` | all error-level assertions pass | an assertion failed | tool error |
| `run-impeccable` | scans completed (findings are advisory) | not used | scan error, reported as `SCAN ERROR` |

The default `audit:axe` and `audit:lhci` runs include the seeded pages, so they
exit 2 by design.

## Your own project

Fresh machine setup and the Make entry point: `docs/tool-pilots.md`.

```bash
node scripts/run-axe.mjs --project /abs/dist --page-path index.html --page-path docs/a.html --out /abs/reports
node scripts/run-lhci.mjs --project /abs/dist --page-path index.html --out /abs/reports
node scripts/run-impeccable.mjs --project /abs/dist --page-path index.html --out /abs/reports
```

The script serves only that directory itself; guards (`scripts/lib/project.mjs`,
checked before a browser starts) refuse `/`, `$HOME` and its parents, hidden
directories under `$HOME`, system trees, pages outside the directory or in
hidden paths. `--out` (also in fixture mode) is refused if it is `/`, `$HOME` or
a parent, hidden under `$HOME`, a system tree, a symlink, an existing file,
inside the served directory, or the served directory / a parent of it. The
default report base is `reports/project-<name>-<hash>`. The server 404s
hidden files and non-web types. `--project` cannot be mixed with `--url`,
`--page` or `--file`; `--url` stays fixture-only.

## Manual checks on the same fixtures

```bash
node scripts/serve.mjs     # prints 127.0.0.1 URLs; Ctrl-C to stop
```

Then audit one served page by URL, or open it in a browser for keyboard and
screen-reader checks:

```bash
node scripts/run-axe.mjs --url http://127.0.0.1:<port>/queue-clean.html
```

## Boundaries

- Targets (navigation only): only `http://127.0.0.1:<port>/<owned fixture page>` (for example
  `/queue-clean.html`), or with `--project` the selected pages of the
  directory the script serves itself (see "Your own project"). `localhost`, other hosts, IPv6, `https:`, `file:`,
  credentials, any other path, query strings and fragments are refused before
  a browser starts. The port is not verified to be `serve.mjs`: only pass a
  port you started. Impeccable file targets must stay inside `fixtures/`.
  This guards the URL the browser is sent to, not the page's subresources:
  a page can load from `127.0.0.1` on any port (other local services), and
  run-axe lists those requests as `otherLoopbackRequests` without blocking.
- Environment: Chrome, LHCI/Lighthouse and the Impeccable engine get a minimal
  environment (`scripts/lib/env.mjs`): fixed `PATH`, a throwaway private
  `HOME` with `XDG_*` dirs inside it, a short `TMPDIR`, `DO_NOT_TRACK=1`,
  `NO_UPDATE_NOTIFIER=1`. Nothing else is inherited: no tokens, no proxies, no
  `LHCI_*` config overrides, no `IMPECCABLE_*` hosts, no `NODE_OPTIONS`
  (`tests/env.test.mjs` sets hostile values and checks they are absent).
- Network: there is **no network isolation**. Chrome launches with
  background networking, component updates, sync and pings off, and its
  proxied non-loopback traffic goes to a closed proxy port
  (`--proxy-server=http://127.0.0.1:9`); loopback bypasses that proxy. The
  axe/Impeccable Chrome also maps every host except 127.0.0.1 to NOTFOUND;
  the LHCI Chrome does not, so `localhost` subresources resolve there. The
  axe runner records non-loopback and other-loopback requests. The proxy flag
  is not a sandbox, and the LHCI and Impeccable processes run with normal
  host networking; no test captures their traffic.
- Uploads: `lighthouserc.json` uses `upload.target: filesystem` with a relative
  `outputDir`. `run-lhci` refuses any other target, server URL, token or LHCI
  server config. Lighthouse error reporting is off (`enableErrorReporting: false`).
- Impeccable: run with `DO_NOT_TRACK=1`, `IMPECCABLE_NO_TELEMETRY=1`,
  `IMPECCABLE_NO_UPDATE_CHECK=1`, `IMPECCABLE_NO_STALENESS_CHECK=1`,
  `--no-config`, a throwaway `IMPECCABLE_HOME`, and live-copy Apply off. No `impeccable install`, no hook,
  no browser extension.
- Tools get `TMPDIR=/tmp` when `TMPDIR` is long: Chrome's singleton socket
  path would exceed the Unix socket limit and Chrome never opens its DevTools
  port.

## What a clean scan does NOT mean

- It is not accessibility sign-off. axe and Lighthouse catch only part of
  WCAG. Keyboard operation, focus order, focus visibility and whether accessible
  names make sense must be checked by hand.
- axe `incomplete` results are reported as `needsManualReview`: axe could not
  decide them, so a person has to check them.
- A passing Lighthouse assertion set is not a performance or SEO budget. This
  pilot asserts accessibility and SEO audits only. Set performance budgets only
  after repeated runs on a fixed browser and build.
- Impeccable findings are design heuristics, not defects. They are advisory
  and never fail the build. A scan error is never a clean result.
