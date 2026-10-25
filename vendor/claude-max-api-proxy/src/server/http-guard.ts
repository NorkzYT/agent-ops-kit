import { isLoopbackAddress } from "./admin-access.js";

// Loopback host names that are always accepted for the Host header. Requests
// forged via DNS rebinding carry the attacker's domain in Host, so restricting
// to these (plus explicitly configured hosts) defeats the attack (F2).
const LOOPBACK_HOSTNAMES = new Set([
  "localhost",
  "127.0.0.1",
  "::1",
  "[::1]",
]);

function hostnameWithoutPort(host: string): string {
  const trimmed = host.trim().toLowerCase();
  if (!trimmed) return trimmed;
  // Bracketed IPv6, optionally with a port: [::1] or [::1]:3456
  if (trimmed.startsWith("[")) {
    const end = trimmed.indexOf("]");
    return end >= 0 ? trimmed.slice(0, end + 1) : trimmed;
  }
  // IPv4/hostname with an optional :port (a bare IPv6 has multiple colons and
  // is left untouched — those only arrive bracketed in a Host header).
  const firstColon = trimmed.indexOf(":");
  const lastColon = trimmed.lastIndexOf(":");
  if (firstColon >= 0 && firstColon === lastColon) {
    return trimmed.slice(0, firstColon);
  }
  return trimmed;
}

/**
 * Validate the Host header against the loopback set plus any explicitly
 * configured allowed hosts. Missing Host is rejected. Port is ignored for the
 * loopback comparison; configured hosts are matched with and without a port.
 */
export function isAllowedHost(
  hostHeader: string | undefined,
  allowedHosts: string[] = [],
): boolean {
  if (!hostHeader) return false;
  const host = hostHeader.trim().toLowerCase();
  if (!host) return false;

  const bare = hostnameWithoutPort(host);
  if (LOOPBACK_HOSTNAMES.has(bare) || isLoopbackAddress(bare)) {
    return true;
  }

  for (const allowed of allowedHosts) {
    const candidate = allowed.trim().toLowerCase();
    if (!candidate) continue;
    if (candidate === host || candidate === bare) return true;
    if (hostnameWithoutPort(candidate) === bare) return true;
  }
  return false;
}

/**
 * Decide which Origin (if any) to reflect in Access-Control-Allow-Origin. Only
 * an origin present in the explicit allowlist is reflected; otherwise CORS
 * headers are omitted entirely so no cross-origin access is granted (F2).
 */
export function resolveCorsOrigin(
  origin: string | undefined,
  allowedOrigins: string[] = [],
): string | undefined {
  if (!origin || allowedOrigins.length === 0) return undefined;
  const normalized = origin.trim();
  return allowedOrigins.some((allowed) => allowed.trim() === normalized)
    ? normalized
    : undefined;
}

/**
 * Refuse to start when binding a non-loopback interface without an API key.
 * Loopback-only with no key stays allowed for local development (F1).
 */
export function assertSafeBind(
  host: string | undefined,
  apiKey: string | undefined,
): void {
  const boundHost = (host ?? "127.0.0.1").trim();
  if (isLoopbackAddress(boundHost)) return;
  if (apiKey?.trim()) return;
  throw new Error(
    `Refusing to bind non-loopback host '${boundHost}' without an API key. ` +
      "Set CLAUDE_MAX_PROXY_API_KEY to expose the proxy off localhost.",
  );
}
