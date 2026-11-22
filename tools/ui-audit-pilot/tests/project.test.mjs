// User-supplied project target: a local static directory the pilot serves
// itself on 127.0.0.1. Guards are checked before any server or browser starts.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import test from 'node:test';
import { auditPage } from '../scripts/run-axe.mjs';
import { launchChrome } from '../scripts/lib/chrome.mjs';
import { CHROME, FIXTURES, mkTmp } from '../scripts/lib/paths.mjs';
import { ProjectRefused, projectTarget, resolveProjectPages, resolveProjectRoot } from '../scripts/lib/project.mjs';
import { startServer } from '../scripts/lib/server.mjs';
import { TargetRefused, assertLoopbackTarget } from '../scripts/lib/target.mjs';
import { runDirs, runScript } from './helpers.mjs';

const PAGE = (body, extra = '') => `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Demo</title><meta name="description" content="Synthetic demo page">${extra}</head><body><main><h1>Demo</h1>${body}</main></body></html>`;

// A synthetic user project inside a fake HOME, with secrets next to it.
function makeHome() {
  const home = mkTmp('project-home');
  const proj = path.join(home, 'work', 'demo-site');
  fs.mkdirSync(path.join(proj, 'docs'), { recursive: true });
  fs.mkdirSync(path.join(proj, '.git'), { recursive: true });
  fs.writeFileSync(path.join(proj, 'index.html'), PAGE('<label for="q">Search</label><input id="q">'));
  fs.writeFileSync(path.join(proj, 'docs', 'bad page.html'), PAGE('<input id="unlabeled">'));
  fs.writeFileSync(path.join(proj, 'style.css'), 'body{font-family:sans-serif}');
  fs.writeFileSync(path.join(proj, '.env'), 'TOKEN=secret');
  fs.writeFileSync(path.join(proj, '.git', 'config'), '[core]');
  fs.writeFileSync(path.join(proj, 'data.sqlite'), 'secret rows');
  fs.mkdirSync(path.join(home, '.ssh'));
  fs.writeFileSync(path.join(home, '.ssh', 'id_ed25519'), 'PRIVATE');
  fs.symlinkSync(path.join(home, '.ssh'), path.join(proj, 'keys'));
  fs.symlinkSync(path.join(home, '.ssh', 'id_ed25519'), path.join(proj, 'leak.html'));
  return { home, proj };
}

test('project root guard refuses broad, hidden and system directories', () => {
  const { home, proj } = makeHome();
  try {
    assert.equal(resolveProjectRoot(proj, { home }), fs.realpathSync(proj));
    const refused = ['/', home, path.dirname(home), path.join(home, '.ssh'), path.join(proj, '.git'), path.join(home, 'missing'),
      path.join(proj, 'index.html'), '/etc', '/proc/self', '/sys', '/dev', 'relative/dir', ''];
    for (const d of refused) assert.throws(() => resolveProjectRoot(d, { home }), ProjectRefused, d);
    // A symlink that resolves into a hidden HOME dir is judged by its target.
    fs.symlinkSync(path.join(home, '.ssh'), path.join(home, 'work', 'innocent'));
    assert.throws(() => resolveProjectRoot(path.join(home, 'work', 'innocent'), { home }), ProjectRefused);
  } finally { fs.rmSync(home, { recursive: true, force: true }); }
});

test('project pages must be existing .html files inside the root, no hidden or escaping paths', () => {
  const { home, proj } = makeHome();
  try {
    const root = resolveProjectRoot(proj, { home });
    assert.deepEqual(resolveProjectPages(root, []), ['index.html']);
    assert.deepEqual(resolveProjectPages(root, ['docs/bad page.html', 'index.html']), ['docs/bad page.html', 'index.html']);
    for (const p of ['../outside.html', '/etc/passwd', '.env', '.git/config', 'style.css', 'missing.html', 'leak.html', 'keys/id_ed25519',
      'docs/../../x.html', 'index.html?x=1', 'index.html#a', 'docs\\bad page.html']) {
      assert.throws(() => resolveProjectPages(root, [p]), ProjectRefused, p);
    }
  } finally { fs.rmSync(home, { recursive: true, force: true }); }
});

