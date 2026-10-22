// Project_Registry operations (task 6.2) over InMemoryTransactStore. Includes the fixed-example ports of
// docs/prototypes/media-harness-projects/validation/test_v4_registry_model.py: concurrent-rename orphan
// guard, case-only rename double-item rejection, delete-vs-rename and delete-vs-invite orphans, and the
// unhandled Personal conflict. The racing op commits between the request's pre-read and its commit.
import { describe, expect, it } from "vitest";
import { InMemoryTransactStore } from "./inMemoryStore";
import {
  MAX_OWNED_PROJECTS,
  MAX_PROJECT_MEMBERS,
  PROJECT_NOT_FOUND,
  ProjectRegistry,
  registryErrorStatus,
} from "./registry";
import type { ProjectRegistryDeps, RegistryError, RegistryResult } from "./registry";
import { NAME_RULES } from "./names";
import {
  countersKey,
  memberKey,
  metaKey,
  mirrorKey,
  nameGuardKey,
  personalKey,
  tombstoneItem,
} from "./schema";
import type { Item, Key, TransactOp, TransactStore } from "./store";

const T0 = new Date("2026-10-01T00:00:00.000Z");
const tomb = (pid: string, deletedAt: string, purgedAt: string): Item => ({ ...tombstoneItem(pid, deletedAt, purgedAt) });
const pidOf = (n: number) => "p" + n.toString(16).padStart(32, "0");

/** Wraps the in-memory store: records every transaction and runs a racing op before the next write. */
class RacingStore implements TransactStore {
  readonly writes: TransactOp[][] = [];
  private hooks: (() => Promise<void>)[] = [];
  constructor(readonly inner: InMemoryTransactStore) {}
  beforeNextWrite(fn: () => Promise<void>): void {
    this.hooks.push(fn);
  }
  async transactWrite(ops: readonly TransactOp[]): Promise<void> {
    const hook = this.hooks.shift();
    if (hook) await hook();
    this.writes.push([...ops]);
    return this.inner.transactWrite(ops);
  }
  batchGet(keys: readonly Key[]): Promise<Item[]> {
    return this.inner.batchGet(keys);
  }
  query(pk: string, skPrefix?: string): Promise<Item[]> {
    return this.inner.query(pk, skPrefix);
  }
}

function setup(over: Partial<ProjectRegistryDeps> = {}) {
  const mem = new InMemoryTransactStore();
  const store = new RacingStore(mem);
  const sleeps: number[] = [];
  let clock = T0;
  let n = 0;
  const deps: ProjectRegistryDeps = {
    store,
    now: () => clock,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
    random: () => 0.5,
    newId: () => pidOf(++n),
    ...over,
  };
  const reg = new ProjectRegistry(deps);
  // A second registry straight over the inner store: the "other request" in a race (no hooks).
  const other = new ProjectRegistry({ ...deps, store: mem });
  return { mem, store, reg, other, sleeps, advance: (ms: number) => (clock = new Date(clock.getTime() + ms)) };
}

function value<T>(r: RegistryResult<T>): T {
  if (!r.ok) throw new Error(`expected ok, got ${JSON.stringify(r.error)}`);
  return r.value;
}

function err<T>(r: RegistryResult<T>): RegistryError {
  if (r.ok) throw new Error("expected an error");
  return r.error;
}

const code = <T>(r: RegistryResult<T>): string => err(r).code;

