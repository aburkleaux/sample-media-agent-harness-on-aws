// checkAccess / ensurePersonalProject (task 6.3) over InMemoryTransactStore, plus the env-selected store
// singleton. The uniform-not-found property over all five denial classes is task 6.6.
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ACCESS_DENIED,
  PROJECT_NOT_FOUND_BODY,
  ProjectRegistryError,
  ProjectsDisabledError,
  checkAccess,
  ensurePersonalProject,
  isProjectId,
  projectRegistry,
  projectsEnabled,
  projectsStoreFromEnv,
  resetProjectsStore,
  setProjectsStore,
} from "./access";
import { isProjectId as idsIsProjectId } from "./ids";
import { InMemoryTransactStore } from "./inMemoryStore";
import { ProjectRegistry } from "./registry";
import { metaKey, personalKey, tombstoneItem } from "./schema";
import { DynamoTransactStore } from "./store";
import type { Item, Key, TransactOp, TransactStore } from "./store";

const pidOf = (n: number) => "p" + n.toString(16).padStart(32, "0");

/** Counts reads and writes, and can run a racing op before the next write. */
class CountingStore implements TransactStore {
  reads = 0;
  writes = 0;
  private hooks: (() => Promise<void>)[] = [];
  constructor(readonly inner: InMemoryTransactStore) {}
  beforeNextWrite(fn: () => Promise<void>): void {
    this.hooks.push(fn);
  }
  async transactWrite(ops: readonly TransactOp[]): Promise<void> {
    const hook = this.hooks.shift();
    if (hook) await hook();
    this.writes++;
    return this.inner.transactWrite(ops);
  }
  batchGet(keys: readonly Key[]): Promise<Item[]> {
    this.reads++;
    return this.inner.batchGet(keys);
  }
  query(pk: string, skPrefix?: string): Promise<Item[]> {
    this.reads++;
    return this.inner.query(pk, skPrefix);
  }
}

function setup() {
  const mem = new InMemoryTransactStore();
  const store = new CountingStore(mem);
  let n = 0;
  setProjectsStore(store, { sleep: async () => {}, random: () => 0.5, newId: () => pidOf(++n) });
  return { mem, store };
}

afterEach(() => {
  resetProjectsStore();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("store selection", () => {
  it("reports disabled, not an import-time throw, when PROJECTS_TABLE is unset", async () => {
    vi.stubEnv("PROJECTS_TABLE", "");
    vi.stubEnv("PROJECTS_STORE", "");
    resetProjectsStore();
    expect(projectsEnabled()).toBe(false);
    await expect(checkAccess("alice", pidOf(1))).rejects.toBeInstanceOf(ProjectsDisabledError);
    await expect(ensurePersonalProject("alice")).rejects.toBeInstanceOf(ProjectsDisabledError);
    expect(() => projectRegistry()).toThrow(ProjectsDisabledError);
  });

  it("selects DynamoDB from PROJECTS_TABLE", () => {
    expect(projectsStoreFromEnv({ PROJECTS_TABLE: "t" })).toBeInstanceOf(DynamoTransactStore);
    expect(projectsStoreFromEnv({})).toBeNull();
  });

  it("selects the dev in-memory store with PROJECTS_STORE=memory, but never under NODE_ENV=production", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(projectsStoreFromEnv({ PROJECTS_STORE: "memory" })).toBeInstanceOf(InMemoryTransactStore);
    expect(projectsStoreFromEnv({ PROJECTS_STORE: "memory", NODE_ENV: "production" })).toBeNull();
    expect(projectsStoreFromEnv({ PROJECTS_STORE: "memory", NODE_ENV: "production", PROJECTS_TABLE: "t" })).toBeInstanceOf(
      DynamoTransactStore,
    );
  });

  it("keeps one in-memory registry for the process", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubEnv("PROJECTS_STORE", "memory");
    vi.stubEnv("NODE_ENV", "development");
    resetProjectsStore();
    expect(projectsEnabled()).toBe(true);
    const a = await ensurePersonalProject("alice");
    const b = await ensurePersonalProject("alice");
    expect(b.projectId).toBe(a.projectId);
  });

  it("setProjectsStore(null) disables Projects", () => {
    setProjectsStore(null);
    expect(projectsEnabled()).toBe(false);
  });

  it("re-exports isProjectId from ids.ts", () => {
    expect(isProjectId).toBe(idsIsProjectId);
  });
});

