// Report output safety: every run writes into a new run directory; nothing that
// already exists is deleted or overwritten. All fixtures are throwaway temp
// trees with inert canary files; no real user directory is touched.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { lhciPage } from '../scripts/run-lhci.mjs';
import { SUMMARY_FILE, renderedFile } from '../scripts/run-impeccable.mjs';
import { classifyRequests } from '../scripts/run-axe.mjs';
import { mkTmp } from '../scripts/lib/paths.mjs';
import { OutputRefused, createRunDir, resolveOutBase, writeNew } from '../scripts/lib/output.mjs';
import { pageName, projectOutDir, projectTarget, ProjectRefused } from '../scripts/lib/project.mjs';
import { runScript } from './helpers.mjs';

const CANARY = 'canary: must survive';

function fixture() {
  const base = mkTmp('out-fixture');
  const home = path.join(base, 'home', 'user');
  const proj = path.join(home, 'work', 'site');
  fs.mkdirSync(path.join(home, '.config', 'app'), { recursive: true });
  fs.mkdirSync(proj, { recursive: true });
  fs.writeFileSync(path.join(proj, 'index.html'), '<!doctype html><title>x</title>');
  fs.writeFileSync(path.join(home, 'canary.txt'), CANARY);
  fs.writeFileSync(path.join(home, '.config', 'app', 'canary.txt'), CANARY);
  return { base, home, proj: fs.realpathSync(proj), cleanup: () => fs.rmSync(base, { recursive: true, force: true }) };
}

// Inventory of every file below dir with its content, to prove nothing changed.
function snapshot(dir) {
  const out = {};
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isSymbolicLink()) out[path.relative(dir, p)] = `-> ${fs.readlinkSync(p)}`;
      else if (e.isDirectory()) { out[path.relative(dir, p) + '/'] = 'dir'; walk(p); }
      else out[path.relative(dir, p)] = fs.readFileSync(p, 'utf8');
    }
  };
  walk(dir);
  return out;
}

test('--out bounds: root, $HOME and ancestors, hidden-under-home, system, symlink, non-dir and project overlap are refused', () => {
  const f = fixture();
  try {
    const before = snapshot(f.base);
    fs.symlinkSync(path.join(f.home, '.config'), path.join(f.base, 'cfg-link'));
    fs.symlinkSync(path.join(f.base, 'elsewhere'), path.join(f.base, 'dangling-link'));
    fs.writeFileSync(path.join(f.base, 'a-file'), CANARY);
    const refused = ['/', f.home, path.dirname(f.home), f.base, '/etc/ui', '/usr/share/ui', '/proc/self/x',
      path.join(f.home, '.config'), path.join(f.home, '.config', 'reports'), path.join(f.base, '.hidden-out'),
      path.join(f.base, 'cfg-link'), path.join(f.base, 'cfg-link', 'reports'), path.join(f.base, 'dangling-link'),
      path.join(f.base, 'a-file'),
      f.proj, path.join(f.proj, 'reports'), path.dirname(f.proj)];
    for (const d of refused) assert.throws(() => resolveOutBase(d, { home: f.home, root: f.proj }), OutputRefused, d);
    // Fine: a new or existing ordinary directory outside the project.
    assert.equal(resolveOutBase(path.join(f.home, 'reports', 'ui'), { home: f.home, root: f.proj }), path.join(f.home, 'reports', 'ui'));
    assert.equal(resolveOutBase(path.join(f.base, 'out'), { home: f.home }), path.join(f.base, 'out'));
    fs.rmSync(path.join(f.base, 'cfg-link')); fs.rmSync(path.join(f.base, 'dangling-link')); fs.rmSync(path.join(f.base, 'a-file'));
    assert.deepEqual(snapshot(f.base), before, 'validation never creates, deletes or edits anything');
  } finally { f.cleanup(); }
});

