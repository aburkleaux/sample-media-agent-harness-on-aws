// Validates the Slack request authorizer's edge checks (no AWS, no network).
// Run: node --test handler.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { isAuthorized } from "./handler.ts";

const NOW = 1_788_400_000;
const goodSig = "v0=" + "a".repeat(64);

test("accepts a well-formed signature with a fresh timestamp", () => {
  const headers = { "X-Slack-Signature": goodSig, "X-Slack-Request-Timestamp": String(NOW - 10) };
  assert.equal(isAuthorized(headers, NOW), true);
});

test("accepts case-insensitive header names (HTTP API lowercases keys)", () => {
  const headers = { "x-slack-signature": goodSig, "x-slack-request-timestamp": String(NOW) };
  assert.equal(isAuthorized(headers, NOW), true);
});

test("rejects a missing signature header", () => {
  assert.equal(isAuthorized({ "X-Slack-Request-Timestamp": String(NOW) }, NOW), false);
});

test("rejects a malformed signature (wrong prefix / length / non-hex)", () => {
  const ts = String(NOW);
  assert.equal(isAuthorized({ "X-Slack-Signature": "v1=" + "a".repeat(64), "X-Slack-Request-Timestamp": ts }, NOW), false);
  assert.equal(isAuthorized({ "X-Slack-Signature": "v0=" + "a".repeat(10), "X-Slack-Request-Timestamp": ts }, NOW), false);
  assert.equal(isAuthorized({ "X-Slack-Signature": "v0=" + "z".repeat(64), "X-Slack-Request-Timestamp": ts }, NOW), false);
});

test("rejects a stale timestamp beyond the replay window (>300s old)", () => {
  const headers = { "X-Slack-Signature": goodSig, "X-Slack-Request-Timestamp": String(NOW - 301) };
  assert.equal(isAuthorized(headers, NOW), false);
});

test("rejects a future-dated timestamp beyond the window (>300s ahead)", () => {
  const headers = { "X-Slack-Signature": goodSig, "X-Slack-Request-Timestamp": String(NOW + 301) };
  assert.equal(isAuthorized(headers, NOW), false);
});

test("rejects a non-numeric / empty timestamp", () => {
  assert.equal(isAuthorized({ "X-Slack-Signature": goodSig, "X-Slack-Request-Timestamp": "not-a-number" }, NOW), false);
  assert.equal(isAuthorized({ "X-Slack-Signature": goodSig, "X-Slack-Request-Timestamp": "" }, NOW), false);
});

test("rejects when there are no headers at all", () => {
  assert.equal(isAuthorized(undefined, NOW), false);
});
