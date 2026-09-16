import assert from "node:assert/strict";
import test from "node:test";
import {
  assertSafeBind,
  isAllowedHost,
  resolveCorsOrigin,
} from "./http-guard.js";

test("isAllowedHost accepts loopback hosts regardless of port", () => {
  assert.equal(isAllowedHost("127.0.0.1:3456"), true);
  assert.equal(isAllowedHost("localhost:8080"), true);
  assert.equal(isAllowedHost("localhost"), true);
  assert.equal(isAllowedHost("[::1]:3456"), true);
  assert.equal(isAllowedHost("[::1]"), true);
});

test("isAllowedHost rejects non-loopback hosts unless configured", () => {
  assert.equal(isAllowedHost("evil.example.com"), false);
  assert.equal(isAllowedHost("proxy.internal:3456"), false);
  assert.equal(isAllowedHost(undefined), false);
  assert.equal(isAllowedHost(""), false);
  // Explicit allowlist entries are honored, with and without a port.
  assert.equal(
    isAllowedHost("proxy.internal:3456", ["proxy.internal"]),
    true,
  );
  assert.equal(
    isAllowedHost("proxy.internal", ["proxy.internal:3456"]),
    true,
  );
});

test("resolveCorsOrigin only reflects allowlisted origins", () => {
  assert.equal(resolveCorsOrigin("https://app.test", []), undefined);
  assert.equal(resolveCorsOrigin(undefined, ["https://app.test"]), undefined);
  assert.equal(
    resolveCorsOrigin("https://app.test", ["https://app.test"]),
    "https://app.test",
  );
  assert.equal(
    resolveCorsOrigin("https://evil.test", ["https://app.test"]),
    undefined,
  );
});

test("assertSafeBind refuses non-loopback bind without an API key", () => {
  assert.doesNotThrow(() => assertSafeBind("127.0.0.1", undefined));
  assert.doesNotThrow(() => assertSafeBind("localhost", undefined));
  assert.doesNotThrow(() => assertSafeBind("::1", undefined));
  assert.doesNotThrow(() => assertSafeBind(undefined, undefined));
  assert.doesNotThrow(() => assertSafeBind("0.0.0.0", "an-api-key"));
  assert.throws(() => assertSafeBind("0.0.0.0", undefined), /non-loopback/);
  assert.throws(() => assertSafeBind("0.0.0.0", "   "), /non-loopback/);
  assert.throws(() => assertSafeBind("192.168.1.10", undefined), /non-loopback/);
});
