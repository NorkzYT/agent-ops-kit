import assert from "node:assert/strict";
import test from "node:test";
import { createServer, type Server } from "node:http";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import { runtimeConfig } from "../config.js";
import { createApp } from "./index.js";

interface RawResponse {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: string;
}

async function withServer(
  fn: (baseHost: string, port: number) => Promise<void>,
): Promise<void> {
  const server: Server = createServer(createApp());
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  try {
    await fn(`127.0.0.1:${port}`, port);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }
}

function send(
  port: number,
  method: string,
  path: string,
  headers: Record<string, string>,
): Promise<RawResponse> {
  return new Promise<RawResponse>((resolve, reject) => {
    const req = request(
      { host: "127.0.0.1", port, method, path, headers },
      (res) => {
        let body = "";
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () =>
          resolve({ status: res.statusCode ?? 0, headers: res.headers, body }),
        );
      },
    );
    req.on("error", reject);
    req.end();
  });
}

test("API key gate rejects unauthenticated /v1 requests and allows correct bearer", async () => {
  const previousKey = runtimeConfig.apiKey;
  runtimeConfig.apiKey = "integration-secret";
  try {
    await withServer(async (host, port) => {
      const missing = await send(port, "GET", "/v1/models", { host });
      assert.equal(missing.status, 401);
      assert.match(missing.body, /invalid_api_key/);

      const wrong = await send(port, "GET", "/v1/models", {
        host,
        authorization: "Bearer nope",
      });
      assert.equal(wrong.status, 401);

      // Correct bearer clears the auth middleware (handler may then 200/5xx,
      // but must not be a 401 auth rejection).
      const ok = await send(port, "GET", "/v1/models", {
        host,
        authorization: "Bearer integration-secret",
      });
      assert.notEqual(ok.status, 401);
    });
  } finally {
    runtimeConfig.apiKey = previousKey;
  }
});

test("health endpoints stay reachable without an API key", async () => {
  const previousKey = runtimeConfig.apiKey;
  runtimeConfig.apiKey = "integration-secret";
  try {
    await withServer(async (host, port) => {
      // Unregistered liveness alias short-circuits past auth/host guards and
      // lands on the 404 handler (not a 401/403), proving health paths bypass.
      const livez = await send(port, "GET", "/livez", {
        host: "evil.example.com",
      });
      assert.equal(livez.status, 404);
    });
  } finally {
    runtimeConfig.apiKey = previousKey;
  }
});

test("Host header is validated against DNS rebinding", async () => {
  await withServer(async (_host, port) => {
    const rebind = await send(port, "GET", "/v1/models", {
      host: "attacker.example.com",
    });
    assert.equal(rebind.status, 403);
    assert.match(rebind.body, /host_not_allowed/);
  });
});

test("CORS is opt-in and preflight is not blindly approved", async () => {
  const previousOrigins = runtimeConfig.allowedOrigins;
  runtimeConfig.allowedOrigins = ["https://allowed.test"];
  try {
    await withServer(async (host, port) => {
      // Disallowed origin: no reflection, preflight refused.
      const denied = await send(port, "OPTIONS", "/v1/chat/completions", {
        host,
        origin: "https://evil.test",
      });
      assert.equal(denied.status, 403);
      assert.equal(denied.headers["access-control-allow-origin"], undefined);

      // Allowed origin: reflected exactly (never "*"), preflight 204.
      const allowed = await send(port, "OPTIONS", "/v1/chat/completions", {
        host,
        origin: "https://allowed.test",
      });
      assert.equal(allowed.status, 204);
      assert.equal(
        allowed.headers["access-control-allow-origin"],
        "https://allowed.test",
      );

      // Same-origin (no Origin) preflight is harmless 204 with no CORS header.
      const plain = await send(port, "OPTIONS", "/v1/chat/completions", {
        host,
      });
      assert.equal(plain.status, 204);
      assert.equal(plain.headers["access-control-allow-origin"], undefined);
    });
  } finally {
    runtimeConfig.allowedOrigins = previousOrigins;
  }
});
