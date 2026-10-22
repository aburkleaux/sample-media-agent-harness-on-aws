/**
 * Catalog visibility from RECORDED target ownership.
 *
 * This replaces a TypeScript reimplementation of a hash that also lived in the
 * interceptor Lambda and the agent — three copies of one algorithm across two
 * languages, held together by a hand-maintained golden vector. Ownership is now
 * a row the agent writes at registration time, so this surface and the gateway
 * interceptor read the same fact and cannot drift.
 */

import { describe, it, expect } from "vitest";
import { actorMaySeeTool } from "./toolCatalog";

const ALICE = "USER#alice";
const BOB = "e1b7c0de-1111-2222-3333-444455556666";

const ALICE_TARGET = "usr-notes-a1";
const BOB_TARGET = "usr-secrets-b2";

const owners = new Map<string, string>([
  [ALICE_TARGET, ALICE],
  [BOB_TARGET, BOB],
]);

const aliceTool = `${ALICE_TARGET}___read_note`;
const bobTool = `${BOB_TARGET}___read_secret`;

describe("actorMaySeeTool", () => {
  it("shows an owner their own tool", () => {
    expect(actorMaySeeTool(ALICE, aliceTool, owners)).toBe(true);
  });

  it("hides another actor's tool", () => {
    expect(actorMaySeeTool(BOB, aliceTool, owners)).toBe(false);
    expect(actorMaySeeTool(ALICE, bobTool, owners)).toBe(false);
  });

  it("shows platform tools to everyone", () => {
    expect(actorMaySeeTool(BOB, "media-outputs___get_download_url", owners)).toBe(true);
    expect(actorMaySeeTool(BOB, "x_amz_bedrock_agentcore_search", owners)).toBe(true);
  });

  it("hides user tools when no actor is known", () => {
    // Fail closed, matching the interceptor.
    expect(actorMaySeeTool("", aliceTool, owners)).toBe(false);
    expect(actorMaySeeTool("", "media-outputs___get_download_url", owners)).toBe(true);
  });

  it("hides a user target with no ownership record", () => {
    // An unrecorded target is unattributable, not public.
    expect(actorMaySeeTool(ALICE, "usr-unrecorded-zz___tool", owners)).toBe(false);
  });

  it("hides everything user-owned when the map is empty", () => {
    expect(actorMaySeeTool(ALICE, aliceTool, new Map())).toBe(false);
  });

  it("resolves the target as the first segment of a multi-separator name", () => {
    const deep = `${ALICE_TARGET}___server___deep_tool`;
    expect(actorMaySeeTool(ALICE, deep, owners)).toBe(true);
    expect(actorMaySeeTool(BOB, deep, owners)).toBe(false);
  });

  it("does not interpret the target name for meaning", () => {
    // The name is opaque: an owner is whatever the record says, so a target
    // whose name resembles another user's is still owned by its record.
    const confusing = new Map([["usr-alice-lookalike", BOB]]);
    expect(actorMaySeeTool(BOB, "usr-alice-lookalike___t", confusing)).toBe(true);
    expect(actorMaySeeTool(ALICE, "usr-alice-lookalike___t", confusing)).toBe(false);
  });
});
