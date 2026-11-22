// Minimal static server for fixtures/: Node stdlib only, bound to 127.0.0.1
// on an ephemeral port, GET/HEAD only, no directory listing, no traversal.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { FIXTURES } from './paths.mjs';

const TYPES = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml', '.txt': 'text/plain; charset=utf-8' };

export function startServer(root = FIXTURES) {
  const base = fs.realpathSync(root);
  const server = http.createServer((req, res) => {
    if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405).end(); return; }
    let rel;
    try { rel = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname); } catch { res.writeHead(400).end(); return; }
    if (rel === '/') rel = '/index.html';
    const file = path.resolve(base, '.' + rel);
    let real;
    try { real = fs.realpathSync(file); } catch { res.writeHead(404).end('not found'); return; }
    if (!real.startsWith(base + path.sep) || !fs.statSync(real).isFile()) { res.writeHead(404).end('not found'); return; }
    const body = fs.readFileSync(real);
    res.writeHead(200, { 'content-type': TYPES[path.extname(real)] || 'application/octet-stream', 'content-length': body.length, 'cache-control': 'no-store' });
    res.end(req.method === 'HEAD' ? undefined : body);
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { address, port } = server.address();
      if (address !== '127.0.0.1') { server.close(); reject(new Error(`server bound to ${address}`)); return; }
      resolve({
        server,
        port,
        origin: `http://127.0.0.1:${port}`,
        url: (file) => `http://127.0.0.1:${port}/${file}`,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}
