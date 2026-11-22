import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { RC, checkUploadConfig, lhciPage } from '../scripts/run-lhci.mjs';
import { mkTmp, pageFile, ROOT } from '../scripts/lib/paths.mjs';
import { startServer } from '../scripts/lib/server.mjs';

const rc = () => JSON.parse(fs.readFileSync(RC, 'utf8'));

test('upload config is filesystem-only', () => {
  const cfg = rc();
  assert.equal(cfg.ci.upload.target, 'filesystem');
  assert.ok(!path.isAbsolute(cfg.ci.upload.outputDir));
  assert.doesNotThrow(() => checkUploadConfig(cfg));
  const raw = fs.readFileSync(RC, 'utf8') + fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8');
  assert.doesNotMatch(raw, /temporary-public-storage|serverBaseUrl|"token"/);
  assert.match(fs.readFileSync(path.join(ROOT, 'scripts', 'run-lhci.mjs'), 'utf8'), /'--target=filesystem'/);
});

test('remote upload targets are refused', () => {
  const bad = [
    { ci: { upload: { target: 'temporary-public-storage' } } },
    { ci: { upload: { target: 'lhci', serverBaseUrl: 'https://lhci.example' } } },
    { ci: { upload: { target: 'filesystem', outputDir: './up', serverBaseUrl: 'https://x' } } },
    { ci: { upload: { target: 'filesystem', outputDir: '/tmp/up' } } },
    { ci: { upload: { target: 'filesystem', outputDir: '../up' } } },
    { ci: { upload: { target: 'filesystem', outputDir: './up' }, server: { port: 9001 } } },
    { ci: {} },
  ];
  for (const c of bad) assert.throws(() => checkUploadConfig(c), JSON.stringify(c));
});

test('lighthouse telemetry off and Chrome background networking disabled', () => {
  const s = rc().ci.collect.settings;
  assert.equal(s.enableErrorReporting, false);
  assert.match(s.chromeFlags, /--disable-background-networking/);
  assert.match(s.chromeFlags, /--proxy-server=http:\/\/127\.0\.0\.1:9/);
});

let srv; let out;
test.before(async () => { srv = await startServer(); out = mkTmp('lhci-out'); });
test.after(async () => { if (srv) await srv.close(); fs.rmSync(out, { recursive: true, force: true }); });

test('lhci assertions pass on the clean page and upload to the filesystem', async () => {
  const r = await lhciPage('clean', srv.url(pageFile('clean')), { outRoot: out });
  assert.equal(r.steps.collect.code, 0, r.steps.collect.stderr.slice(-2000));
  assert.equal(r.status, 'pass', JSON.stringify(r.failedAssertions));
  assert.equal(r.steps.assert.code, 0);
  assert.equal(r.steps.upload.code, 0);
  assert.ok(fs.existsSync(path.join(r.dir, 'upload', 'manifest.json')));
  assert.match(r.steps.upload.stdout, /Dumping 1 reports to disk/);
});

test('lhci assertion fails on the regression page (missing meta description)', async () => {
  const r = await lhciPage('regression', srv.url(pageFile('regression')), { outRoot: out });
  assert.equal(r.steps.collect.code, 0, r.steps.collect.stderr.slice(-2000));
  assert.equal(r.status, 'assertion-failed');
  assert.notEqual(r.steps.assert.code, 0);
  assert.deepEqual(r.failedAssertions.filter((a) => a.level === 'error').map((a) => a.auditId), ['meta-description']);
  assert.ok(fs.existsSync(path.join(r.dir, 'upload', 'manifest.json')), 'failed runs still keep local evidence');
});
