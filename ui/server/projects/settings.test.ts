// Project settings: description, tags, and the indexing flag (default OFF; turned ON only via the
// Asset_Service, never through the registry). Registry over the in-memory store, plus the turn context.
import { describe, expect, it } from "vitest";
import { InMemoryTransactStore } from "./inMemoryStore";
import { ProjectRegistry, toProjectMeta } from "./registry";
import type { RegistryResult } from "./registry";
import { indexingOn, validateDescription, validateTags } from "./settings";
import { buildTurnContext } from "./turnContext";

const pidOf = (n: number) => "p" + n.toString(16).padStart(32, "0");

function setup() {
  let n = 0;
  const store = new InMemoryTransactStore();
  const reg = new ProjectRegistry({
    store, now: () => new Date("2026-10-07T00:00:00.000Z"), sleep: async () => {}, random: () => 0,
    newId: () => pidOf(++n),
  });
  return { store, reg };
}

function value<T>(r: RegistryResult<T>): T {
  if (!r.ok) throw new Error(`expected ok, got ${JSON.stringify(r.error)}`);
  return r.value;
}

describe("validators", () => {
  it("description: trimmed, 500 max, newlines allowed, control characters refused", () => {
    expect(validateDescription("  Meridian launch\r\ncuts  ")).toBe("Meridian launch\ncuts");
    expect(validateDescription("x".repeat(501))).toMatchObject({ code: "invalid_settings", field: "description" });
    expect(validateDescription("bell\u0007")).toMatchObject({ field: "description" });
    expect(validateDescription(5)).toMatchObject({ field: "description" });
  });

  it("tags: trimmed, de-duplicated case-insensitively, at most 10 of 32 characters", () => {
    expect(validateTags([" promo ", "Promo", "Q4  2026", ""])).toEqual(["promo", "Q4 2026"]);
    expect(validateTags(Array.from({ length: 11 }, (_, i) => `t${i}`))).toMatchObject({ field: "tags" });
    expect(validateTags(["x".repeat(33)])).toMatchObject({ field: "tags" });
    expect(validateTags("promo")).toMatchObject({ field: "tags" });
  });

  it("indexing reads absent as ON (pre-settings rows) and anything but true as OFF", () => {
    expect(indexingOn({})).toBe(true);
    expect(indexingOn({ indexing: true })).toBe(true);
    expect(indexingOn({ indexing: false })).toBe(false);
    expect(indexingOn({ indexing: "true" })).toBe(false);
  });
});

describe("registry", () => {
  it("creates with indexing OFF by default and stores description and tags", async () => {
    const { reg } = setup();
    const p = value(await reg.create("alice", "Promo", { description: " For the launch ", tags: ["promo"] }));
    expect(p).toMatchObject({ description: "For the launch", tags: ["promo"], indexing: false });
    const detail = value(await reg.detail("alice", p.projectId));
    expect(detail.project).toMatchObject({ description: "For the launch", tags: ["promo"], indexing: false });
  });

  it("creates with indexing ON when asked", async () => {
    const { reg } = setup();
    expect(value(await reg.create("alice", "Searchable", { indexing: true })).indexing).toBe(true);
  });

  // The sidebar's / chat picker's search mark reads the LIST, so the list must carry the flag.
  it("the project list carries each project's indexing state", async () => {
    const { reg } = setup();
    const on = value(await reg.create("alice", "Searchable", { indexing: true }));
    const off = value(await reg.create("alice", "Plain"));
    const list = await reg.list("alice");
    expect(list.find((p) => p.projectId === on.projectId)?.indexing).toBe(true);
    expect(list.find((p) => p.projectId === off.projectId)?.indexing).toBe(false);
  });

  it("refuses bad settings on create without writing anything", async () => {
    const { reg } = setup();
    const r = await reg.create("alice", "Promo", { tags: ["x".repeat(40)] });
    expect(r.ok).toBe(false);
    expect((await reg.list("alice")).filter((p) => p.name === "Promo")).toHaveLength(0);
    expect((await reg.create("alice", "Promo", { indexing: "yes" as unknown as boolean })).ok).toBe(false);
  });

  it("the Personal project starts with indexing OFF", async () => {
    const { reg } = setup();
    expect(value(await reg.ensurePersonal("alice")).indexing).toBe(false);
  });

  it("updateSettings: owner only, and leaves indexing alone", async () => {
    const { reg } = setup();
    const p = value(await reg.create("alice", "Promo"));
    value(await reg.invite("alice", p.projectId, { sub: "bob", display: "bob" }));
    const denied = await reg.updateSettings("bob", p.projectId, { description: "mine" });
    expect(denied).toMatchObject({ ok: false, error: { code: "owner_only", action: "settings" } });
    const updated = value(await reg.updateSettings("alice", p.projectId, { description: "Cuts", tags: ["a", "b"] }));
    expect(updated).toMatchObject({ description: "Cuts", tags: ["a", "b"], indexing: false });
    expect(value(await reg.detail("bob", p.projectId)).project.description).toBe("Cuts");
    const bad = await reg.updateSettings("alice", p.projectId, { description: 7 });
    expect(bad).toMatchObject({ ok: false, error: { code: "invalid_settings", field: "description" } });
  });

  it("an old META row without the flag reads as indexing ON", () => {
    const m = toProjectMeta({ PK: "PROJECT#x", SK: "META", projectId: pidOf(9), name: "Old", nameFold: "old",
      ownerSub: "a", personal: false, status: "active", memberCount: 0, createdAt: "t" });
    expect(m).toMatchObject({ indexing: true, description: "", tags: [] });
  });
});

describe("turn context", () => {
  it("carries indexing and a non-empty description; omits both when unknown", () => {
    const base = { turnId: "t", webSearch: false, projectAccess: "ok" as const };
    const withSettings = buildTurnContext({
      ...base, activeProject: { id: pidOf(1), name: "Promo", personal: false, indexing: false, description: "Cuts" },
    });
    expect(withSettings.active_project).toEqual({ id: pidOf(1), name: "Promo", collection_id: pidOf(1),
      personal: false, indexing: false, description: "Cuts" });
    const unknown = buildTurnContext({ ...base, activeProject: { id: pidOf(1), name: "Promo", personal: false } });
    expect(unknown.active_project).toEqual({ id: pidOf(1), name: "Promo", collection_id: pidOf(1), personal: false });
  });
});
