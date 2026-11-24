---
name: anatomy
description: Use when an idea needs an interactive isometric explainer.
version: 1.0.0
author: Ryan (Anatomy, MIT); Hermes adaptation by Agent-Ops-Kit
license: MIT (upstream LICENSE and notice in references/upstream-notice.md)
metadata:
  hermes:
    tags: [explainer, isometric, svg, interactive, figure, diagram, visualization, teaching]
    category: creative
    requires_toolsets: [terminal, file]
---

# Anatomy: explain an idea as a working machine

Anatomy builds a crafted, interactive isometric SVG/HTML figure that explains a
technical idea as a real physical object: a token bucket becomes a hopper and
a turnstile, a cache key becomes a pin-tumbler lock, a blur becomes a
bed-of-nails stamp. The reader understands the idea because they recognise the
machine, and every number the figure shows comes from the same small model
that moves its parts. This is the upstream Anatomy skill
(https://github.com/wheresryan22/anatomy, MIT) adapted for Hermes: the kit,
checks, references and example sources ship here, so building and checking a
figure downloads nothing; nothing isolates it from the network either (see
"Offline and safety").

## When to use

Use it when the deliverable is a figure:

- The user asks to see, show, visualise, draw or illustrate how something
  works, or asks for an explainer, diagram, illustration, interactive figure
  or "the polished kind" of technical drawing, even without saying "isometric".
- A figure for docs, a blog post, a landing page, a talk, a handout (A4/Letter
  PDF) or a React/Next.js page.
- An idea with a mechanism (flow, threshold, queue, state, feedback, ratio,
  backpressure) the user wants to understand by playing with it.

## When not to use

- A plain question ("what is a mutex?", "why does X fail?"): answer in text.
  You may add one line offering a figure; build it only if they say yes.
- A data chart, dashboard or plot of real numbers: that is a chart, not a
  machine.
- A quick sketch, ASCII diagram, Mermaid flowchart or slide outline the user
  asked for by name: give them that.
- A marketing video (`marketing-video-system`) or a screenshot of a UI.

Every Anatomy figure is a dense, real machine (40+ parts), so it costs real
time. Don't produce one when a paragraph answers the question.

## Size it and tell the user

- **Figure**: one mechanism, 40-80 parts, SVG, at most one WebGL layer. Build
  it without asking; say "building a figure" and start.
- **Hero**: 80-150 parts, several WebGL layers or 3D. About 2-4 hours. Say so,
  then build it unless the user asked for something quick.
- **Epic**: whole vehicle, plant or many systems. Don't start; give the
  estimate and propose a Hero version.

In chat (Discord), have a working page within the first hour, deliver it, and
deepen from there only if asked.

## Workspace

Never build inside this skill directory and never edit the installed copy.
Every figure gets a new, empty, dedicated directory created exclusively by
`mktemp -d`: inside the user's project when they named one, otherwise under
the Hermes scratch directory (`$TMPDIR`, pruned after 24h idle, so say where
it is). Never copy the kit into an existing directory, a project root or a
path you merged into before: the copy would overwrite the user's `LICENSE`,
`kit/` or `scripts/`. The block refuses a parent that is missing or a
symlink, never creates or changes parent directories, and a rerun makes
another new directory instead of reusing one. The workspace mirrors the skill
layout so example imports (`../../kit/iso-kit.mjs`,
`../../scripts/inline-kit.mjs`) work unchanged:

```bash
SKILL_DIR=...   # the directory holding this SKILL.md (skill_view shows it)
PARENT=...      # the user's named project directory, else "$TMPDIR"
SLUG=...        # the figure's name: lowercase letters, digits and -
WORK="$(
  set -eu
  case "$SLUG" in ''|*[!a-z0-9-]*) echo "refusing: slug must be lowercase a-z, 0-9 or -" >&2; exit 1;; esac
  if [ ! -d "$PARENT" ] || [ -L "$PARENT" ]; then echo "refusing: $PARENT is not an existing real directory" >&2; exit 1; fi
  w="$(mktemp -d "$PARENT/anatomy-$SLUG.XXXXXX")"   # new and exclusive; never an existing dir
  mkdir "$w/figures" "$w/figures/$SLUG"
  cp -R "$SKILL_DIR/kit" "$SKILL_DIR/scripts" "$SKILL_DIR/LICENSE" "$w/"
  cp "$SKILL_DIR/examples/dial-indicator/build.mjs" "$w/figures/$SLUG/build.mjs"   # or the closest example
  printf '%s\n' "$w"
)" && cd "$WORK" && node "figures/$SLUG/build.mjs" && node "figures/$SLUG/build.mjs" --light && echo "workspace: $WORK"
```

The copied example still writes its own file names (`dial-indicator.html`);
rename the outputs to `<slug>` when you adapt it.

Framework-free figures need Node.js 20.10+ (no npm packages) and Chrome or
Chromium for the checks (`CHROME_PATH` if it isn't found). The React/TS kit
(`kit/iso-kit.ts`, `kit/react/`) is compiled by the user's own React/TypeScript
toolchain; running a `.ts` file directly with Node needs Node 22.18+ or 23.6+
(built-in type stripping). Nothing is installed here.

## Method

Read `references/method.md` before the first geometry: it is the upstream
workflow (size, truth first, invent the object, parts list, plan the world,
build geometry once, painter's order, frame, make it live, verify like a
critic) with the craft rules. Then read, as needed:

- `references/craft.md` before designing (parts vocabulary, tones, painter's order).
- `references/kit.md` for the API; `references/motion.md` for live behaviour,
  keyboard and reduced motion; `references/verify.md` for the checks.
- `references/walkthrough.md` for a worked design; `references/webgl.md`,
  `references/3d.md`, `references/react.md`, `references/print.md` only when
  the figure uses them.

Open the closest example source first: `examples/dial-indicator/` (smallest,
about 300 lines), `examples/push-button/`, `examples/desk-computer/`,
`examples/arcade-cabinet/`, `examples/raptor-engine/`, `examples/ripple-tank/`,
`examples/turning-dial/`, and the React sources `examples/test-rig/` and
`examples/spot-plate/`. Only sources ship; build an example to see it.

Every readout, caption and motion must be true: write the model as a small
pure module and drive the drawing from it. Don't invent numbers.

## Verify before you deliver

Run in the workspace, and read every PNG you make:

```bash
node scripts/capture.mjs figures/<slug>/<slug>.html --out shots/desktop.png
node scripts/capture.mjs figures/<slug>/<slug>.html --width 390 --out shots/phone.png
node scripts/capture.mjs figures/<slug>/<slug>-light.html --out shots/light.png
node scripts/capture.mjs figures/<slug>/<slug>.html --reduced --out shots/reduced.png
node scripts/lines.mjs figures/<slug>/<slug>.html --out shots/lines.png   # must print line ends: 0
```

With pipes, cables or round parts, `node figures/<slug>/build.mjs --audit`
must end `audit passed`. `capture.mjs` prints console errors and sideways
scroll; both must be empty. Never report a check you did not run.

## Accessibility

Keep what the kit gives and check it in the built page: the figure has
`role="img"` with an `aria-label` that states the idea, the control is a
focusable `role="slider"` (or button) with `aria-valuenow`/`min`/`max` and
works from the keyboard, decorative layers are `aria-hidden`, motion stops
under `prefers-reduced-motion` and offscreen, and both themes read at 390 px.
The caption under the card says the idea in words, so the figure is never
the only carrier of the explanation.

## Offline and safety

- Figures are standalone HTML with the kit inlined: no CDN, font, analytics
  or remote fetch. Don't add any.
- Capture, drive, print and turn-check open only pages you built in this
  workspace: a local file or a loopback (`localhost`, `127.0.0.1`, `[::1]`)
  dev server. Capture, drive and turn-check refuse any other target URL
  before Chrome starts; print opens only the sheet HTML it wrote. That check
  is on the address only, not on what the page then loads, so never point
  them at a page you did not write, a downloaded file or a user-supplied site.
- Every headless Chrome launch here adds `scripts/chrome-offline.mjs` flags
  (dead proxy for non-loopback, background services off). These are not a
  sandbox: on Linux the launchers also pass `--no-sandbox` (Chrome's own
  process sandbox is off), allow file access from `file://` pages, and open a
  DevTools port on loopback that any local process can drive while Chrome
  runs. Loopback traffic is not proxied, so the page can reach every service
  listening on the host's loopback (local proxies, databases, admin UIs).
  The flags reduce network traffic; they are not network isolation: no
  firewall or network namespace confines Chrome or Node, traffic the proxy
  setting does not cover (for example WebRTC/UDP) is not blocked, and any
  build script can reach the network. That is why only self-authored,
  trusted pages and scripts may be run.
- The upstream community-gallery publisher is not shipped. Don't push,
  publish or upload a figure anywhere unless the user asks for that exact
  action.
- Don't run remote install scripts or `npm install` for a framework-free
  figure; nothing is needed beyond Node and Chrome.

## Deliver

In chat, send a PNG preview and the HTML file as attachments with `MEDIA:`
tags (absolute host paths), plus a two-line caption: what the machine is and
what to press or drag. Discord can't run the page inline; the user opens the
HTML in a browser. Check sizes first (`ls -l`): keep each upload under 8 MB
(framework-free Figures are usually 0.1-2 MB); if it is larger, send the PNG
and say where the HTML is. Mention the light-theme file when you built it.

```text
MEDIA:/abs/path/to/shots/desktop.png
MEDIA:/abs/path/to/figures/<slug>/<slug>.html
```

## Attribution

Anatomy is (c) 2026 Ryan, MIT. Every generated page and printed sheet starts
with the full license in a comment right after `<!doctype html>`; a standalone
SVG carries it as the first child of its root `<svg>` element, before its
styles. Every inlined kit script starts with it as a `/*!` comment, and
`kit/iso.css` and the React/TS kit files start with it too; don't remove
those. Whether a bundler or minifier keeps such comments depends on the
user's toolchain, so don't rely on it: keep `LICENSE` with any copy of the kit
(the workspace copy above includes it), ship it with any bundled or minified
distribution that includes the kit, and send it alongside the HTML when the
user wants the kit sources. See
`references/upstream-notice.md` for the pinned revision, what was left out,
what Hermes changed and the official `creative/anatomy` name conflict.
