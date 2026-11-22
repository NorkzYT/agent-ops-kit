// Launch the system Chrome headless with background networking off and all
// non-loopback traffic sent to a dead proxy, then talk CDP over Node's
// built-in WebSocket (no puppeteer dependency).
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { minimalEnv, shortTmpDir } from './env.mjs';
import { CHROME, mkTmp } from './paths.mjs';

// Shared with lighthouserc.json (space-free so LHCI can pass them through).
export const QUIET_FLAGS = [
  '--headless=new',
  '--disable-background-networking',
  '--disable-component-update',
  '--disable-default-apps',
  '--disable-sync',
  '--disable-extensions',
  '--disable-domain-reliability',
  '--disable-client-side-phishing-detection',
  '--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication,CertificateTransparencyComponentUpdater',
  '--metrics-recording-only',
  '--no-first-run',
  '--no-default-browser-check',
  '--no-pings',
  '--password-store=basic',
  '--use-mock-keychain',
  // Loopback bypasses the proxy implicitly; everything else hits a closed port.
  '--proxy-server=http://127.0.0.1:9',
];

// Chrome gets the minimal env with its throwaway profile dir as HOME (see
// lib/env.mjs; the TMPDIR there is kept short for Chrome's sockets).
export function chromeEnv(home) {
  return minimalEnv(home, { TMPDIR: shortTmpDir() });
}

export async function launchChrome({ timeoutMs = 45000 } = {}) {
  const profile = mkTmp('chrome');
  const args = [...QUIET_FLAGS, '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', 'about:blank'];
  const proc = spawn(CHROME, args, { stdio: ['ignore', 'pipe', 'pipe'], env: chromeEnv(profile) });
  proc.stdout.resume();
  let stderr = '';
  proc.stderr.on('data', (d) => { stderr += d; if (stderr.length > 65536) stderr = stderr.slice(-65536); });
  const portFile = path.join(profile, 'DevToolsActivePort');
  const start = Date.now();
  let port;
  while (!port) {
    if (proc.exitCode !== null) throw new Error(`chrome exited ${proc.exitCode}: ${stderr.slice(-2000)}`);
    if (Date.now() - start > timeoutMs) { proc.kill('SIGKILL'); fs.rmSync(profile, { recursive: true, force: true }); throw new Error(`chrome did not open a DevTools port: ${stderr.slice(-2000)}`); }
    try { port = Number(fs.readFileSync(portFile, 'utf8').split('\n')[0]); } catch { /* not yet */ }
    if (!port) await new Promise((r) => setTimeout(r, 100));
  }
  const close = async () => {
    if (proc.exitCode === null) {
      proc.kill('SIGTERM');
      await new Promise((r) => { const t = setTimeout(() => { proc.kill('SIGKILL'); r(); }, 3000); proc.once('exit', () => { clearTimeout(t); r(); }); });
    }
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  };
  return { port, proc, close, args };
}

export async function pageTarget(port) {
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('no page target');
  return page;
}

export function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const listeners = new Set();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`)); else resolve(msg.result);
    } else if (msg.method) {
      for (const l of listeners) l(msg);
    }
  });
  const opened = new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error(`CDP websocket failed: ${wsUrl}`)), { once: true });
  });
  return opened.then(() => ({
    send(method, params = {}) {
      const msgId = ++id;
      ws.send(JSON.stringify({ id: msgId, method, params }));
      return new Promise((resolve, reject) => pending.set(msgId, { resolve, reject }));
    },
    on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    waitFor(method, timeoutMs = 15000) {
      return new Promise((resolve, reject) => {
        const t = setTimeout(() => { off(); reject(new Error(`timeout waiting for ${method}`)); }, timeoutMs);
        const off = this.on((m) => { if (m.method === method) { clearTimeout(t); off(); resolve(m.params); } });
      });
    },
    close() { ws.close(); },
  }));
}
