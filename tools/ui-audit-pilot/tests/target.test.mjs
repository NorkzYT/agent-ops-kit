import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { FIXTURES } from '../scripts/lib/paths.mjs';
import { startServer } from '../scripts/lib/server.mjs';
import { TargetRefused, assertLoopbackTarget } from '../scripts/lib/target.mjs';
import { runScript } from './helpers.mjs';

const REFUSED = [
  'https://example.com/',
  'http://example.com/',
  'http://localhost:8080/queue-bad.html',
  'http://127.0.0.1.nip.io:8080/',
  'http://10.0.0.5:8080/',
  'http://0.0.0.0:8080/',
  'http://[::1]:8080/',
  'http://user:pw@127.0.0.1:8080/',
  'http://127.0.0.1/',
  'https://127.0.0.1:8443/',
  'file:///etc/passwd',
  'not a url',
  // Loopback, but not an owned fixture page: other local services are out of scope.
  'http://127.0.0.1:8317/v1/models',
  'http://127.0.0.1:4173/',
  'http://127.0.0.1:4173/package.json',
  'http://127.0.0.1:4173/queue-clean.html?x=1',
  'http://127.0.0.1:4173/queue-clean.html#x',
  'http://127.0.0.1:4173/sub/queue-clean.html',
];

test('non-loopback and malformed targets are refused', () => {
  for (const u of REFUSED) assert.throws(() => assertLoopbackTarget(u), TargetRefused, u);
  assert.equal(assertLoopbackTarget('http://127.0.0.1:4173/queue-clean.html'), 'http://127.0.0.1:4173/queue-clean.html');
});

for (const script of ['run-axe.mjs', 'run-lhci.mjs']) {
  test(`${script} refuses a non-loopback --url before starting a browser`, () => {
    for (const u of ['https://example.com/', 'http://localhost:9/']) {
      const r = runScript(script, ['--url', u]);
      assert.equal(r.code, 1, r.stderr);
      assert.match(r.stderr, /refused target/);
      assert.doesNotMatch(r.stdout, /violation|pass \(|assertion/);
    }
  });
}

test('run-impeccable refuses file targets outside fixtures/', () => {
  const r = runScript('run-impeccable.mjs', ['--no-render', '--file', '/etc/passwd']);
  assert.equal(r.code, 1);
  assert.match(r.stderr, /refused file target outside fixtures/);
});

test('fixture server binds 127.0.0.1 only and blocks traversal', async () => {
  const srv = await startServer();
  try {
    assert.equal(srv.server.address().address, '127.0.0.1');
    assert.equal((await fetch(srv.url('queue-clean.html'))).status, 200);
    assert.equal((await fetch(`${srv.origin}/..%2fpackage.json`)).status, 404);
    assert.equal((await fetch(`${srv.origin}/%2e%2e/%2e%2e/etc/passwd`)).status, 404);
    assert.equal((await fetch(srv.url('queue-clean.html'), { method: 'POST' })).status, 405);
  } finally { await srv.close(); }
});

test('fixtures reference no external resources', () => {
  for (const f of fs.readdirSync(FIXTURES)) {
    const body = fs.readFileSync(path.join(FIXTURES, f), 'utf8');
    assert.doesNotMatch(body, /(src|href|url)\s*[=(]\s*["']?(https?:)?\/\//i, f);
  }
});
