#!/usr/bin/env node
// Pre-render asset gate for a HyperFrames project. HyperFrames pages are
// editable HTML, so the props-level provenance gate alone is not enough: a hand
// edit can add an <img>, a CSS url() or a CDN script. This gate scans every
// .html/.css file in the project and every file under assets/, and fails when:
//   - a reference is a URL (http:, https:, //, data: with a remote import, ...)
//   - a reference leaves the project or is absolute
//   - a referenced file is not a provenance-cleared asset (commercialUse true)
//   - a file sits in assets/ without a cleared provenance record
// It runs immediately before `hyperframes render`, after any edit.
import {readdirSync, readFileSync, statSync} from 'node:fs';
import {join, normalize, relative, sep, isAbsolute, extname} from 'node:path';
import {parseArgs, loadJson, finish} from '../../remotion-marketing/scripts/lib.mjs';
import {COMMERCIAL_LICENSES} from '../../remotion-marketing/scripts/check-provenance.mjs';

const ATTR_RE = /\b(?:src|href|poster|data-src|data-composition-src|xlink:href|data|action|formaction|background|ping|manifest|cite|longdesc|archive|codebase|icon)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi;
const SRCSET_RE = /\bsrcset\s*=\s*(?:"([^"]*)"|'([^']*)')/gi;
const CSS_URL_RE = /url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)/gi;
const IMPORT_RE = /@import\s+(?:url\()?\s*["']([^"']+)["']/gi;
const FONT_FAMILY_RE = /font-family\s*:\s*([^;}"]*(?:"[^"]*"[^;}"]*)*)/gi;
const FONT_FACE_RE = /@font-face\s*{[^}]*font-family\s*:\s*["']?([^;"'}]+)/gi;
const GENERIC_FAMILIES = new Set(['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui',
  'ui-serif', 'ui-sans-serif', 'ui-monospace', 'ui-rounded', 'math', 'emoji', 'fangsong', '-apple-system', 'blinkmacsystemfont']);
const CSS_WIDE = new Set(['inherit', 'initial', 'unset', 'revert', 'revert-layer']);
const JS_NET_RE = /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|importScripts|sendBeacon|RTCPeerConnection|FontFace)\s*\(|\bnew\s+(?:Shared)?Worker\s*\(|\bimport\s*\(|\bdocument\.write(?:ln)?\s*\(|\bwindow\.open\s*\(|\blocation(?:\.href)?\s*=[^=]/g;
// Only inside script code (inline <script> bodies and .js/.mjs files): URL
// literals, static imports, and DOM writes that would add a reference the
// static scan never sees, including runtime font changes.
const SCRIPT_RE = [
  [/["'`](?:[a-z][a-z0-9+.-]*:)?\/\/[^"'`\s]*/gi, 'URL literal'],
  [/\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?["'`]/g, 'static import'],
  [/\.(?:src|href|srcset|poster|data|action|innerHTML|outerHTML|cssText)\s*=[^=]/g, 'reference/markup assignment'],
  [/\b(?:setAttribute|insertAdjacentHTML|insertRule)\s*\(/g, 'dynamic attribute/markup/CSS'],
  [/\beval\s*\(|\bnew\s+Function\s*\(|\bset(?:Timeout|Interval)\s*\(\s*["'`]/g, 'dynamic code'],
  [/\.(?:fontFamily|font)\s*=[^=]|setProperty\s*\(\s*["'`](?:font|src|background)/g, 'runtime font/style change'],
];
const SCRIPT_BODY_RE = /<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi;
const META_REFRESH_RE = /<meta\b[^>]*http-equiv\s*=\s*["']?\s*refresh/gi;
const SRCDOC_RE = /\bsrcdoc\s*=/gi;
// `font:` shorthand (not `font-family:`, not a `--font` custom property).
const FONT_SHORTHAND_RE = /(?<![\w-])font\s*:\s*([^;}"]*(?:"[^"]*"[^;}"]*)*)/gi;
const SYSTEM_FONT_KEYWORDS = new Set(['caption', 'icon', 'menu', 'message-box', 'small-caption', 'status-bar']);
const FONT_SIZE_RE = /(?:^|\s)(?:[+-]?(?:\d+\.?\d*|\.\d+)(?:px|pt|pc|em|rem|ex|ch|cap|ic|lh|rlh|vw|vh|vmin|vmax|vi|vb|svh|lvh|dvh|cm|mm|in|q|%)|xx-small|x-small|small|medium|large|x-large|xx-large|xxx-large|smaller|larger|(?:calc|clamp|min|max|var)\([^)]*\))(?:\s*\/\s*\S+)?\s+/i;

/** Family list of a `font:` shorthand value, or {error}. */
export function shorthandFamilies(value) {
  const v = value.trim();
  const low = v.toLowerCase();
  if (CSS_WIDE.has(low) || low.startsWith('var(')) return {families: ''};
  if (SYSTEM_FONT_KEYWORDS.has(low)) return {error: `system font keyword "${low}" (resolved by the renderer, not a local @font-face)`};
  const m = v.match(FONT_SIZE_RE);
  if (!m) return {error: `font shorthand "${v}" could not be parsed`};
  return {families: v.slice(m.index + m[0].length)};
}

function walk(dir, root = dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, root, out);
    else out.push(relative(root, p).split(sep).join('/'));
  }
  return out;
}

function refsIn(text, isCss) {
  const refs = [];
  const push = (m) => {
    const v = (m[1] ?? m[2] ?? m[3] ?? '').trim();
    if (v) refs.push(v);
  };
  if (!isCss) {
    for (const m of text.matchAll(ATTR_RE)) push(m);
    for (const m of text.matchAll(SRCSET_RE)) {
      for (const part of (m[1] ?? m[2] ?? '').split(',')) {
        const v = part.trim().split(/\s+/)[0];
        if (v) refs.push(v);
      }
    }
  }
  for (const m of text.matchAll(CSS_URL_RE)) push(m);
  for (const m of text.matchAll(IMPORT_RE)) refs.push(m[1]);
  return refs;
}

function scanScript(path, code, violations) {
  for (const [re, what] of SCRIPT_RE) {
    for (const m of code.matchAll(re)) violations.push(`${path}: script ${what} "${m[0].trim()}" is not allowed in a render`);
  }
}

/**
 * Heuristic, not a sandbox: it reads markup, CSS and script text with regular
 * expressions. Obfuscated script (string concatenation, eval, CSS escapes) can
 * evade it; the render-time controls (private HOME, minimal env, Chrome proxy)
 * are separate layers, and neither layer is proof of no network egress.
 *
 * Pure check over an in-memory project.
 * @param files {Record<string,string|null>} project-relative path → text (null for binary)
 * @param provenance manifest with assets[{id, path, license, commercialUse}]
 * @param assetPaths props.assetPaths (id → path relative to assets/)
 */
export function checkHtmlAssets({files, provenance, assetPaths}) {
  const violations = [];
  const warnings = [];
  const cleared = new Map(); // project path -> asset id
  const byId = new Map((provenance.assets ?? []).map((a) => [a.id, a]));
  for (const [id, rel] of Object.entries(assetPaths ?? {})) {
    const a = byId.get(id);
    const ok = a && a.commercialUse === true && COMMERCIAL_LICENSES.has(a.license);
    if (ok) cleared.set(`assets/${normalize(rel).split(sep).join('/')}`, id);
  }

  for (const path of Object.keys(files)) {
    if (path.startsWith('assets/') && !cleared.has(path)) {
      violations.push(`file "${path}" is in assets/ without a provenance record cleared for commercial use`);
    }
  }

  for (const [path, text] of Object.entries(files)) {
    const ext = extname(path).toLowerCase();
    if (text === null || !['.html', '.htm', '.css', '.svg'].includes(ext)) continue;
    for (const ref of refsIn(text, ext === '.css')) {
      if (ref.startsWith('#')) continue; // fragment / SVG id
      if (/^data:/i.test(ref)) {
        violations.push(`${path}: inline data: URI "${ref.slice(0, 40)}…" bypasses provenance; ship it as a file in assets/`);
        continue;
      }
      if (/^[a-z][a-z0-9+.-]*:/i.test(ref) || ref.startsWith('//')) {
        violations.push(`${path}: external reference "${ref}" (renders must not fetch from the network)`);
        continue;
      }
      if (isAbsolute(ref) || ref.startsWith('/')) {
        violations.push(`${path}: absolute reference "${ref}" (use assets/...)`);
        continue;
      }
      const clean = normalize(ref.split(/[?#]/)[0]).split(sep).join('/');
      if (clean.startsWith('..')) {
        violations.push(`${path}: reference "${ref}" leaves the project`);
        continue;
      }
      if (clean.endsWith('.html') && files[clean] !== undefined && !clean.startsWith('assets/')) continue; // sub-composition
      if (!cleared.has(clean)) {
        violations.push(`${path}: "${ref}" is not a provenance-cleared asset`);
      } else if (files[clean] === undefined) {
        violations.push(`${path}: "${ref}" is cleared but the file is missing from the project`);
      }
    }
    // HyperFrames 0.9.80 resolves fonts at compile time: a generic primary
    // family (sans-serif, system-ui, ...) is rewritten to "Inter" and any named
    // family without a local @font-face is fetched from Google Fonts. So the
    // primary family must be a local @font-face, and fallbacks must be generic
    // or local. CSS-wide keywords (inherit, ...) are fine.
    const declared = new Set();
    for (const t of Object.values(files)) {
      if (typeof t !== 'string') continue;
      for (const m of t.matchAll(FONT_FACE_RE)) declared.add(m[1].trim().toLowerCase());
    }
    const familyLists = [...text.matchAll(FONT_FAMILY_RE)].map((m) => m[1]);
    for (const m of text.matchAll(FONT_SHORTHAND_RE)) {
      const r = shorthandFamilies(m[1]);
      if (r.error) violations.push(`${path}: ${r.error}; use a local @font-face family first`);
      else if (r.families) familyLists.push(r.families);
    }
    for (const list of familyLists) {
      const fams = list.split(',').map((raw) => raw.trim().replace(/^["']|["']$/g, '').trim().toLowerCase()).filter(Boolean);
      fams.forEach((fam, i) => {
        if (fam.startsWith('var(') || CSS_WIDE.has(fam) || declared.has(fam)) return;
        if (GENERIC_FAMILIES.has(fam)) {
          if (i === 0) violations.push(`${path}: primary font-family "${fam}" is generic; HyperFrames rewrites it to "Inter" and fetches Inter from Google Fonts. Put a local @font-face family first`);
          return;
        }
        violations.push(`${path}: font-family "${fam}" has no local @font-face (HyperFrames would fetch it from Google Fonts); ship the font file in assets/ with a cleared record`);
      });
    }
    if (ext !== '.css') {
      for (const m of text.matchAll(JS_NET_RE)) {
        violations.push(`${path}: script network call "${m[0]}" is not allowed in a render`);
      }
      for (const m of text.matchAll(META_REFRESH_RE)) violations.push(`${path}: meta refresh "${m[0]}" is not allowed`);
      for (const m of text.matchAll(SRCDOC_RE)) violations.push(`${path}: iframe srcdoc is not allowed (its markup escapes this scan)`);
      for (const m of text.matchAll(SCRIPT_BODY_RE)) scanScript(path, m[1], violations);
    }
  }
  // Script files anywhere in the project, referenced or not.
  for (const [path, text] of Object.entries(files)) {
    if (typeof text !== 'string' || !['.js', '.mjs', '.cjs'].includes(extname(path).toLowerCase())) continue;
    for (const m of text.matchAll(JS_NET_RE)) violations.push(`${path}: script network call "${m[0]}" is not allowed in a render`);
    scanScript(path, text, violations);
  }
  return {violations, warnings};
}

export function loadProjectFiles(dir) {
  const files = {};
  for (const rel of walk(dir)) {
    const ext = extname(rel).toLowerCase();
    files[rel] = ['.html', '.htm', '.css', '.svg', '.js', '.mjs', '.cjs'].includes(ext)
      ? readFileSync(join(dir, rel), 'utf8')
      : null;
  }
  return files;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.project || !args.props || !args.provenance) {
    console.error('usage: check-html-assets.mjs --project <dir> --props <props.json> --provenance <provenance.json>');
    process.exit(2);
  }
  const props = loadJson(args.props);
  const provenance = loadJson(args.provenance);
  const {violations, warnings} = checkHtmlAssets({
    files: loadProjectFiles(args.project),
    provenance,
    assetPaths: props.assetPaths,
  });
  finish('HyperFrames project asset gate', violations, warnings);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
