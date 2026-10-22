import { describe, it, expect } from "vitest";
import {
  ACTOR_OWNER_TAG_RE,
  EXTERNAL_OWNER_TAG_RE,
  artifactOwnerTag,
  externalArtifactOwnerTag,
  ownerTaggedRenderSession,
  ownerTagOfRenderSession,
} from "./artifactOwner";

describe("artifact owner tags", () => {
  it("are deterministic, shaped, and never contain the actor id", () => {
    const sub = "4681f2da-c97f-49e0-aea5-359741dfcb3e";
    expect(artifactOwnerTag(sub)).toBe(artifactOwnerTag(sub));
    expect(artifactOwnerTag(sub)).toMatch(ACTOR_OWNER_TAG_RE);
    expect(artifactOwnerTag(sub)).not.toContain(sub.slice(0, 8));
    expect(artifactOwnerTag("a")).not.toBe(artifactOwnerTag("b"));
  });

  it("separate internal actors from external callers, and external callers per (agent, issuer, sub)", () => {
    const x = externalArtifactOwnerTag("agent-1", "https://idp", "sub-1");
    expect(x).toMatch(EXTERNAL_OWNER_TAG_RE);
    expect(x).not.toMatch(ACTOR_OWNER_TAG_RE);
    expect(externalArtifactOwnerTag("agent-2", "https://idp", "sub-1")).not.toBe(x);
    expect(externalArtifactOwnerTag("agent-1", "https://other", "sub-1")).not.toBe(x);
    expect(externalArtifactOwnerTag("agent-1", "https://idp", "sub-2")).not.toBe(x);
    // Length-prefixed material: moving a separator between fields is a different triple.
    expect(externalArtifactOwnerTag("a|b", "c", "d")).not.toBe(externalArtifactOwnerTag("a", "b|c", "d"));
  });

  it("refuse an empty identity rather than tagging everyone alike", () => {
    expect(() => artifactOwnerTag("")).toThrow();
    expect(() => externalArtifactOwnerTag("agent", "", "sub")).toThrow();
  });

  it("round-trip through a render session id, which stays a valid runtime session (>= 33 chars)", () => {
    const sid = ownerTaggedRenderSession("actor-1", 1_700_000_000_000);
    expect(sid.length).toBeGreaterThanOrEqual(33);
    expect(sid).toMatch(/^[A-Za-z0-9-]+$/);
    expect(ownerTagOfRenderSession(sid)).toBe(artifactOwnerTag("actor-1"));
    // Legacy random render sessions carry no owner.
    expect(ownerTagOfRenderSession("render-1787851114826-pmdrgo7mvih0")).toBeNull();
    expect(ownerTagOfRenderSession("4681f2da-c97f-49e0-aea5-359741dfcb3e")).toBeNull();
  });
});
