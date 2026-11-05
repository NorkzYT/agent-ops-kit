# Design package: First-step guide

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-design` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-design` sha256 `7935bcb28c35273a…` as of 2026-11-04

Files: `design-tokens.json` (machine-readable tokens), `design-preview.html` (self-contained preview: no script, no remote asset, no web fonts).

## Colour tokens
### light theme
| Token | Value |
|---|---|
| `--color-bg` | `#f7f7f4` |
| `--color-surface` | `#ffffff` |
| `--color-text` | `#1f2a37` |
| `--color-text-muted` | `#4b5563` |
| `--color-accent` | `#0f766e` |
| `--color-on-accent` | `#ffffff` |
| `--color-border` | `#7c8594` |
| `--color-focus` | `#b45309` |
| `--color-success` | `#15803d` |
| `--color-warning` | `#a16207` |
| `--color-danger` | `#b91c1c` |
| `--color-info` | `#1d4ed8` |

Contrast, computed from the tokens:
- text on bg: 13.54:1 (min 4.5:1) pass
- text on surface: 14.54:1 (min 4.5:1) pass
- text-muted on bg: 7.04:1 (min 4.5:1) pass
- text-muted on surface: 7.56:1 (min 4.5:1) pass
- accent on bg: 5.10:1 (min 4.5:1) pass
- accent on surface: 5.47:1 (min 4.5:1) pass
- success on bg: 4.67:1 (min 4.5:1) pass
- success on surface: 5.02:1 (min 4.5:1) pass
- warning on bg: 4.59:1 (min 4.5:1) pass
- warning on surface: 4.92:1 (min 4.5:1) pass
- danger on bg: 6.03:1 (min 4.5:1) pass
- danger on surface: 6.47:1 (min 4.5:1) pass
- info on bg: 6.24:1 (min 4.5:1) pass
- info on surface: 6.70:1 (min 4.5:1) pass
- on-accent on accent: 5.47:1 (min 4.5:1) pass
- border on bg: 3.47:1 (min 3:1) pass
- border on surface: 3.72:1 (min 3:1) pass
- focus on bg: 4.68:1 (min 3:1) pass
- focus on surface: 5.02:1 (min 3:1) pass

## Typography
- display: Source Sans 3 (sans-serif fallback), weights 600, 700; verified by D1 (synthetic fixture)
- body: Source Sans 3 (sans-serif fallback), weights 400, 600; verified by D1 (synthetic fixture)
- mono: JetBrains Mono (monospace fallback), weights 400; verified by D1 (synthetic fixture)
- Scale: base 16px × 1.25 → 16px, 20px, 25px, 31px, 39px, 49px; body line height 1.5; measure 66 characters

## Spacing, radius and motion
- Spacing scale: 4px, 8px, 12px, 16px, 24px, 32px, 48px
- Radius: small 2px, medium 4px, large 8px
- Motion: 120ms / 240ms; enter `cubic-bezier(0.16, 1, 0.3, 1)`, move `cubic-bezier(0.65, 0, 0.35, 1)`; reduced motion shows the final state

## Interaction states
| Screen | State | Message | Recovery |
|---|---|---|---|
| guide | default | (the screen itself) | - |
| guide | empty | No project yet. The guide creates your first one in three steps. | Start step 1 |
| guide | loading | Loading your workspace. | - |
| guide | error | The project was not created: that name is already used in this workspace. Choose another name and try again. | Edit the name |
| guide | success | Project created. Next: add a first task. | - |
| help | default | (the screen itself) | - |
| help | empty | You have not dismissed the guide, so there is nothing to reopen. | Browse help topics |
| help | loading | Loading help topics. | - |
| help | error | Help topics did not load. Check your connection, then reload the page. | Reload help |

## Responsive behaviour
| Breakpoint | From | Columns | Navigation | Notes |
|---|---|---|---|---|
| mobile | 0px | 1 | collapsible-menu | The guide becomes a full-width sheet above the workspace; actions stack, primary first. |
| tablet | 768px | 2 | top-bar | The guide docks beside the workspace. |
| desktop | 1024px | 2 | sidebar | The guide keeps a fixed column; the workspace takes the rest. |
Max content width 1120px.

## Accessibility requirements (target WCAG 2.2 AA)
- `a11y-contrast` contrast: Text and status colours meet 4.5:1 on background and surface; borders and the focus ring meet 3:1. — static check on the preview: contrast computed from the tokens: pass
- `a11y-focus` focus-visible: Every control shows a 3px focus ring in the focus colour; no outline is removed without a replacement. — static check on the preview: focus ring rule present in the preview CSS
- `a11y-keyboard` keyboard: The guide can be completed, skipped and reopened with the keyboard alone, in visual order. — not verified (needs browser)
- `a11y-target` target-size: Buttons and inputs are at least 44 by 44 CSS pixels. — not verified (needs browser)
- `a11y-motion` reduced-motion: With reduced motion requested, transitions are removed and the final state shows at once. — static check on the preview: reduced-motion rule present in the preview CSS
- `a11y-landmarks` landmarks: Header, nav, main and footer landmarks; a skip link is the first focusable element. — static check on the preview: landmarks and skip link present in the preview markup
- `a11y-errors` form-errors: The project-name error is tied to the field with aria-invalid and aria-describedby and says how to fix it. — static check on the preview: aria-invalid/aria-describedby wiring present in the preview markup
- `a11y-resize` text-resize: At 200 percent text size and 320 CSS pixels wide nothing is clipped and no horizontal scroll appears. — not verified (needs browser)
- `a11y-status` status-messages: Loading and success messages are announced politely; errors are announced assertively. — not verified (needs assistive-tech)

## Asset rights
- font Source Sans 3: licensed (Company design kit licence (synthetic fixture); source D1)
- font JetBrains Mono: licensed (Company design kit licence (synthetic fixture); source D1)
- The preview embeds no images, icons, illustrations, logos or font files.

## Not verified
- Browser render and screenshots: not run (the preview is checked statically only).
- Assistive technology and keyboard walk-through: not run.
- Usability with real users: not run.
- Typefaces are not loaded; the preview uses them only when installed locally.
