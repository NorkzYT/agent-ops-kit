import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { mkTmp, ROOT } from '../scripts/lib/paths.mjs';

export function runScript(script, args = [], env = {}, { timeoutMs = 240000 } = {}) {
  const t = Date.now();
  const r = spawnSync(process.execPath, [path.join(ROOT, 'scripts', script), ...args], { cwd: ROOT, encoding: 'utf8', timeout: timeoutMs, killSignal: 'SIGKILL', env: { ...process.env, ...env } });
  return { code: r.status, signal: r.signal, stdout: r.stdout, stderr: r.stderr, ms: Date.now() - t };
}

// Run directories a runner created under `out` (one per run, lib/output.mjs).
export function runDirs(out) {
  return fs.readdirSync(out, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => path.join(out, e.name)).sort();
}

// A throwaway HOME for CLI runs that pass a temp --out. The operator's TMPDIR
// may sit in a hidden directory under the real $HOME (~/.hermes/cache/scratch),
// which the --out guard rightly refuses; the fixture isolates HOME instead of
// relaxing the guard. The caller removes it.
export function isolatedHome() {
  return mkTmp('cli-home');
}
