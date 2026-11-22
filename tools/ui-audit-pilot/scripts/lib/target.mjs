// Target guard: the pilot only audits the owned fixture pages served from
// 127.0.0.1. Anything else (other hosts, "localhost", IPv6, file:, https:,
// credentials, a path that is not an owned fixture page, a query or fragment)
// is refused before a browser is started. The port cannot be proven to belong
// to scripts/serve.mjs: another local service on that port answering the same
// path would be audited, so only pass ports you started.
import { PAGES } from './paths.mjs';

export class TargetRefused extends Error {}
const OWNED_PATHS = new Set(Object.values(PAGES).map((f) => `/${f}`));

export function assertLoopbackTarget(raw) {
  let u;
  try {
    u = new URL(String(raw));
  } catch {
    throw new TargetRefused(`refused target: not a URL: ${raw}`);
  }
  if (u.protocol !== 'http:') throw new TargetRefused(`refused target: protocol ${u.protocol} (only http: on 127.0.0.1)`);
  if (u.hostname !== '127.0.0.1') throw new TargetRefused(`refused target: host ${u.hostname} is not 127.0.0.1`);
  if (u.username || u.password) throw new TargetRefused('refused target: credentials in URL');
  if (!u.port) throw new TargetRefused('refused target: explicit ephemeral port required');
  if (!OWNED_PATHS.has(u.pathname)) throw new TargetRefused(`refused target: path ${u.pathname} is not an owned fixture page (${[...OWNED_PATHS].join(', ')})`);
  if (u.search || u.hash || String(raw).includes('#')) throw new TargetRefused('refused target: query strings and fragments are not allowed');
  return u.href;
}