test('run directories are new per run and pre-existing data in --out is preserved', () => {
  const f = fixture();
  try {
    const out = path.join(f.base, 'out');
    fs.mkdirSync(path.join(out, 'lhci', 'index'), { recursive: true });
    fs.writeFileSync(path.join(out, 'lhci', 'index', 'canary.txt'), CANARY);
    fs.writeFileSync(path.join(out, 'axe-index.json'), CANARY);
    const before = snapshot(out);
    const a = createRunDir(out);
    const b = createRunDir(out);
    assert.notEqual(a, b);
    assert.equal(path.dirname(a), out);
    assert.deepEqual(fs.readdirSync(a), []);
    writeNew(path.join(a, 'axe-index.json'), '{}');
    assert.throws(() => writeNew(path.join(a, 'axe-index.json'), 'clobber'), OutputRefused);
    assert.equal(fs.readFileSync(path.join(a, 'axe-index.json'), 'utf8'), '{}');
    // A shared explicit run id (one ui-audit-project run) reuses the dir; files stay exclusive.
    assert.equal(createRunDir(out, path.basename(a)), a);
    for (const id of ['', '..', '.hidden', 'a/b', 'a b', 'x'.repeat(81)]) assert.throws(() => createRunDir(out, id), OutputRefused, id);
    const after = snapshot(out);
    for (const [k, v] of Object.entries(before)) assert.equal(after[k], v, k);
    // A run id that is a symlink is refused, not followed.
    fs.symlinkSync(path.join(f.home, '.config'), path.join(out, 'linked'));
    assert.throws(() => createRunDir(out, 'linked'), OutputRefused);
    assert.equal(fs.readFileSync(path.join(f.home, '.config', 'app', 'canary.txt'), 'utf8'), CANARY);
  } finally { f.cleanup(); }
});

test('lhciPage refuses an existing page dir instead of deleting it (no Chrome needed)', async () => {
  const f = fixture();
  try {
    const outRoot = path.join(f.base, 'run', 'lhci');
    fs.mkdirSync(path.join(outRoot, 'clean', '.lighthouseci'), { recursive: true });
    fs.writeFileSync(path.join(outRoot, 'clean', 'canary.txt'), CANARY);
    fs.writeFileSync(path.join(outRoot, 'clean', '.lighthouseci', 'lhr-1.json'), CANARY);
    const before = snapshot(outRoot);
    await assert.rejects(lhciPage('clean', 'http://127.0.0.1:9/queue-clean.html', { outRoot }), OutputRefused);
    await assert.rejects(lhciPage('clean', 'http://127.0.0.1:9/queue-clean.html'), OutputRefused, 'no implicit default dir');
    await assert.rejects(lhciPage('../escape', 'http://127.0.0.1:9/queue-clean.html', { outRoot }), OutputRefused);
    assert.deepEqual(snapshot(outRoot), before);
  } finally { f.cleanup(); }
});

test('two projects with the same basename get different default report dirs', () => {
  const f = fixture();
  try {
    const other = path.join(f.base, 'elsewhere', 'site');
    fs.mkdirSync(other, { recursive: true });
    const a = projectOutDir(f.proj, undefined, { home: f.home });
    const b = projectOutDir(fs.realpathSync(other), undefined, { home: f.home });
    assert.notEqual(a, b);
    assert.match(path.basename(a), /^project-site-[0-9a-f]{10}$/);
  } finally { f.cleanup(); }
});

test('report names: a page called "summary" cannot replace the Impeccable summary; colliding pages refused', () => {
  assert.notEqual(renderedFile(pageName('summary.html')), SUMMARY_FILE);
  assert.notEqual(renderedFile(pageName('impeccable-summary.html')), SUMMARY_FILE);
  const srv = { origin: 'http://127.0.0.1:1' };
  assert.throws(() => projectTarget(srv, ['docs/a.html', 'docs-a.html']), ProjectRefused);
});

test('request classification reports other 127.0.0.1 ports separately (they are not blocked)', () => {
  const r = classifyRequests(['http://127.0.0.1:4000/index.html', 'http://127.0.0.1:4000/a.css', 'http://127.0.0.1:5432/x',
    'http://localhost:8080/', 'https://example.com/a.js', 'data:image/png;base64,AA=='], 'http://127.0.0.1:4000/index.html');
  assert.deepEqual(r.otherLoopback, ['http://127.0.0.1:5432/x']);
  assert.deepEqual(r.nonLoopback, ['http://localhost:8080/', 'https://example.com/a.js']);
});

