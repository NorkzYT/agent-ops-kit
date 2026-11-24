// User-supplied project target: a local directory of static pages that the
// pilot serves itself (lib/server.mjs, 127.0.0.1, ephemeral port). This is the
// supported way to audit your own UI. Only the navigation TARGET is restricted:
// arbitrary localhost URLs are refused as targets (lib/target.mjs), but a
// selected page can still load subresources from other 127.0.0.1 ports, and
// there is no network isolation (README "Boundaries").
//
// Guards, all checked before a server or browser starts:
// - the root is an existing absolute directory, judged by its real path;
// - not "/", not $HOME or any ancestor of it, not a system tree;
// - no hidden component below $HOME (~/.ssh, ~/.config, ~/.hermes, ...) and no
//   hidden component in the project-relative page path (.git, .env, ...);
// - pages are existing .html files whose real path stays inside the root;
// - reports go to a new run directory outside the served root (lib/output.mjs).
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SYSTEM, hidden, resolveOutBase, within } from './output.mjs';
import { REPORTS } from './paths.mjs';

export class ProjectRefused extends Error {}

const refuse = (msg) => { throw new ProjectRefused(`refused project: ${msg}`); };

function realHome(home) {
  try { return fs.realpathSync(home); } catch { return path.resolve(home); }
}

export function resolveProjectRoot(dir, { home = process.env.HOME || os.homedir() } = {}) {
  if (typeof dir !== 'string' || !dir || !path.isAbsolute(dir)) refuse(`directory must be an absolute path (got ${JSON.stringify(dir)})`);
  let real;
  try { real = fs.realpathSync(dir); } catch { refuse(`${dir} does not exist`); }
  if (!fs.statSync(real).isDirectory()) refuse(`${dir} is not a directory`);
  if (real === path.parse(real).root) refuse('the filesystem root is too broad');
  for (const s of SYSTEM) if (within(real, s)) refuse(`${real} is inside the system tree ${s}`);
  const h = home ? realHome(home) : null;
  if (h) {
    if (within(h, real)) refuse(`${real} is $HOME or contains it; pass the project directory itself`);
    if (within(real, h) && hidden(path.relative(h, real))) refuse(`${real} is inside a hidden directory under $HOME`);
  }
  if (hidden(path.basename(real))) refuse(`${real} is a hidden directory`);
  return real;
}

export function resolveProjectPages(root, pages) {
  const list = pages?.length ? pages : ['index.html'];
  return list.map((rel) => {
    if (typeof rel !== 'string' || !rel || path.isAbsolute(rel) || /[\\?#\0]/.test(rel)) refuse(`page path must be relative, without \\ ? # (got ${JSON.stringify(rel)})`);
    const segs = rel.split('/');
    if (segs.some((s) => s === '' || s === '.' || s === '..' || s.startsWith('.'))) refuse(`page path ${rel} has an empty, dot or hidden segment`);
    if (!rel.endsWith('.html')) refuse(`page path ${rel} is not an .html page`);
    let real;
    try { real = fs.realpathSync(path.join(root, rel)); } catch { refuse(`page ${rel} does not exist in the project`); }
    if (!within(real, root) || real === root || hidden(path.relative(root, real))) refuse(`page ${rel} resolves outside the project or into a hidden path`);
    if (!fs.statSync(real).isFile()) refuse(`page ${rel} is not a file`);
    return rel;
  });
}

export function pageName(rel) {
  return rel.replace(/\.html$/, '').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase() || 'page';
}

/**
 * Report base directory for a project run (lib/output.mjs guards). The default
 * carries a hash of the real root, so two projects with the same basename never
 * share a directory. Each run still writes into its own new run directory.
 */
export function projectOutDir(root, out, opts = {}) {
  const tag = createHash('sha256').update(root).digest('hex').slice(0, 10);
  return resolveOutBase(out, { ...opts, root, fallback: path.join(REPORTS, `project-${pageName(path.basename(root))}-${tag}`) });
}

/** URLs and the allowed path set for selected pages on a running pilot server. */
export function projectTarget(srv, pages) {
  const out = pages.map((rel) => {
    const url = `${srv.origin}/${rel.split('/').map(encodeURIComponent).join('/')}`;
    return { name: pageName(rel), rel, url, pathname: new URL(url).pathname };
  });
  const names = new Set(out.map((p) => p.name));
  if (names.size !== out.length) refuse('two selected pages map to the same report name');
  return { pages: out, allowed: new Set(out.map((p) => p.pathname)) };
}

/** Parse --project / --page-path / --out; returns null when --project is absent. */
export function projectArgs(values) {
  if (!values.project) {
    if (values['page-path']?.length) refuse('--page-path needs --project');
    return null;
  }
  if (values.url || values.page?.length || values.file?.length) refuse('--project cannot be combined with --url, --page or --file');
  const root = resolveProjectRoot(values.project);
  const pages = resolveProjectPages(root, values['page-path']);
  // Same check as projectTarget(), before any run directory or server exists.
  if (new Set(pages.map(pageName)).size !== pages.length) refuse('two selected pages map to the same report name');
  return { root, pages, outDir: projectOutDir(root, values.out) };
}

export const PROJECT_OPTIONS = { project: { type: 'string' }, 'page-path': { type: 'string', multiple: true }, 'run-id': { type: 'string' } };