/** Port of the v4 `violations` invariant check over a store snapshot. */
function violations(mem: InMemoryTransactStore): string[] {
  const items = mem.snapshot();
  const out: string[] = [];
  const at = (pk: string, sk: string) => items.find((i) => i.PK === pk && i.SK === sk);
  const metas = new Map(items.filter((i) => i.SK === "META" && !i.tombstone).map((i) => [i.PK.slice(8), i]));
  for (const [pid, m] of metas) {
    const mem_ = items.filter((i) => i.PK === `PROJECT#${pid}` && i.SK.startsWith("MEMBER#"));
    const owners = mem_.filter((i) => i.role === "owner");
    const members = mem_.filter((i) => i.role === "member");
    if (owners.length !== 1) out.push(`${pid}: ${owners.length} owners`);
    if (members.length > MAX_PROJECT_MEMBERS) out.push(`${pid}: ${members.length} members`);
    if (m.memberCount !== members.length) out.push(`${pid}: memberCount ${m.memberCount} != ${members.length}`);
    if (m.personal && (members.length || m.name !== "Personal" || m.status !== "active")) out.push(`${pid}: personal changed`);
    for (const i of mem_) {
      if ("expiresAt" in i || "ttl" in i) out.push(`${pid}: membership with expiry`);
      if (m.status === "active" && !at(`USER#${i.SK.slice(7)}`, `PROJECT#${pid}`)) out.push(`${pid}: ${i.SK} has no mirror`);
    }
  }
  for (const i of items) {
    if (i.PK.startsWith("USER#") && i.SK.startsWith("PROJECT#")) {
      const pid = i.SK.slice(8);
      const sub = i.PK.slice(5);
      const m = metas.get(pid);
      if (!m || m.status !== "active" || !at(`PROJECT#${pid}`, `MEMBER#${sub}`)) out.push(`orphan mirror ${sub}->${pid}`);
    }
    if (i.PK.startsWith("USER#") && i.SK.startsWith("NAME#")) {
      const m = metas.get(i.projectId as string);
      if (!m || m.status !== "active" || m.nameFold !== i.SK.slice(5)) out.push(`orphan name guard ${i.PK} ${i.SK}`);
    }
    if (i.SK === "COUNTERS") {
      const owner = i.PK.slice(5);
      const live = [...metas.values()].filter((m) => m.ownerSub === owner && !m.personal && m.status === "active").length;
      if (i.ownedCount !== live || live > MAX_OWNED_PROJECTS) out.push(`${owner}: ownedCount ${i.ownedCount} != ${live}`);
    }
  }
  return out;
}

async function seeded() {
  const s = setup();
  const p = value(await s.reg.create("alice", "Trailer", { ownerDisplay: "alice@example.com" }));
  return { ...s, pid: p.projectId };
}

// ------------------------------------------------------------------------------------------ create

describe("create", () => {
  it("writes META, owner MEMBER, mirror, name guard, and COUNTERS in one transaction", async () => {
    const { mem, store, reg } = setup();
    const p = value(await reg.create("alice", "  Trailer  ", { ownerDisplay: "alice@example.com" }));
    expect(p).toMatchObject({ projectId: pidOf(1), name: "Trailer", ownerSub: "alice", personal: false, memberCount: 0 });
    expect(store.writes).toHaveLength(1);
    expect(mem.get(metaKey(p.projectId))).toMatchObject({
      nameFold: "trailer",
      status: "active",
      collectionState: "absent",
      createdAt: T0.toISOString(),
    });
    expect(mem.get(memberKey(p.projectId, "alice"))).toEqual({
      ...memberKey(p.projectId, "alice"),
      role: "owner",
      display: "alice@example.com",
      addedAt: T0.toISOString(),
      addedBy: "alice",
    });
    expect(mem.get(mirrorKey("alice", p.projectId))).toMatchObject({ role: "owner" });
    expect(mem.get(nameGuardKey("alice", "trailer"))).toMatchObject({ projectId: p.projectId });
    expect(mem.get(countersKey("alice"))).toMatchObject({ ownedCount: 1 });
    expect(violations(mem)).toEqual([]);
  });

  it("rejects each name rule and a case-insensitive duplicate, writing nothing", async () => {
    const { mem, reg } = await seeded();
    const before = mem.snapshot();
    const rule = async (name: string) => {
      const r = await reg.create("alice", name);
      return r.ok ? null : r.error.code === "invalid_name" ? r.error.rule : r.error.code;
    };
    expect(await rule("   ")).toBe(NAME_RULES.empty);
    expect(await rule("x".repeat(65))).toBe(NAME_RULES.tooLong);
    expect(await rule("a\u0007b")).toBe(NAME_RULES.controlCharacter);
    expect(await rule("PERSONAL")).toBe(NAME_RULES.reserved);
    expect(await rule("tRaIlEr")).toBe(NAME_RULES.duplicate);
    expect(mem.snapshot()).toEqual(before);
  });

  it("allows the same name under a different Owner", async () => {
    const { reg } = await seeded();
    expect((await reg.create("bob", "Trailer")).ok).toBe(true);
  });

  it("enforces the 50-owned limit through COUNTERS; Personal is not counted", async () => {
    const { mem, reg } = setup();
    value(await reg.ensurePersonal("alice"));
    mem.seed({ ...countersKey("alice"), ownedCount: MAX_OWNED_PROJECTS - 1 });
    value(await reg.create("alice", "Last one"));
    expect(mem.get(countersKey("alice"))).toMatchObject({ ownedCount: MAX_OWNED_PROJECTS });
    const before = mem.snapshot();
    const r = await reg.create("alice", "One too many");
    expect(code(r)).toBe("owned_project_limit");
    expect(registryErrorStatus(err(r))).toBe(400);
    expect(mem.snapshot()).toEqual(before);
  });

  it("regenerates the id when it is held by a live Project or a tombstone", async () => {
    const ids = [pidOf(100), pidOf(200), pidOf(300)];
    const s = setup({ newId: () => ids.shift()! });
    s.mem.seed({ ...metaKey(pidOf(100)), projectId: pidOf(100), status: "active" });
    s.mem.seed(tomb(pidOf(200), T0.toISOString(), T0.toISOString()));
    const p = value(await s.reg.create("alice", "Trailer"));
    expect(p.projectId).toBe(pidOf(300));
    expect(s.sleeps).toEqual([]);
    expect(s.mem.get(metaKey(pidOf(200)))).toMatchObject({ tombstone: true });
  });

  it("returns id_generation_failed after 3 regenerations, writing nothing", async () => {
    const s = setup({ newId: () => pidOf(7) });
    s.mem.seed(tomb(pidOf(7), T0.toISOString(), T0.toISOString()));
    const before = s.mem.snapshot();
    const r = await s.reg.create("alice", "Trailer");
    expect(code(r)).toBe("id_generation_failed");
    expect(s.store.writes).toHaveLength(4);
    expect(s.mem.snapshot()).toEqual(before);
  });
});

