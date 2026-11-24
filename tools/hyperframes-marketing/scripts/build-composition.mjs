#!/usr/bin/env node
// Builds an editable HyperFrames project (index.html + assets/) from the same
// typed campaign props the legacy Remotion lane uses. Only assets named in the
// props are copied, and only after the caller has passed the provenance gate.
// The page uses CSS keyframes only: no GSAP, no CDN script, no web font URL,
// so the page references nothing remote (the HyperFrames CLI is not sandboxed
// from the network; see README "Network"). Text must use a family with a
// local @font-face built from props.typography: HyperFrames 0.9.80 rewrites a
// generic primary family (sans-serif, system-ui, ...) to "Inter" and fetches
// Inter from Google Fonts at compile time, and aliases other named families the
// same way. check-html-assets.mjs enforces a local face as the primary family.
import {copyFileSync, existsSync, lstatSync, mkdirSync, rmSync, writeFileSync} from 'node:fs';
import {dirname, join, normalize, isAbsolute, resolve, sep} from 'node:path';
import {usedAssetIds} from '../../remotion-marketing/scripts/lib.mjs';

export const DIMENSIONS = {
  '16:9': {width: 1920, height: 1080},
  '9:16': {width: 1080, height: 1920},
  '1:1': {width: 1080, height: 1080},
};

const FONT_EXT = /\.(ttf|otf|woff2?)$/i;
const FONT_FORMAT = {ttf: 'truetype', otf: 'opentype', woff: 'woff', woff2: 'woff2'};
const FAMILY_RE = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$/;

const IMAGE_EXT = /\.(png|jpe?g|webp|svg)$/i;
const VIDEO_EXT = /\.(mp4|webm|mov)$/i;

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

const HEX = /^#[0-9a-fA-F]{3,8}$/;
function color(value, fallback) {
  return HEX.test(value ?? '') ? value : fallback;
}

function safeId(id) {
  if (!/^[A-Za-z0-9_-]+$/.test(id ?? '')) throw new Error(`unsafe id "${id}"`);
  return id;
}

/** Reject absolute paths, `..` escapes and URLs in assetPaths. */
export function safeAssetRel(rel) {
  if (typeof rel !== 'string' || rel === '') throw new Error('empty asset path');
  if (/^[a-z][a-z0-9+.-]*:/i.test(rel) || rel.startsWith('//')) {
    throw new Error(`asset path "${rel}" is a URL; only local files are allowed`);
  }
  if (isAbsolute(rel)) throw new Error(`asset path "${rel}" is absolute`);
  if (rel.includes('\0')) throw new Error(`asset path "${rel}" contains a NUL byte`);
  const n = normalize(rel);
  if (n.startsWith('..')) throw new Error(`asset path "${rel}" escapes the assets directory`);
  if (n === '.' || n.endsWith(sep)) throw new Error(`asset path "${rel}" does not name a file`);
  return n;
}

/**
 * `safeAssetRel` plus a filesystem check: the path is joined to `assetsRoot`
 * and no existing component below the root may be a symlink (or a non-directory
 * where a directory is needed), so neither a read nor a write can be redirected
 * outside the root. Missing components are fine (the caller may create them).
 * The root itself may be a symlink: it is the caller's explicit choice.
 * This does not defend against a concurrent local writer racing the check.
 */
export function resolveAssetPath(assetsRoot, rel) {
  const clean = safeAssetRel(rel);
  const parts = clean.split(sep);
  let cur = resolve(assetsRoot);
  for (let i = 0; i < parts.length; i++) {
    cur = join(cur, parts[i]);
    let st;
    try { st = lstatSync(cur); } catch (e) {
      if (e.code === 'ENOENT') break;
      throw e;
    }
    if (st.isSymbolicLink()) throw new Error(`asset path "${rel}" goes through a symlink at ${cur}; symlinks under the assets directory are refused`);
    if (i < parts.length - 1 && !st.isDirectory()) throw new Error(`asset path "${rel}": ${cur} is not a directory`);
    if (i === parts.length - 1 && !st.isFile()) throw new Error(`asset path "${rel}": ${cur} is not a regular file`);
  }
  return join(resolve(assetsRoot), clean);
}

/** Create the parent directories of `rel` under the root, re-checking for symlinks afterwards. */
export function ensureAssetParent(assetsRoot, rel) {
  const abs = resolveAssetPath(assetsRoot, rel);
  mkdirSync(dirname(abs), {recursive: true});
  return resolveAssetPath(assetsRoot, rel);
}

function sec(frames, fps) {
  return Number((frames / fps).toFixed(4));
}

