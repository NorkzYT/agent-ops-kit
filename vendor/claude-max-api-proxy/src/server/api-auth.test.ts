import assert from "node:assert/strict";
import test from "node:test";
import { extractBearerToken, isApiKeyAuthorized } from "./api-auth.js";

test("extractBearerToken parses the Bearer scheme case-insensitively", () => {
  assert.equal(extractBearerToken("Bearer secret-token"), "secret-token");
  assert.equal(extractBearerToken("bearer  spaced "), "spaced");
  assert.equal(extractBearerToken(undefined), undefined);
  assert.equal(extractBearerToken("Basic abc"), undefined);
});

test("isApiKeyAuthorized is open when no key is configured", () => {
  assert.equal(isApiKeyAuthorized({}), true);
  assert.equal(isApiKeyAuthorized({ apiKey: "   " }), true);
});

test("isApiKeyAuthorized requires a matching bearer token when a key is set", () => {
  assert.equal(
    isApiKeyAuthorized({ apiKey: "s3cret", authorization: "Bearer s3cret" }),
    true,
  );
  assert.equal(
    isApiKeyAuthorized({ apiKey: "s3cret", authorization: "Bearer wrong" }),
    false,
  );
  assert.equal(isApiKeyAuthorized({ apiKey: "s3cret" }), false);
  assert.equal(
    isApiKeyAuthorized({ apiKey: "s3cret", authorization: "s3cret" }),
    false,
  );
});
