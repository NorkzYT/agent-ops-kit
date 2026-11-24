// Shared paths and the owned-fixture catalogue. Reports go to a new run
// directory under reports/ (gitignored) or --out (lib/output.mjs); tool homes
// and Chrome profiles are per-run temp dirs.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const FIXTURES = path.join(ROOT, 'fixtures');
export const REPORTS = path.join(ROOT, 'reports');
export const CHROME = process.env.UI_AUDIT_CHROME || '/usr/bin/google-chrome';

// Owned pages only. Scripts take one of these names, never a free-form URL.
export const PAGES = {
  bad: 'queue-bad.html',          // seeded: unlabeled input + low-contrast text
  clean: 'queue-clean.html',      // control: no seeded defects
  regression: 'queue-regression.html', // Lighthouse regression: no meta description
};

export function pageFile(name) {
  if (!Object.hasOwn(PAGES, name)) {
    throw new Error(`unknown fixture page "${name}"; expected one of: ${Object.keys(PAGES).join(', ')}`);
  }
  return PAGES[name];
}

export function tmpBase() {
  const base = process.env.TMPDIR && process.env.TMPDIR.trim() ? process.env.TMPDIR : os.tmpdir();
  fs.mkdirSync(base, { recursive: true });
  return base;
}

export function mkTmp(prefix) {
  return fs.mkdtempSync(path.join(tmpBase(), `ui-audit-${prefix}-`));
}

export function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}
