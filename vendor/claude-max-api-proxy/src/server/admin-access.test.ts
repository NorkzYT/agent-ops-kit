import assert from "node:assert/strict";
import test from "node:test";
import { isAdminAuthorized } from "./admin-access.js";

test("admin access defaults to loopback only", () => {
  assert.equal(isAdminAuthorized({ remoteAddress: "127.0.0.1" }), true);
  assert.equal(isAdminAuthorized({ remoteAddress: "::1" }), true);
  assert.equal(isAdminAuthorized({ remoteAddress: "::ffff:127.0.0.1" }), true);
  assert.equal(isAdminAuthorized({ remoteAddress: "192.168.1.20" }), false);
});

test("configured admin token is required even on loopback", () => {
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "127.0.0.1",
      adminToken: "secret",
    }),
    false,
  );
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "192.168.1.20",
      adminToken: "secret",
      authorization: "Bearer secret",
    }),
    true,
  );
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "127.0.0.1",
      adminToken: "secret",
      authorization: "Bearer wrong",
    }),
    false,
  );
});

test("distinct API and admin tokens: X-Admin-Token is authoritative", () => {
  // Real dual-token deployment: the API key rides Authorization (consumed by the
  // outer API middleware) while the admin secret rides X-Admin-Token. The API
  // bearer must never satisfy the admin gate on its own.
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "192.168.1.20",
      adminToken: "admin-secret",
      authorization: "Bearer api-key",
      adminTokenHeader: "admin-secret",
    }),
    true,
  );
  // API bearer present, no admin header: fail closed (Authorization must not be
  // accepted as the admin token when it carries the API key).
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "192.168.1.20",
      adminToken: "admin-secret",
      authorization: "Bearer api-key",
    }),
    false,
  );
  // A wrong admin header cannot be rescued by a matching Authorization bearer:
  // the dedicated header takes precedence and is not shadowed.
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "192.168.1.20",
      adminToken: "admin-secret",
      authorization: "Bearer admin-secret",
      adminTokenHeader: "wrong",
    }),
    false,
  );
  // Correct admin header, wrong API bearer: still authorized for the admin gate
  // (the API bearer is the outer middleware's concern, not this check's).
  assert.equal(
    isAdminAuthorized({
      remoteAddress: "192.168.1.20",
      adminToken: "admin-secret",
      authorization: "Bearer nope",
      adminTokenHeader: "admin-secret",
    }),
    true,
  );
});
