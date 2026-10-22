// Tests for the `/lab` runtime session id.
//
// Why this exists: a `/lab` CRUD action is a pure DynamoDB read, but it reaches
// DynamoDB *through* an AgentCore runtime invoke. Measured against sandbox, a
// cold runtime session costs ~8.5s while a REUSED session costs ~190ms — the
// 8.3s delta is one-time container init (`_init_agent_subsystems` plus
// `_ensure_gateway_connected`, both of which run before `main.py`'s Lab bypass).
// `/api` already pins a session; `/lab` sent none, so every Capabilities tab
// paid full init.
//
// The safety property is the reason this is derived rather than constant.
// `agent/src/main.py` binds the gateway connection's `actor_id` once per
// container and comments: "a session belongs to one user, so this container
// serves one actor for its lifetime." So a session id shared across actors
// would handnone user a container whose gateway connection asserts a DIFFERENT
// actor — exactly the cross-actor tool leak the interceptor exists to prevent.
// Hence: one session per actor, never a global or per-process value.

import { describe, it, expect } from "vitest";

import { labRuntimeSessionId } from "./labSession";

describe("labRuntimeSessionId", () => {
  it("returns a stable id for the same actor", () => {
    // Stability is what buys the warm container: two Capabilities tabs in one
    // sitting must land on the same microVM.
    const a = labRuntimeSessionId("operator@mediaworksdemo.com");
    const b = labRuntimeSessionId("operator@mediaworksdemo.com");
    expect(a).toBe(b);
  });

  it("returns different ids for different actors", () => {
    // The isolation property. If these collided, one user's request could be
    // served by a container whose gateway connection asserts another user.
    const a = labRuntimeSessionId("operator@mediaworksdemo.com");
    const b = labRuntimeSessionId("extcaller2@mediaworksdemo.com");
    expect(a).not.toBe(b);
  });

  it("meets the runtime's 33-character minimum", () => {
    // AgentCore rejects shorter session ids; the render route pads for the same
    // reason. A short actor id must not produce a short session id.
    expect(labRuntimeSessionId("a").length).toBeGreaterThanOrEqual(33);
    expect(
      labRuntimeSessionId("operator@mediaworksdemo.com").length,
    ).toBeGreaterThanOrEqual(33);
  });

  it("does not leak the actor id verbatim", () => {
    // The session id reaches AWS as a header and appears in logs/traces, so it
    // should not carry the raw email address.
    const id = labRuntimeSessionId("operator@mediaworksdemo.com");
    expect(id).not.toContain("operator@mediaworksdemo.com");
    expect(id).not.toContain("@");
  });

  it("returns empty string for a missing actor", () => {
    // No actor ⇒ no pinning. Falling back to a shared/random session would
    // either break isolation or silently defeat the warm-container win; an
    // empty string makes the caller omit the header, preserving today's
    // behavior on the unauthenticated path.
    expect(labRuntimeSessionId("")).toBe("");
    expect(labRuntimeSessionId(undefined)).toBe("");
  });

  it("uses only characters safe for an HTTP header value", () => {
    const id = labRuntimeSessionId("operator@mediaworksdemo.com");
    expect(id).toMatch(/^[A-Za-z0-9._-]+$/);
  });
});