// ---------------------------------------------------------------------------------- ensure Personal

describe("ensurePersonal", () => {
  it("creates the Personal_Project once and returns the same id afterwards", async () => {
    const { mem, reg } = setup();
    const a = value(await reg.ensurePersonal("alice"));
    expect(a).toMatchObject({ name: "Personal", nameFold: "personal", personal: true, memberCount: 0, collectionState: "absent" });
    expect(mem.get(personalKey("alice"))).toMatchObject({ projectId: a.projectId });
    expect(mem.get(countersKey("alice"))).toBeNull();
    expect(value(await reg.ensurePersonal("alice")).projectId).toBe(a.projectId);
    expect(violations(mem)).toEqual([]);
  });

  it("concurrent first requests all observe one Personal_Project", async () => {
    const { mem, reg } = setup();
    const results = await Promise.all(Array.from({ length: 8 }, () => reg.ensurePersonal("alice")));
    const ids = new Set(results.map((r) => value(r).projectId));
    expect(ids.size).toBe(1);
    expect(mem.snapshot().filter((i) => i.SK === "META" && i.personal)).toHaveLength(1);
  });

  it("a lost race (PERSONAL written between pre-read and commit) returns the winner", async () => {
    const { mem, store, reg, other } = setup();
    let winner = "";
    store.beforeNextWrite(async () => {
      winner = value(await other.ensurePersonal("alice")).projectId;
    });
    expect(value(await reg.ensurePersonal("alice")).projectId).toBe(winner);
    expect(mem.snapshot().filter((i) => i.SK === "META")).toHaveLength(1);
  });

  it("retries a TransactionConflict cancellation (v4: personal conflict was unhandled)", async () => {
    const { mem, reg, sleeps } = setup();
    mem.injectConflicts(1);
    const p = value(await reg.ensurePersonal("alice"));
    expect(mem.get(personalKey("alice"))).toMatchObject({ projectId: p.projectId });
    expect(sleeps).toEqual([25]);
  });
});

// ------------------------------------------------------------------------------------------- reads

