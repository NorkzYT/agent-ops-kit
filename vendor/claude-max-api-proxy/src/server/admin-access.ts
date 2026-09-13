import { timingSafeEqual } from "node:crypto";
import type { NextFunction, Request, Response } from "express";

export function isLoopbackAddress(address: string | undefined): boolean {
  if (!address) return false;
  const normalized = address.toLowerCase().replace(/^::ffff:/, "");
  return (
    normalized === "127.0.0.1" ||
    normalized === "::1" ||
    normalized === "localhost"
  );
}

/**
 * Constant-time token comparison. Guards against timingSafeEqual throwing on a
 * length mismatch by short-circuiting on unequal lengths (the length itself is
 * not secret).
 */
export function safeTokenEquals(provided: string, expected: string): boolean {
  const left = Buffer.from(provided);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

export function isAdminAuthorized(params: {
  remoteAddress?: string;
  authorization?: string;
  adminTokenHeader?: string;
  adminToken?: string;
}): boolean {
  const expected = params.adminToken?.trim();
  if (!expected) {
    return isLoopbackAddress(params.remoteAddress);
  }
  // The dedicated X-Admin-Token header is authoritative: when present it decides
  // the outcome so the API Authorization header can never shadow (or rescue) it.
  // Only when no admin header is supplied do we fall back to a Bearer token, so
  // single-token deployments (admin secret sent via Authorization, no separate
  // API key) keep working while dual-token deployments stay fail-closed.
  const provided =
    params.adminTokenHeader !== undefined
      ? params.adminTokenHeader.trim()
      : params.authorization?.match(/^Bearer\s+(.+)$/i)?.[1]?.trim();
  return Boolean(provided && safeTokenEquals(provided, expected));
}

export function requireAdminAccess(
  req: Request,
  res: Response,
  next: NextFunction,
): void {
  if (
    isAdminAuthorized({
      remoteAddress: req.socket.remoteAddress,
      authorization: req.header("authorization"),
      adminTokenHeader: req.header("x-admin-token"),
      adminToken: process.env.CLAUDE_PROXY_ADMIN_TOKEN,
    })
  ) {
    next();
    return;
  }

  res.status(403).json({
    error: {
      message:
        "Administrative access is restricted to localhost unless CLAUDE_PROXY_ADMIN_TOKEN is configured.",
      type: "permission_error",
      code: "admin_access_denied",
    },
  });
}