for (const script of ['run-axe.mjs', 'run-lhci.mjs', 'run-impeccable.mjs']) {
  test(`${script} refuses an unsafe --out before a browser starts and leaves canaries intact`, () => {
    const f = fixture();
    try {
      const before = snapshot(f.base);
      const cases = [['--out', f.home], ['--out', path.join(f.home, '.config')], ['--out', '/'],
        ['--project', f.proj, '--out', path.join(f.proj, 'reports')], ['--project', f.proj, '--out', path.dirname(f.proj)],
        ['--project', f.proj, '--run-id', '../x']];
      for (const args of cases) {
        const r = runScript(script, args, { HOME: f.home });
        assert.equal(r.code, 1, `${args.join(' ')}: ${r.stdout}${r.stderr}`);
        assert.match(r.stderr, /refused (output|project)/, args.join(' '));
      }
      assert.deepEqual(snapshot(f.base), before);
    } finally { f.cleanup(); }
  });
}

// Regression: two --page-path values that normalize to one report name
// (docs/a.html, docs-a.html -> "docs-a") used to be refused only after the
// server started, outside its try/finally, so the CLI hung on the open
// listener. Bounded: a hang is a SIGKILL here, not a stuck test run.
for (const script of ['run-axe.mjs', 'run-lhci.mjs', 'run-impeccable.mjs']) {
  test(`${script} refuses colliding project page names before any output or server, and exits`, () => {
    const f = fixture();
    try {
      fs.mkdirSync(path.join(f.proj, 'docs'));
      fs.writeFileSync(path.join(f.proj, 'docs', 'a.html'), '<!doctype html><title>a</title>');
      fs.writeFileSync(path.join(f.proj, 'docs-a.html'), '<!doctype html><title>b</title>');
      const out = path.join(f.base, 'out');
      const before = snapshot(f.base);
      const r = runScript(script, ['--project', f.proj, '--page-path', 'docs/a.html', '--page-path', 'docs-a.html', '--out', out], { HOME: f.home }, { timeoutMs: 20000 });
      assert.equal(r.signal, null, `${script} did not exit (killed after ${r.ms}ms): ${r.stderr}`);
      assert.equal(r.code, 1, r.stdout + r.stderr);
      assert.match(r.stderr, /refused project: .*same report name/);
      assert.ok(!fs.existsSync(out), 'no run directory created');
      assert.deepEqual(snapshot(f.base), before);
    } finally { f.cleanup(); }
  });
}

// Regression: the operator's TMPDIR is ~/.hermes/cache/scratch. A temp --out
// there must stay refused under the real HOME (guard not weakened), and the CLI
// tests' isolated HOME is what lets the same directory through.
test('--out under a hidden-HOME scratch TMPDIR: refused for that HOME, accepted with an isolated HOME', () => {
  const f = fixture();
  try {
    const scratch = path.join(f.home, '.hermes', 'cache', 'scratch');
    fs.mkdirSync(scratch, { recursive: true });
    const out = fs.mkdtempSync(path.join(scratch, 'ui-audit-axe-out-'));
    assert.throws(() => resolveOutBase(out, { home: f.home }), (e) => e instanceof OutputRefused && /hidden directory under \$HOME/.test(e.message));
    const r = runScript('run-axe.mjs', ['--page', 'clean', '--out', out], { HOME: f.home });
    assert.equal(r.code, 1, r.stdout + r.stderr);
    assert.match(r.stderr, /refused output: .*hidden directory under \$HOME/);
    assert.deepEqual(fs.readdirSync(out), [], 'refused before any run directory is created');
    const isolated = path.join(f.base, 'isolated-home');
    fs.mkdirSync(isolated);
    assert.equal(resolveOutBase(out, { home: isolated }), out);
  } finally { f.cleanup(); }
});