describe("list and detail", () => {
  it("lists active Projects the Actor owns or is in, Personal first, then by name", async () => {
    const { reg, pid } = await seeded();
    value(await reg.ensurePersonal("alice"));
    value(await reg.create("alice", "archive"));
    const gone = value(await reg.create("alice", "Gone"));
    value(await reg.delete("alice", gone.projectId, "Gone"));
    const bobs = value(await reg.create("bob", "Bob's"));
    value(await reg.invite("bob", bobs.projectId, { sub: "alice", display: "alice@example.com" }));

    const list = await reg.list("alice");
    expect(list.map((p) => [p.name, p.role])).toEqual([
      ["Personal", "owner"],
      ["archive", "owner"],
      ["Bob's", "member"],
      ["Trailer", "owner"],
    ]);
    expect(list.find((p) => p.projectId === pid)).toMatchObject({ activeAssetCount: 0, personal: false });
    expect(await reg.list("carol")).toEqual([]);
  });

  it("detail shows the Owner and Members by display and the caller's role", async () => {
    const { reg, pid, advance } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob@example.com" }));
    advance(1000);
    value(await reg.invite("alice", pid, { sub: "carol", display: "carol" }));
    const d = value(await reg.detail("bob", pid));
    expect(d.role).toBe("member");
    expect(d.owner).toMatchObject({ sub: "alice", display: "alice@example.com", role: "owner" });
    expect(d.members.map((m) => m.display)).toEqual(["bob@example.com", "carol"]);
  });

  it("returns the one not-found for every denial class", async () => {
    const { mem, reg, pid } = await seeded();
    const gone = value(await reg.create("alice", "Gone"));
    value(await reg.delete("alice", gone.projectId, "Gone"));
    mem.seed(tomb(pidOf(999), T0.toISOString(), T0.toISOString()));
    const denials = [
      await reg.detail("alice", "not-an-id"),
      await reg.detail("alice", pidOf(4242)),
      await reg.detail("alice", gone.projectId),
      await reg.detail("alice", pidOf(999)),
      await reg.detail("mallory", pid),
    ];
    for (const r of denials) expect(JSON.stringify(r)).toBe(JSON.stringify({ ok: false, error: PROJECT_NOT_FOUND }));
    // Every write op shares the same not-found, and none of them writes.
    const before = mem.snapshot();
    for (const r of [
      await reg.rename("mallory", pid, "X"),
      await reg.delete("mallory", pid, "Trailer"),
      await reg.invite("mallory", pid, { sub: "eve", display: "eve" }),
      await reg.remove("mallory", pid, "alice"),
      await reg.leave("mallory", pid),
      await reg.leave("alice", "PROJECT#x"),
    ]) {
      expect(r).toEqual({ ok: false, error: PROJECT_NOT_FOUND });
    }
    expect(mem.snapshot()).toEqual(before);
  });
});

// ------------------------------------------------------------------------------------------ rename