test('project server serves only allowed public file types and never hidden files or symlink escapes', async () => {
  const { home, proj } = makeHome();
  const srv = await startServer(resolveProjectRoot(proj, { home }));
  try {
    assert.equal(srv.server.address().address, '127.0.0.1');
    assert.equal((await fetch(srv.url('index.html'))).status, 200);
    assert.equal((await fetch(srv.url('style.css'))).status, 200);
    assert.equal((await fetch(`${srv.origin}/docs/bad%20page.html`)).status, 200);
    for (const p of ['.env', '.git/config', '%2eenv', 'data.sqlite', 'keys/id_ed25519', 'leak.html', '..%2f..%2f.ssh%2fid_ed25519', 'docs/']) {
      const r = await fetch(`${srv.origin}/${p}`);
      assert.equal(r.status, 404, p);
      assert.doesNotMatch(await r.text(), /secret|PRIVATE|core/, p);
    }
  } finally { await srv.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test('target guard accepts only the selected project pages on the pilot server origin', () => {
  const allowed = new Set(['/index.html', '/docs/bad%20page.html']);
  assert.equal(assertLoopbackTarget('http://127.0.0.1:4173/index.html', allowed), 'http://127.0.0.1:4173/index.html');
  for (const u of ['http://127.0.0.1:4173/queue-bad.html', 'http://127.0.0.1:4173/style.css', 'http://localhost:4173/index.html', 'http://127.0.0.1:4173/index.html?a']) {
    assert.throws(() => assertLoopbackTarget(u, allowed), TargetRefused, u);
  }
  // Without a project scope the fixture catalogue still applies.
  assert.throws(() => assertLoopbackTarget('http://127.0.0.1:4173/index.html'), TargetRefused);
});

test('projectTarget builds the allowed path set from the running server', async () => {
  const { home, proj } = makeHome();
  const root = resolveProjectRoot(proj, { home });
  const srv = await startServer(root);
  try {
    const t = projectTarget(srv, resolveProjectPages(root, ['docs/bad page.html']));
    assert.deepEqual([...t.allowed], ['/docs/bad%20page.html']);
    assert.equal(t.pages[0].name, 'docs-bad-page');
    assert.equal(assertLoopbackTarget(t.pages[0].url, t.allowed), `${srv.origin}/docs/bad%20page.html`);
  } finally { await srv.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

for (const script of ['run-axe.mjs', 'run-lhci.mjs', 'run-impeccable.mjs']) {
  test(`${script} --project refuses broad/relative dirs and mixed fixture flags, before a browser starts`, () => {
    for (const args of [['--project', '/'], ['--project', '/etc'], ['--project', process.env.HOME || '/root'],
      ['--project', 'tests'], ['--project', FIXTURES, '--page', 'bad'], ['--page-path', 'index.html']]) {
      const r = runScript(script, args);
      assert.equal(r.code, 1, `${args.join(' ')}: ${r.stderr}`);
      assert.match(r.stderr, /refused project/, args.join(' '));
      assert.doesNotMatch(r.stdout, /violation|pass \(|finding/);
    }
  });
}

test('run-axe --project audits a real user page with real Chrome and keeps reports outside the project', () => {
  const { home, proj } = makeHome();
  const out = mkTmp('project-out');
  try {
    const r = runScript('run-axe.mjs', ['--project', proj, '--page-path', 'docs/bad page.html', '--page-path', 'index.html', '--out', out], { HOME: home });
    assert.equal(r.code, 2, r.stderr);
    const [run] = runDirs(out);
    const bad = JSON.parse(fs.readFileSync(path.join(run, 'axe-docs-bad-page.json'), 'utf8'));
    const good = JSON.parse(fs.readFileSync(path.join(run, 'axe-index.json'), 'utf8'));
    assert.ok(bad.violations.some((v) => v.id === 'label'));
    assert.equal(good.violations.length, 0, JSON.stringify(good.violations));
    assert.deepEqual(bad.nonLoopbackRequests, []);
    assert.equal(bad.project, fs.realpathSync(proj));
    assert.ok(!fs.readdirSync(proj).some((f) => f.startsWith('axe-')), 'nothing written into the project');
    assert.throws(() => runScriptOut(proj), /refused/);
  } finally { fs.rmSync(home, { recursive: true, force: true }); fs.rmSync(out, { recursive: true, force: true }); }
});

// --out inside the project would put reports in the served tree; refused.
function runScriptOut(proj) {
  const r = runScript('run-axe.mjs', ['--project', proj, '--out', path.join(proj, 'reports')], { HOME: path.dirname(path.dirname(proj)) });
  if (r.code === 1 && /refused/.test(r.stderr)) throw new Error('refused');
  return r;
}

test('a project page that calls out to the network is reported, not silently audited clean', async () => {
  const { home, proj } = makeHome();
  fs.writeFileSync(path.join(proj, 'beacon.html'), PAGE('<img alt="x" src="http://198.51.100.7/pixel.png">'));
  const root = resolveProjectRoot(proj, { home });
  const srv = await startServer(root);
  const chrome = await launchChrome();
  try {
    const t = projectTarget(srv, resolveProjectPages(root, ['beacon.html']));
    const rep = await auditPage(t.pages[0].name, t.pages[0].url, { chrome, allowed: t.allowed });
    assert.ok(rep.nonLoopbackRequests.some((u) => u.includes('198.51.100.7')), JSON.stringify(rep.nonLoopbackRequests));
  } finally { await chrome.close(); await srv.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

// Only the navigation target is restricted. A selected page CAN load
// subresources from another service on 127.0.0.1 (any port): the inert canary
// server below is reached. The runner reports it; it does not block it.
test('subresources on other 127.0.0.1 ports are not blocked, only reported', async () => {
  const { home, proj } = makeHome();
  const hits = [];
  const canary = http.createServer((req, res) => { hits.push(req.url); res.writeHead(204).end(); });
  await new Promise((r) => canary.listen(0, '127.0.0.1', r));
  const port = canary.address().port;
  fs.writeFileSync(path.join(proj, 'local.html'), PAGE(`<img alt="x" src="http://127.0.0.1:${port}/canary.png">`));
  const root = resolveProjectRoot(proj, { home });
  const srv = await startServer(root);
  const chrome = await launchChrome();
  try {
    const t = projectTarget(srv, resolveProjectPages(root, ['local.html']));
    const rep = await auditPage(t.pages[0].name, t.pages[0].url, { chrome, allowed: t.allowed });
    assert.deepEqual(hits, ['/canary.png'], 'the other local service was reached');
    assert.deepEqual(rep.otherLoopbackRequests, [`http://127.0.0.1:${port}/canary.png`]);
    assert.deepEqual(rep.nonLoopbackRequests, []);
  } finally { await chrome.close(); await srv.close(); await new Promise((r) => canary.close(r)); fs.rmSync(home, { recursive: true, force: true }); }
});

test('run-lhci and run-impeccable --project audit real user pages; filesystem-only reports outside the project', () => {
  const { home, proj } = makeHome();
  const out = mkTmp('project-out');
  // Pre-existing data in OUT, including an old lhci/index dir: must survive.
  fs.mkdirSync(path.join(out, 'lhci', 'index', 'upload'), { recursive: true });
  fs.writeFileSync(path.join(out, 'lhci', 'index', 'upload', 'canary.txt'), 'canary');
  fs.writeFileSync(path.join(out, 'impeccable-summary.json'), 'canary');
  try {
    const l = runScript('run-lhci.mjs', ['--project', proj, '--page-path', 'index.html', '--out', out, '--run-id', 'run-a'], { HOME: home });
    assert.equal(l.code, 0, l.stdout + l.stderr);
    const sum = JSON.parse(fs.readFileSync(path.join(out, 'run-a', 'lhci', 'index', 'pilot-summary.json'), 'utf8'));
    assert.equal(sum.status, 'pass');
    assert.ok(sum.steps.collect.command.includes(`--chromePath=${CHROME}`), 'UI_AUDIT_CHROME / default Chrome reaches LHCI');
    assert.equal(sum.project, fs.realpathSync(proj));
    assert.ok(fs.readdirSync(path.join(out, 'run-a', 'lhci', 'index', 'upload')).some((f) => f.endsWith('.html')));
    // The same run id again would reuse lhci/index: refused, first run kept.
    const again = runScript('run-lhci.mjs', ['--project', proj, '--page-path', 'index.html', '--out', out, '--run-id', 'run-a'], { HOME: home });
    assert.equal(again.code, 1, again.stdout + again.stderr);
    assert.match(again.stderr, /already exists/);
    assert.ok(fs.existsSync(path.join(out, 'run-a', 'lhci', 'index', 'pilot-summary.json')));
    const lb = runScript('run-lhci.mjs', ['--project', proj, '--page-path', 'docs/bad page.html', '--out', out], { HOME: home });
    assert.equal(lb.code, 2, lb.stdout + lb.stderr);
    assert.match(lb.stdout, /error:label/);

    const i = runScript('run-impeccable.mjs', ['--project', proj, '--page-path', 'index.html', '--page-path', 'docs/bad page.html', '--out', out, '--run-id', 'run-a'], { HOME: home });
    assert.equal(i.code, 0, i.stdout + i.stderr);
    assert.equal(fs.readFileSync(path.join(out, 'lhci', 'index', 'upload', 'canary.txt'), 'utf8'), 'canary');
    assert.equal(fs.readFileSync(path.join(out, 'impeccable-summary.json'), 'utf8'), 'canary');
    assert.ok(fs.existsSync(path.join(out, 'run-a', 'impeccable-rendered-docs-bad-page.json')));
    const isum = JSON.parse(fs.readFileSync(path.join(out, 'run-a', 'impeccable-summary.json'), 'utf8'));
    assert.equal(isum.project, fs.realpathSync(proj));
    assert.deepEqual(isum.scans.map((s) => s.kind), ['file', 'rendered', 'rendered']);
    assert.ok(isum.scans.every((s) => ['clean', 'findings'].includes(s.outcome)), JSON.stringify(isum.scans.map((s) => [s.outcome, s.stderr])));
    assert.deepEqual(fs.readdirSync(proj).sort(), ['.env', '.git', 'data.sqlite', 'docs', 'index.html', 'keys', 'leak.html', 'style.css']);
  } finally { fs.rmSync(home, { recursive: true, force: true }); fs.rmSync(out, { recursive: true, force: true }); }
});
