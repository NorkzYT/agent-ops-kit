// Run a child process, capture stdout/stderr, never through a shell.
import { spawn } from 'node:child_process';

export function run(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { ...opts, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    const t = opts.timeoutMs ? setTimeout(() => child.kill('SIGKILL'), opts.timeoutMs) : null;
    child.on('error', (err) => { if (t) clearTimeout(t); resolve({ code: 127, signal: null, stdout, stderr: stderr + String(err) }); });
    child.on('close', (code, signal) => { if (t) clearTimeout(t); resolve({ code, signal, stdout, stderr }); });
  });
}
