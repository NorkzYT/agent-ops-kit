import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { ROOT } from '../scripts/lib/paths.mjs';

export function runScript(script, args = []) {
  const t = Date.now();
  const r = spawnSync(process.execPath, [path.join(ROOT, 'scripts', script), ...args], { cwd: ROOT, encoding: 'utf8', timeout: 240000 });
  return { code: r.status, stdout: r.stdout, stderr: r.stderr, ms: Date.now() - t };
}