describe("checkAccess", () => {
  async function shared() {
    const ctx = setup();
    const reg = projectRegistry();
    const created = await reg.create("alice", "Launch");
    if (!created.ok) throw new Error(JSON.stringify(created.error));
    const pid = created.value.projectId;
    const invited = await reg.invite("alice", pid, { sub: "bob", display: "bob@example.com" });
    if (!invited.ok) throw new Error(JSON.stringify(invited.error));
    return { ...ctx, pid };
  }

  it("grants the Owner and a Member their roles", async () => {
    const { pid } = await shared();
    const owner = await checkAccess("alice", pid);
    const member = await checkAccess("bob", pid);
    expect(owner).toMatchObject({ ok: true, role: "owner", project: { projectId: pid, name: "Launch", status: "active" } });
    expect(member).toMatchObject({ ok: true, role: "member", project: { projectId: pid } });
  });

  it("refuses a malformed id before any read", async () => {
    const { store } = await shared();
    const reads = store.reads;
    for (const bad of ["", "default", "P" + "0".repeat(32), pidOf(1) + "0", "../p1"]) {
      expect(await checkAccess("alice", bad)).toBe(ACCESS_DENIED);
    }
    expect(store.reads).toBe(reads);
  });

  it("returns the one denial for never created, deleted, tombstoned, and not a member, writing nothing", async () => {
    const { mem, store, pid } = await shared();
    const reg = projectRegistry();
    const gone = await reg.create("alice", "Old");
    if (!gone.ok) throw new Error("create failed");
    const deleted = await reg.delete("alice", gone.value.projectId, "Old");
    expect(deleted.ok).toBe(true);
    const tombPid = pidOf(999);
    mem.seed({ ...tombstoneItem(tombPid, "2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z") });

    const before = mem.snapshot();
    const writes = store.writes;
    const denials = [
      await checkAccess("alice", pidOf(500)), // never created
      await checkAccess("alice", gone.value.projectId), // deleted
      await checkAccess("alice", tombPid), // tombstoned
      await checkAccess("carol", pid), // not a member
    ];
    for (const d of denials) {
      expect(d).toBe(ACCESS_DENIED);
      expect(JSON.stringify(d.ok ? null : { error: d.error })).toBe(
        '{"error":{"code":"project_not_found","message":"Project not found"}}',
      );
    }
    expect(JSON.stringify(PROJECT_NOT_FOUND_BODY)).toBe('{"error":{"code":"project_not_found","message":"Project not found"}}');
    expect(store.writes).toBe(writes);
    expect(mem.snapshot()).toEqual(before);
  });

  it("denies a Member who left", async () => {
    const { pid } = await shared();
    expect((await projectRegistry().leave("bob", pid)).ok).toBe(true);
    expect(await checkAccess("bob", pid)).toBe(ACCESS_DENIED);
  });
});

describe("ensurePersonalProject", () => {
  it("creates the Personal_Project once and returns it thereafter", async () => {
    const { mem } = setup();
    const first = await ensurePersonalProject("alice");
    expect(first).toMatchObject({ name: "Personal", personal: true, ownerSub: "alice", status: "active" });
    const again = await ensurePersonalProject("alice");
    expect(again.projectId).toBe(first.projectId);
    expect(mem.get(personalKey("alice"))).toMatchObject({ projectId: first.projectId });
    expect(await checkAccess("alice", first.projectId)).toMatchObject({ ok: true, role: "owner" });
  });

  it("concurrent first calls all return one id", async () => {
    const { mem } = setup();
    const got = await Promise.all(Array.from({ length: 10 }, () => ensurePersonalProject("alice")));
    expect(new Set(got.map((m) => m.projectId)).size).toBe(1);
    const metas = mem.snapshot().filter((i) => i.SK === "META" && i.ownerSub === "alice");
    expect(metas).toHaveLength(1);
  });

  it("returns the winner when another request takes PERSONAL between the pre-read and the commit", async () => {
    const { mem, store } = setup();
    let winner = "";
    store.beforeNextWrite(async () => {
      // Straight to the inner store: the racing request commits first.
      const r = await new ProjectRegistry({ store: mem, newId: () => pidOf(77) }).ensurePersonal("alice");
      if (!r.ok) throw new Error("racer failed");
      winner = r.value.projectId;
    });
    const got = await ensurePersonalProject("alice");
    expect(winner).toBe(pidOf(77));
    expect(got.projectId).toBe(winner);
    expect(mem.get(metaKey(pidOf(1)))).toBeNull(); // the loser's META never landed
  });

  it("retries a TransactionConflict and still returns the one id", async () => {
    const { mem } = setup();
    mem.injectConflicts(2);
    const got = await ensurePersonalProject("alice");
    expect(mem.get(personalKey("alice"))).toMatchObject({ projectId: got.projectId });
  });

  it("throws a retryable registry_busy, changing nothing, when conflicts outlast the retries", async () => {
    const { mem } = setup();
    mem.injectConflicts(10);
    const err = await ensurePersonalProject("alice").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ProjectRegistryError);
    expect((err as ProjectRegistryError).status).toBe(503);
    expect((err as ProjectRegistryError).error).toMatchObject({ code: "registry_busy", retryable: true });
    expect(mem.snapshot()).toEqual([]);
  });
});