describe("rename", () => {
  it("moves the name guard and keeps the id", async () => {
    const { mem, reg, pid } = await seeded();
    const p = value(await reg.rename("alice", pid, " Cut B "));
    expect(p).toMatchObject({ projectId: pid, name: "Cut B", nameFold: "cut b" });
    expect(mem.get(nameGuardKey("alice", "trailer"))).toBeNull();
    expect(mem.get(nameGuardKey("alice", "cut b"))).toMatchObject({ projectId: pid });
    expect(violations(mem)).toEqual([]);
  });

  it("case-only rename updates META only and keeps the guard (v4: was a ValidationException)", async () => {
    const { mem, store, reg, pid } = await seeded();
    const guard = mem.get(nameGuardKey("alice", "trailer"));
    value(await reg.rename("alice", pid, "TRAILER"));
    expect(store.writes.at(-1)!.map((o) => o.kind)).toEqual(["update"]);
    expect(mem.get(metaKey(pid))).toMatchObject({ name: "TRAILER", nameFold: "trailer" });
    expect(mem.get(nameGuardKey("alice", "trailer"))).toEqual(guard);
    expect(violations(mem)).toEqual([]);
  });

  it("refuses Members, the Personal_Project, and a duplicate name", async () => {
    const { mem, reg, pid } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    value(await reg.create("alice", "Promo"));
    const personal = value(await reg.ensurePersonal("alice"));
    const before = mem.snapshot();
    const owner = await reg.rename("bob", pid, "Mine");
    expect(owner).toMatchObject({ ok: false, error: { code: "owner_only", action: "rename" } });
    expect(code(await reg.rename("alice", personal.projectId, "Private"))).toBe("personal_project");
    expect(await reg.rename("alice", pid, "PROMO")).toMatchObject({ ok: false, error: { rule: NAME_RULES.duplicate } });
    expect(mem.snapshot()).toEqual(before);
  });

  it("v4 concurrent renames: a racing rename makes the pinned nameFold stale, the retry leaves no orphan guard", async () => {
    const { mem, store, reg, other, pid, sleeps } = await seeded();
    store.beforeNextWrite(async () => {
      value(await other.rename("alice", pid, "Cut C"));
    });
    const p = value(await reg.rename("alice", pid, "Cut B"));
    expect(p.name).toBe("Cut B");
    expect(sleeps).toEqual([25]);
    expect(mem.get(nameGuardKey("alice", "cut c"))).toBeNull();
    expect(violations(mem)).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------------ delete

describe("delete", () => {
  it("marks deleted and removes every mirror, the guard, and one owned count in one transaction", async () => {
    const { mem, store, reg, pid, advance } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    advance(60_000);
    const writesBefore = store.writes.length;
    value(await reg.delete("alice", pid, "Trailer"));
    expect(store.writes.length - writesBefore).toBe(1);
    expect(mem.get(metaKey(pid))).toMatchObject({ status: "deleted", deletedAt: new Date(T0.getTime() + 60_000).toISOString() });
    expect(mem.get(mirrorKey("alice", pid))).toBeNull();
    expect(mem.get(mirrorKey("bob", pid))).toBeNull();
    expect(mem.get(nameGuardKey("alice", "trailer"))).toBeNull();
    expect(mem.get(countersKey("alice"))).toMatchObject({ ownedCount: 0 });
    expect(await reg.list("bob")).toEqual([]);
    expect(await reg.detail("alice", pid)).toEqual({ ok: false, error: PROJECT_NOT_FOUND });
    expect(violations(mem)).toEqual([]);
  });

  it("requires the exact name, Owner role, and refuses the Personal_Project", async () => {
    const { mem, reg, pid } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    const personal = value(await reg.ensurePersonal("alice"));
    const before = mem.snapshot();
    expect(code(await reg.delete("alice", pid, "trailer"))).toBe("confirm_name_mismatch");
    expect(code(await reg.delete("bob", pid, "Trailer"))).toBe("owner_only");
    expect(code(await reg.delete("alice", personal.projectId, "Personal"))).toBe("personal_project");
    expect(mem.snapshot()).toEqual(before);
  });

  it("v4 delete-vs-rename: the rename that committed after the pre-read leaves no orphan guard", async () => {
    const { mem, store, reg, other, pid } = await seeded();
    store.beforeNextWrite(async () => {
      value(await other.rename("alice", pid, "Cut B"));
    });
    // The re-read sees the new name, which no longer matches the confirmation.
    expect(code(await reg.delete("alice", pid, "Trailer"))).toBe("confirm_name_mismatch");
    expect(violations(mem)).toEqual([]);

  });

  it("a racing case-only rename keeps the pinned nameFold, so the delete commits and orphans nothing", async () => {
    const { mem, store, reg, other, pid } = await seeded();
    store.beforeNextWrite(async () => {
      value(await other.rename("alice", pid, "TRAILER"));
    });
    value(await reg.delete("alice", pid, "Trailer"));
    expect(mem.get(nameGuardKey("alice", "trailer"))).toBeNull();
    expect(violations(mem)).toEqual([]);
  });

  it("v4 delete-vs-invite: the invite that committed after the pre-read leaves no orphan mirror", async () => {
    const { mem, store, reg, other, pid, sleeps } = await seeded();
    store.beforeNextWrite(async () => {
      value(await other.invite("alice", pid, { sub: "bob", display: "bob" }));
    });
    value(await reg.delete("alice", pid, "Trailer"));
    expect(sleeps).toEqual([25]);
    expect(mem.get(mirrorKey("bob", pid))).toBeNull();
    expect(violations(mem)).toEqual([]);
  });
});

// -------------------------------------------------------------------------------------- membership

describe("invite, remove, leave", () => {
  it("invite adds a Member with display, mirror, and memberCount; no expiry attribute", async () => {
    const { mem, reg, pid, advance } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob@example.com" }));
    expect(mem.get(memberKey(pid, "bob"))).toEqual({
      ...memberKey(pid, "bob"),
      role: "member",
      display: "bob@example.com",
      addedAt: T0.toISOString(),
      addedBy: "alice",
    });
    expect(mem.get(mirrorKey("bob", pid))).toMatchObject({ role: "member" });
    expect(mem.get(metaKey(pid))).toMatchObject({ memberCount: 1 });
    // Time alone ends no membership (Req 1.28).
    advance(10 * 365 * 24 * 3600 * 1000);
    expect(value(await reg.detail("bob", pid)).role).toBe("member");
    expect(violations(mem)).toEqual([]);
  });

  it("invite refuses the Owner, an existing Member, a Member caller, Personal, and the 26th Member", async () => {
    const { mem, reg, pid } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    const personal = value(await reg.ensurePersonal("alice"));
    expect(code(await reg.invite("alice", pid, { sub: "alice", display: "alice" }))).toBe("already_in_project");
    expect(code(await reg.invite("alice", pid, { sub: "bob", display: "bob" }))).toBe("already_in_project");
    expect(code(await reg.invite("bob", pid, { sub: "carol", display: "carol" }))).toBe("owner_only");
    expect(code(await reg.invite("alice", personal.projectId, { sub: "bob", display: "bob" }))).toBe("personal_project");
    for (let i = 2; i <= MAX_PROJECT_MEMBERS; i++) value(await reg.invite("alice", pid, { sub: `u${i}`, display: `u${i}` }));
    const before = mem.snapshot();
    const r = await reg.invite("alice", pid, { sub: "one-too-many", display: "x" });
    expect(r).toMatchObject({ ok: false, error: { code: "member_limit", limit: 25 } });
    expect(mem.snapshot()).toEqual(before);
    expect(violations(mem)).toEqual([]);
  });

  it("remove and leave end the membership; the Owner cannot leave or be removed", async () => {
    const { mem, reg, pid } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    value(await reg.invite("alice", pid, { sub: "carol", display: "carol" }));
    const personal = value(await reg.ensurePersonal("alice"));

    expect(code(await reg.remove("bob", pid, "carol"))).toBe("owner_only");
    expect(code(await reg.remove("alice", pid, "alice"))).toBe("owner_cannot_leave");
    expect(code(await reg.leave("alice", pid))).toBe("owner_cannot_leave");
    expect(code(await reg.leave("alice", personal.projectId))).toBe("personal_project");
    expect(code(await reg.remove("alice", pid, "dave"))).toBe("member_not_found");

    value(await reg.remove("alice", pid, "bob"));
    value(await reg.leave("carol", pid));
    expect(mem.get(memberKey(pid, "bob"))).toBeNull();
    expect(mem.get(mirrorKey("carol", pid))).toBeNull();
    expect(mem.get(metaKey(pid))).toMatchObject({ memberCount: 0 });
    expect(await reg.detail("bob", pid)).toEqual({ ok: false, error: PROJECT_NOT_FOUND });
    expect(code(await reg.leave("carol", pid))).toBe("project_not_found");
    expect(violations(mem)).toEqual([]);
  });

  it("a racing remove of the same Member maps to member_not_found without another write", async () => {
    const { mem, store, reg, other, pid, sleeps } = await seeded();
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    store.beforeNextWrite(async () => {
      value(await other.remove("alice", pid, "bob"));
    });
    const writes = store.writes.length;
    expect(code(await reg.remove("alice", pid, "bob"))).toBe("member_not_found");
    expect(store.writes.length - writes).toBe(1);
    expect(sleeps).toEqual([]);
    expect(violations(mem)).toEqual([]);
  });
});

// -------------------------------------------------------------------------------------- retry policy

describe("retry policy", () => {
  it("retries TransactionConflict with full-jitter backoff (base 50 ms) and succeeds within 3 retries", async () => {
    const { mem, reg, pid, sleeps } = await seeded();
    mem.injectConflicts(3);
    value(await reg.invite("alice", pid, { sub: "bob", display: "bob" }));
    expect(sleeps).toEqual([25, 50, 100]);
  });

  it("returns registry_busy (retryable, 503) after 3 retries and changes no item", async () => {
    const { mem, reg, pid, sleeps } = await seeded();
    const before = mem.snapshot();
    mem.injectConflicts(4);
    const r = await reg.rename("alice", pid, "Cut B");
    expect(r).toMatchObject({ ok: false, error: { code: "registry_busy", retryable: true } });
    expect(registryErrorStatus(err(r))).toBe(503);
    expect(sleeps).toHaveLength(3);
    expect(mem.snapshot()).toEqual(before);
  });

  it("jitter stays within [0, base * 2^(retry-1))", async () => {
    const s = setup({ random: () => 0.999 });
    const q = value(await s.reg.create("alice", "Trailer"));
    s.mem.injectConflicts(4);
    expect(code(await s.reg.invite("alice", q.projectId, { sub: "bob", display: "bob" }))).toBe("registry_busy");
    expect(s.sleeps).toHaveLength(3);
    s.sleeps.forEach((ms, i) => {
      expect(ms).toBeGreaterThanOrEqual(0);
      expect(ms).toBeLessThan(50 * 2 ** i);
    });
  });
});
