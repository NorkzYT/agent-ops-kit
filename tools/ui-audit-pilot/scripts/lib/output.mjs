// Report output: every run writes into a NEW run directory and never deletes
// or overwrites anything that was already there.
//
// resolveOutBase(out) judges a caller-supplied --out (lexical and real path):
// refused are the filesystem root, $HOME or any ancestor of it, hidden
// directories under $HOME, a hidden basename, system trees, an existing
// non-directory, a final component that is a symlink, and (project runs) a
// directory inside the served root or the root / an ancestor of it.
// createRunDir(base) then makes base/<run-id>/ (mkdir, never rm). Report files
// are written with writeNew (O_EXCL), so a name collision is an error, not a
// silent clobber.
import { randomBytes } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export class OutputRefused extends Error {}

export const SYSTEM = ['/proc', '/sys', '/dev', '/etc', '/boot', '/root', '/run', '/var/run', '/bin', '/sbin', '/lib', '/lib64', '/usr', '/snap'];
export const within = (p, base) => p === base || p.startsWith(base.endsWith(path.sep) ? base : base + path.sep);
export const hidden = (rel) => rel.split(/[\\/]/).some((s) => s.startsWith('.') && s !== '.');
const refuse = (msg) => { throw new OutputRefused(`refused output: ${msg}`); };
export const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/;

function realOrSelf(p) {
  try { return fs.realpathSync(p); } catch { return path.resolve(p); }
}

/** Real path of the nearest existing ancestor joined with the not-yet-created tail. */
export function realTarget(dir) {
  let cur = dir;
  const tail = [];
  for (;;) {
    try { return path.join(fs.realpathSync(cur), ...tail.reverse()); } catch { /* not yet */ }
    const up = path.dirname(cur);
    if (up === cur) return dir;
    tail.push(path.basename(cur));
    cur = up;
  }
}

function judge(p, { home, root }) {
  if (p === path.parse(p).root) refuse('the filesystem root is too broad');
  for (const s of SYSTEM) if (within(p, s)) refuse(`${p} is inside the system tree ${s}`);
  if (home) {
    if (within(home, p)) refuse(`${p} is $HOME or contains it; pass a dedicated report directory`);
    if (within(p, home) && hidden(path.relative(home, p))) refuse(`${p} is inside a hidden directory under $HOME`);
  }
  if (hidden(path.basename(p))) refuse(`${p} is a hidden directory`);
  if (root) {
    if (within(p, root)) refuse(`${p} is inside the served project; reports would be served`);
    if (within(root, p)) refuse(`${p} is the served project or contains it`);
  }
}

/**
 * Validate a report base directory. `out` (caller-supplied, resolved against
 * cwd) gets every check; without `out`, `fallback` (a tool-owned directory)
 * only gets the served-root checks.
 */
export function resolveOutBase(out, { root = null, home = process.env.HOME || os.homedir(), fallback } = {}) {
  const realRoot = root ? realOrSelf(root) : null;
  if (!out) {
    if (!fallback) refuse('no output directory');
    const dir = path.resolve(fallback);
    for (const p of [dir, realTarget(dir)]) {
      if (realRoot && (within(p, realRoot) || within(realRoot, p))) refuse(`${p} overlaps the served project; pass --out`);
    }
    return dir;
  }
  if (typeof out !== 'string' || out.includes('\0')) refuse(`bad --out ${JSON.stringify(out)}`);
  const dir = path.resolve(out);
  let st = null;
  try { st = fs.lstatSync(dir); } catch { /* created later */ }
  if (st?.isSymbolicLink()) refuse(`${dir} is a symlink; pass the real directory`);
  if (st && !st.isDirectory()) refuse(`${dir} exists and is not a directory`);
  const h = home ? realOrSelf(home) : null;
  const opts = { home: h, root: realRoot };
  judge(dir, { ...opts, home: home ? path.resolve(home) : null });
  judge(realTarget(dir), opts);
  return dir;
}

export function newRunId(now = new Date()) {
  return `${now.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z')}-${process.pid}-${randomBytes(3).toString('hex')}`;
}

/**
 * Create base/<run-id>/ and return it. A generated id must be new. An explicit
 * id (shared by the three runners of one ui-audit-project run) may already
 * exist as a real directory; files inside are still created exclusively.
 */
export function createRunDir(base, runId) {
  if (runId !== undefined && !RUN_ID.test(String(runId))) refuse(`run id ${JSON.stringify(runId)} must match ${RUN_ID}`);
  fs.mkdirSync(base, { recursive: true });
  const bst = fs.lstatSync(base);
  if (bst.isSymbolicLink() || !bst.isDirectory()) refuse(`${base} is not a real directory`);
  const realBase = fs.realpathSync(base);
  for (let i = 0; i < 5; i += 1) {
    const id = runId ?? newRunId();
    const dir = path.join(base, id);
    try {
      fs.mkdirSync(dir, { mode: 0o755 });
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      if (runId === undefined) continue;
      const st = fs.lstatSync(dir);
      if (st.isSymbolicLink() || !st.isDirectory()) refuse(`${dir} exists and is not a real directory`);
    }
    if (fs.realpathSync(dir) !== path.join(realBase, id)) refuse(`${dir} resolved outside ${realBase}`);
    return dir;
  }
  refuse(`could not create a new run directory in ${base}`);
}

/** Write a new file; refuses (EEXIST) instead of overwriting. */
export function writeNew(file, data) {
  try {
    fs.writeFileSync(file, data, { flag: 'wx' });
  } catch (err) {
    if (err.code === 'EEXIST') refuse(`${file} already exists; not overwriting`);
    throw err;
  }
  return file;
}

/** Create a new directory that must not exist yet (no rm, no reuse). */
export function mkdirNew(dir) {
  try {
    fs.mkdirSync(dir);
  } catch (err) {
    if (err.code === 'EEXIST') refuse(`${dir} already exists; not reusing or deleting it`);
    throw err;
  }
  return dir;
}

/**
 * Validate --out / --run-id for a runner and create its run directory, before
 * any server or browser starts. Fixture runs without --out use the tool-owned
 * reports/ directory; project runs use projectArgs().outDir.
 */
export function runDirFor(values, project, { fallback } = {}) {
  const base = project ? project.outDir : resolveOutBase(values.out, { fallback });
  return createRunDir(base, values['run-id']);
}

/** Refuse duplicate report names (two --page/--page-path that map to one file). */
export function assertUniqueNames(names) {
  const seen = new Set();
  for (const n of names) {
    if (seen.has(n)) refuse(`two selected pages map to the same report name "${n}"`);
    seen.add(n);
  }
}