/** Pure: props → {html, assets:[{id, rel}], durationSec}. */
export function renderHtml(props, {aspectRatio} = {}) {
  const ratio = aspectRatio ?? props.aspectRatio ?? '16:9';
  const dims = DIMENSIONS[ratio];
  if (!dims) throw new Error(`unknown aspect ratio "${ratio}"`);
  const fps = props.fps;
  if (!Number.isInteger(fps) || fps <= 0) throw new Error('props.fps must be a positive integer');
  const brand = props.brand ?? {};
  const primary = color(brand.primaryColor, '#4338ca');
  const secondary = color(brand.secondaryColor, '#0ea5e9');
  const accent = color(brand.accentColor, '#f59e0b');
  const paths = props.assetPaths ?? {};
  const assets = [];
  const assetRef = (id) => {
    const rel = paths[id];
    if (!rel) throw new Error(`asset "${id}" has no entry in assetPaths`);
    const clean = safeAssetRel(rel);
    assets.push({id, rel: clean});
    return `assets/${clean.split('\\').join('/')}`;
  };
  const typo = props.typography;
  if (!typo || !FAMILY_RE.test(typo.family ?? '') || !Array.isArray(typo.faces) || typo.faces.length === 0) {
    throw new Error('props.typography {family, faces:[{assetId, weight, style}]} is required: HyperFrames fetches Google Fonts for generic families');
  }
  const fontFaces = typo.faces.map((face) => {
    const src = assetRef(face.assetId);
    const ext = (src.match(FONT_EXT) ?? [])[1]?.toLowerCase();
    if (!ext) throw new Error(`font asset "${face.assetId}" is not a .ttf/.otf/.woff/.woff2 file`);
    const weight = Number.isInteger(face.weight) && face.weight >= 100 && face.weight <= 900 ? face.weight : 400;
    const style = face.style === 'italic' ? 'italic' : 'normal';
    return `      @font-face { font-family: "${typo.family}"; src: url("${escapeHtml(src)}") format("${FONT_FORMAT[ext]}"); font-weight: ${weight}; font-style: ${style}; }`;
  }).join('\n');
  const facts = new Map((props.facts ?? []).map((f) => [f.id, f]));
  const portrait = dims.height > dims.width;
  const base = Math.round(Math.min(dims.width, dims.height) / 1080 * 100) / 100;

  let cursor = 0;
  const scenes = [];
  const captions = [];
  for (const scene of props.scenes ?? []) {
    const id = safeId(scene.id);
    const start = sec(cursor, fps);
    const dur = sec(scene.durationInFrames, fps);
    cursor += scene.durationInFrames;
    const factHtml = (scene.factIds ?? [])
      .map((fid) => facts.get(fid))
      .filter(Boolean)
      .map((f, i) => `<li class="fact" style="animation-delay:${(0.35 + i * 0.15).toFixed(2)}s">${escapeHtml(f.claim)}</li>`)
      .join('');
    let media = '';
    if (scene.mediaAssetId) {
      const src = assetRef(scene.mediaAssetId);
      if (IMAGE_EXT.test(src)) media = `<img class="media" src="${escapeHtml(src)}" alt="">`;
      else if (VIDEO_EXT.test(src)) media = `<video class="media" src="${escapeHtml(src)}" muted playsinline></video>`;
      else throw new Error(`scene "${id}" media "${src}" is not an image or video`);
    }
    const cta = scene.kind === 'outro' && props.cta
      ? `<div class="cta">${escapeHtml(props.cta.text)}<span class="url">${escapeHtml(props.cta.url ?? '')}</span></div>`
      : '';
    scenes.push(`    <section id="scene-${id}" class="clip scene kind-${escapeHtml(scene.kind)}" data-start="${start}" data-duration="${dur}" data-track-index="0" data-track-kind="graphics">
      <div class="safe">
        ${media}
        <h1 class="headline">${escapeHtml(scene.headline)}</h1>
        ${scene.subhead ? `<p class="subhead">${escapeHtml(scene.subhead)}</p>` : ''}
        ${factHtml ? `<ul class="facts">${factHtml}</ul>` : ''}
        ${cta}
      </div>
    </section>`);
    if (props.captions && scene.caption) {
      captions.push(`    <div id="caption-${id}" class="clip caption" data-start="${start}" data-duration="${dur}" data-track-index="1" data-track-kind="captions">${escapeHtml(scene.caption)}</div>`);
    }
  }
  const durationSec = sec(cursor, fps);
  if (durationSec <= 0) throw new Error('props.scenes is empty');

  let logo = '';
  if (brand.logoAssetId) {
    logo = `    <img id="logo" class="clip logo" src="${escapeHtml(assetRef(brand.logoAssetId))}" alt="${escapeHtml(brand.name)}" data-start="0" data-duration="${durationSec}" data-track-index="2">`;
  }
  let music = '';
  if (props.music && props.music.mode !== 'none' && props.music.assetId) {
    music = `    <audio id="music" class="clip" src="${escapeHtml(assetRef(props.music.assetId))}" data-start="0" data-duration="${durationSec}" data-track-index="3"></audio>`;
  }

  const safe = props.safeMargins === false ? 0 : 0.05;
  const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=${dims.width}, height=${dims.height}">
    <title>${escapeHtml(props.product?.name ?? brand.name ?? 'Marketing video')}</title>
    <!-- Generated by tools/hyperframes-marketing/scripts/build-composition.mjs.
         Editable, but re-run the asset gate (check-html-assets.mjs) after any edit:
         every src must be a provenance-cleared file under assets/, no URLs. -->
    <style>
${fontFaces}
      :root { --primary: ${primary}; --secondary: ${secondary}; --accent: ${accent}; --u: ${base}px; }
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { width: ${dims.width}px; height: ${dims.height}px; overflow: hidden; background: #0b1020; }
      #root { position: relative; width: 100%; height: 100%; font-family: "${typo.family}", sans-serif; color: #fff;
        background: linear-gradient(135deg, var(--primary), var(--secondary)); }
      .scene { position: absolute; inset: 0; }
      .safe { position: absolute; inset: ${safe * 100}%; display: flex; flex-direction: column; justify-content: center;
        align-items: ${portrait ? 'center' : 'flex-start'}; text-align: ${portrait ? 'center' : 'left'}; gap: calc(24 * var(--u)); padding-bottom: calc(150 * var(--u)); }
      .headline { font-size: calc(${portrait ? 92 : 104} * var(--u)); font-weight: 700; line-height: 1.05; letter-spacing: -0.02em; max-width: 92%;
        animation: rise 0.7s cubic-bezier(.2,.8,.2,1) both; }
      .subhead { font-size: calc(48 * var(--u)); opacity: 0.9; animation: rise 0.7s 0.15s cubic-bezier(.2,.8,.2,1) both; }
      .facts { list-style: none; display: flex; flex-direction: column; gap: calc(14 * var(--u)); }
      .fact { font-size: calc(38 * var(--u)); padding: calc(10 * var(--u)) calc(22 * var(--u)); border-left: calc(8 * var(--u)) solid var(--accent);
        background: rgba(0,0,0,0.22); animation: rise 0.6s cubic-bezier(.2,.8,.2,1) both; }
      .cta { font-size: calc(56 * var(--u)); font-weight: 700; background: var(--accent); color: #111; padding: calc(18 * var(--u)) calc(36 * var(--u));
        border-radius: calc(16 * var(--u)); animation: pop 0.6s 0.3s cubic-bezier(.2,.8,.2,1) both; display: flex; flex-direction: column; gap: calc(6 * var(--u)); }
      .cta .url { font-size: calc(28 * var(--u)); font-weight: 400; }
      .media { max-width: 70%; max-height: 45%; object-fit: contain; border-radius: calc(12 * var(--u)); animation: fade 0.6s both; }
      .logo { position: absolute; top: ${safe * 100 + 1}%; right: ${safe * 100 + 1}%; height: calc(72 * var(--u)); }
      .caption { position: absolute; left: 10%; right: 10%; bottom: 10%; text-align: center; font-size: calc(36 * var(--u)); line-height: 1.3;
        background: rgba(0,0,0,0.6); padding: calc(12 * var(--u)) calc(20 * var(--u)); border-radius: calc(10 * var(--u)); }
      @keyframes rise { from { opacity: 0; transform: translateY(calc(40 * var(--u))); } to { opacity: 1; transform: none; } }
      @keyframes pop { from { opacity: 0; transform: scale(0.85); } to { opacity: 1; transform: none; } }
      @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="${durationSec}" data-width="${dims.width}" data-height="${dims.height}" data-fps="${fps}" data-no-timeline>
${scenes.join('\n')}
${captions.join('\n')}
${logo}
${music}
    </div>
  </body>
</html>
`;
  return {html, assets, durationSec, dims, ratio};
}

/**
 * Write the project. `assetsRoot` is where assetPaths resolve. The project dir
 * is recreated so stale files from an earlier build cannot ride along.
 */
export function buildProject(props, {outDir, assetsRoot, aspectRatio}) {
  const result = renderHtml(props, {aspectRatio});
  // Every copied asset must be one the props declare (the provenance gate
  // checked exactly this set).
  const declared = usedAssetIds(props);
  for (const a of result.assets) {
    if (!declared.has(a.id)) throw new Error(`asset "${a.id}" is not declared by the props`);
  }
  rmSync(outDir, {recursive: true, force: true});
  mkdirSync(join(outDir, 'assets'), {recursive: true});
  for (const a of result.assets) {
    const from = resolveAssetPath(assetsRoot, a.rel);
    if (!existsSync(from)) throw new Error(`asset "${a.id}" file missing: ${from}`);
    const to = join(outDir, 'assets', a.rel);
    mkdirSync(dirname(to), {recursive: true});
    copyFileSync(from, to);
  }
  writeFileSync(join(outDir, 'index.html'), result.html);
  return result;
}
