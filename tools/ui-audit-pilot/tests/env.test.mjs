import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { minimalEnv } from '../scripts/lib/env.mjs';
import { engineEnv } from '../scripts/run-impeccable.mjs';
import { lhciEnv } from '../scripts/run-lhci.mjs';
import { run } from '../scripts/lib/proc.mjs';
import { ROOT } from '../scripts/lib/paths.mjs';

// Hostile values a parent shell could carry. None may reach a tool.
const HOSTILE = {
  GITHUB_TOKEN: 'canary-gh', OPENAI_API_KEY: 'canary-oa', AWS_SECRET_ACCESS_KEY: 'canary-aws',
  LHCI_TOKEN: 'canary-lhci', LHCI_UPLOAD__TARGET: 'temporary-public-storage', LHCI_SERVER_BASE_URL: 'https://lhci.example',
  LHCI_UPLOAD__SERVER_BASE_URL: 'https://lhci.example', LHCI_ASSERT__PRESET: 'lighthouse:all',
  IMPECCABLE_API_URL: 'https://evil.example', IMPECCABLE_UPDATE_HOST: 'https://evil.example', IMPECCABLE_CREDENTIAL_STORE: '/tmp/x',
  IMPECCABLE_LIVE_COPY_AGENT: 'on', HTTPS_PROXY: 'http://evil.example:3128', HTTP_PROXY: 'http://evil.example:3128', NO_PROXY: '*',
  NODE_OPTIONS: '--require /tmp/evil.js', LD_PRELOAD: '/tmp/evil.so', CHROME_PATH: '/tmp/evil-chrome',
  HOME: '/tmp/evil-home', XDG_CONFIG_HOME: '/tmp/evil-xdg', XDG_CACHE_HOME: '/tmp/evil-cache', DO_NOT_TRACK: '0', IMPECCABLE_NO_TELEMETRY: '0',
};

function withHostile(fn) {
  const saved = {};
  for (const [k, v] of Object.entries(HOSTILE)) { saved[k] = process.env[k]; process.env[k] = v; }
  try { return fn(); } finally {
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  }
}

function assertClean(env, home) {
  const evil = Object.entries(env).filter(([, v]) => /canary|evil|lighthouse:all|public-storage/.test(String(v)));
  assert.deepEqual(evil, [], 'hostile value reached the child env');
  for (const k of Object.keys(env)) {
    assert.ok(!/^(LHCI_|IMPECCABLE_API|IMPECCABLE_UPDATE|IMPECCABLE_CREDENTIAL)/.test(k), k);
    assert.ok(!/TOKEN|SECRET|KEY|PROXY|NODE_OPTIONS|LD_/.test(k), k);
  }
  assert.equal(env.HOME, home);
  for (const k of ['XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME']) assert.ok(env[k].startsWith(home + path.sep), k);
  assert.equal(env.DO_NOT_TRACK, '1');
}

test('minimalEnv drops inherited secrets/overrides and uses a private HOME', () => {
  withHostile(() => {
    const env = minimalEnv('/x/home');
    assertClean(env, '/x/home');
    assert.equal(env.NO_UPDATE_NOTIFIER, '1');
    assert.match(env.PATH, /^\//);
  });
});

test('impeccable env: private home, telemetry/update/live-copy off, nothing inherited', () => {
  withHostile(() => {
    const { env, home } = engineEnv();
    try {
      assertClean(env, home);
      assert.equal(env.IMPECCABLE_HOME, path.join(home, 'impeccable'));
      assert.equal(env.IMPECCABLE_NO_TELEMETRY, '1');
      assert.equal(env.IMPECCABLE_NO_UPDATE_CHECK, '1');
      assert.equal(env.IMPECCABLE_NO_STALENESS_CHECK, '1');
      assert.equal(env.IMPECCABLE_LIVE_COPY_AGENT, 'off');
    } finally { fs.rmSync(home, { recursive: true, force: true }); }
  });
});

test('lhci env: no LHCI_* override, proxy or token inherited', () => {
  withHostile(() => {
    const { env, home } = lhciEnv();
    try {
      assertClean(env, home);
      assert.equal(env.CI, '1');
    } finally { fs.rmSync(home, { recursive: true, force: true }); }
  });
});

test('a child started with the tool env really sees none of the hostile values', async () => {
  const { env, home } = withHostile(() => lhciEnv());
  try {
    const r = await run(process.execPath, ['-e', 'process.stdout.write(JSON.stringify(process.env))'], { env, timeoutMs: 20000 });
    assert.equal(r.code, 0, r.stderr);
    assertClean(JSON.parse(r.stdout), home);
  } finally { fs.rmSync(home, { recursive: true, force: true }); }
});

test('no tool runner spreads process.env into a child', () => {
  for (const f of ['run-impeccable.mjs', 'run-lhci.mjs', 'run-axe.mjs', 'lib/chrome.mjs']) {
    assert.doesNotMatch(fs.readFileSync(path.join(ROOT, 'scripts', f), 'utf8'), /\.\.\.process\.env|env:\s*process\.env/, f);
  }
});
