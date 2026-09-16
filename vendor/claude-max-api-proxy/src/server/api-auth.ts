import type { NextFunction, Request, Response } from "express";
import { runtimeConfig } from "../config.js";
import { safeTokenEquals } from "./admin-access.js";

export function extractBearerToken(
  authorization: string | undefined,
): string | undefined {
  return authorization?.match(/^Bearer\s+(.+)$/i)?.[1]?.trim();
}

/**
 * Returns true when the request is authorized against the shared API key.
 * When no key is configured the proxy is open (back-compat for local dev),
 * so this returns true. The bearer token is compared in constant time (F1).
 */
export function isApiKeyAuthorized(params: {
  authorization?: string;
  apiKey?: string;
}): boolean {
  const expected = params.apiKey?.trim();
  if (!expected) return true;
  const bearer = extractBearerToken(params.authorization);
  return Boolean(bearer && safeTokenEquals(bearer, expected));
}

/**
 * Express middleware enforcing `Authorization: Bearer <key>` when
 * CLAUDE_MAX_PROXY_API_KEY is set. Standard OpenAI SDKs already send this
 * header, so client compatibility is preserved. Reused across /v1 and the
 * admin/ops mutation surfaces rather than duplicated per route (F1).
 */
export function requireApiKey(
  req: Request,
  res: Response,
  next: NextFunction,
): void {
  if (
    isApiKeyAuthorized({
      authorization: req.header("authorization"),
      apiKey: runtimeConfig.apiKey,
    })
  ) {
    next();
    return;
  }

  res.status(401).json({
    error: {
      message:
        "Missing or invalid API key. Provide 'Authorization: Bearer <CLAUDE_MAX_PROXY_API_KEY>'.",
      type: "authentication_error",
      code: "invalid_api_key",
    },
  });
}
