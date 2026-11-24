import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { auditPage } from '../scripts/run-axe.mjs';
import { launchChrome } from '../scripts/lib/chrome.mjs';
import { mkTmp, pageFile } from '../scripts/lib/paths.mjs';
import { startServer } from '../scripts/lib/server.mjs';
import { isolatedHome, runDirs, runScript } from './helpers.mjs';

let srv; let chrome; const reports = {};

test.before(async () => {
  srv = await startServer();
  chrome = await launchChrome();
  for (const p of ['bad', 'clean']) reports[p] = await auditPage(p, srv.url(pageFile(p)), { chrome });
});
test.after(async () => { if (chrome) await chrome.close(); if (srv) await srv.close(); });

const rule = (r, id) => r.violations.find((v) => v.id === id);

test('bad page: axe identifies the seeded unlabeled control', () => {
  const label = rule(reports.bad, 'label');
  assert.ok(label, 'label violation expected');
  assert.deepEqual(label.nodes.map((n) => n.target), [['#seeded-unlabeled']]);
});

test('bad page: axe identifies the seeded low-contrast text', () => {
  const cc = rule(reports.bad, 'color-contrast');
  assert.ok(cc);
  assert.ok(cc.nodes.some((n) => n.target.includes('#seeded-low-contrast')));
});

test('bad page: axe incomplete results are kept as needs-manual-review', () => {
  const nm = reports.bad.needsManualReview.find((v) => v.id === 'color-contrast');
  assert.ok(nm, 'gradient contrast should be incomplete, not dropped');
  assert.ok(nm.nodes.some((n) => n.target.includes('#seeded-gradient')));
  assert.ok(nm.nodes[0].reasons.some((m) => /gradient/i.test(m)));
  assert.equal(reports.bad.raw.incomplete.length, reports.bad.needsManualReview.length);
});

test('clean page: no label violation and no violations at all', () => {
  assert.equal(rule(reports.clean, 'label'), undefined);
  assert.deepEqual(reports.clean.violations, []);
  assert.ok(reports.clean.passCount > 0);
});

test('axe runs made no non-loopback requests', () => {
  assert.deepEqual(reports.bad.nonLoopbackRequests, []);
  assert.deepEqual(reports.clean.nonLoopbackRequests, []);
});

test('run-axe CLI: exit 2 on seeded page, 0 on clean, reports written', () => {
  const out = mkTmp('axe-out');
  const home = isolatedHome();
  try {
    const bad = runScript('run-axe.mjs', ['--page', 'bad', '--out', out], { HOME: home });
    assert.equal(bad.code, 2, bad.stderr);
    assert.match(bad.stdout, /needs-manual-review \[color-contrast\]/);
    const clean = runScript('run-axe.mjs', ['--page', 'clean', '--out', out], { HOME: home });
    assert.equal(clean.code, 0, clean.stderr);
    const dirs = runDirs(out);
    assert.equal(dirs.length, 2, 'each run writes into its own new run directory');
    const rep = JSON.parse(fs.readFileSync(path.join(dirs.find((d) => fs.existsSync(path.join(d, 'axe-bad.json'))), 'axe-bad.json'), 'utf8'));
    assert.ok(rep.violations.some((v) => v.id === 'label'));
  } finally { for (const d of [out, home]) fs.rmSync(d, { recursive: true, force: true }); }
});
