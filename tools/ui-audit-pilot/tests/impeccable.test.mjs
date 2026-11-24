import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { classify, engine, engineEnv, fileScan, renderedScan } from '../scripts/run-impeccable.mjs';
import { launchChrome } from '../scripts/lib/chrome.mjs';
import { startServer } from '../scripts/lib/server.mjs';
import { mkTmp } from '../scripts/lib/paths.mjs';
import { isolatedHome, runScript } from './helpers.mjs';

let eng; let env; let home;
test.before(() => {
  eng = engine();
  ({ env, home } = engineEnv());
});
test.after(() => fs.rmSync(home, { recursive: true, force: true }));

test('exit mapping keeps findings and scan errors distinct', () => {
  assert.equal(classify(0), 'clean');
  assert.equal(classify(2), 'findings');
  assert.equal(classify(1), 'scan-error');
  assert.equal(classify(139), 'unexpected-exit');
});

test('pinned engine binary matches the shipped checksum manifest', () => {
  assert.match(eng.sha256, /^[0-9a-f]{64}$/);
  assert.equal(eng.version, '0.1.27');
});

test('file scan: seeded low contrast is a finding, clean page is clean', async () => {
  const bad = await fileScan(['fixtures/queue-bad.html'], { env, bin: eng.bin });
  assert.equal(bad.outcome, 'findings', bad.stderr);
  assert.ok(bad.findings.some((f) => f.ruleId === 'low-contrast'));
  assert.equal(bad.sarif.runs[0].invocations[0].executionSuccessful, true);
  const clean = await fileScan(['fixtures/queue-clean.html'], { env, bin: eng.bin });
  assert.equal(clean.outcome, 'clean', clean.stderr);
  assert.deepEqual(clean.findings, []);
});

test('file scan: a missing target is a scan error, not a clean result', async () => {
  const r = await fileScan(['fixtures/does-not-exist.html'], { env, bin: eng.bin });
  assert.equal(r.code, 1);
  assert.equal(r.outcome, 'scan-error');
});

test('rendered scan over CDP on 127.0.0.1 finds the seeded low contrast', async () => {
  const srv = await startServer();
  const chrome = await launchChrome();
  try {
    const bad = await renderedScan('bad', { chrome, srv, env, bin: eng.bin });
    assert.equal(bad.outcome, 'findings', bad.stderr);
    assert.ok(bad.findings.some((f) => f.ruleId === 'low-contrast' && f.selector === '#seeded-low-contrast'));
    const clean = await renderedScan('clean', { chrome, srv, env, bin: eng.bin });
    assert.equal(clean.outcome, 'clean', clean.stderr);
  } finally { await chrome.close(); await srv.close(); }
});

test('run-impeccable CLI: findings exit 0 (advisory), scan error exit 1 (distinct)', () => {
  const out = mkTmp('impeccable-out');
  const cliHome = isolatedHome();
  test.after(() => { for (const d of [out, cliHome]) fs.rmSync(d, { recursive: true, force: true }); });
  const ok = runScript('run-impeccable.mjs', ['--no-render', '--file', 'fixtures/queue-bad.html', '--out', out], { HOME: cliHome });
  assert.equal(ok.code, 0, ok.stderr);
  assert.match(ok.stdout, /ADVISORY/);
  assert.match(ok.stdout, /findings, \d+ finding\(s\) \[low-contrast\]/);
  const err = runScript('run-impeccable.mjs', ['--no-render', '--file', 'fixtures/does-not-exist.html', '--out', out], { HOME: cliHome });
  assert.equal(err.code, 1);
  assert.match(err.stderr, /SCAN ERROR/);
  assert.doesNotMatch(err.stdout, /advisory and do not fail/);
});
