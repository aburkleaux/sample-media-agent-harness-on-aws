// InMemoryTransactStore semantics (task 6.1). The registry property tests (6.4, 6.5) trust this store to
// behave like DynamoDB's TransactWriteItems; each case here pins one behavior they depend on.
import { describe, expect, it } from "vitest";
import { InMemoryTransactStore } from "./inMemoryStore";
import { countersKey, memberKey, metaKey, mirrorKey, nameGuardKey } from "./schema";
import { StoreValidationError, TransactionCancelledError, cond } from "./store";
import type { TransactOp } from "./store";

const PID = "p" + "a".repeat(32);
const PID2 = "p" + "b".repeat(32);

const meta = (over: Record<string, unknown> = {}) => ({
  ...metaKey(PID),
  projectId: PID,
  name: "Trailer",
  nameFold: "trailer",
  ownerSub: "alice",
  personal: false,
  status: "active",
  memberCount: 0,
  ...over,
});

async function cancelled(p: Promise<unknown>): Promise<TransactionCancelledError> {
  const e = await p.then(
    () => null,
    (err: unknown) => err,
  );
  expect(e).toBeInstanceOf(TransactionCancelledError);
  return e as TransactionCancelledError;
}

describe("InMemoryTransactStore transactWrite", () => {
  it("applies every op of a transaction whose conditions hold", async () => {
    const s = new InMemoryTransactStore();
    await s.transactWrite([
      { kind: "put", item: meta(), condition: cond.notExists("PK") },
      { kind: "put", item: { ...nameGuardKey("alice", "trailer"), projectId: PID }, condition: cond.notExists("PK") },
      { kind: "update", key: countersKey("alice"), add: { ownedCount: 1 } },
    ]);
    expect(s.get(metaKey(PID))?.name).toBe("Trailer");
    expect(s.get(countersKey("alice"))).toEqual({ ...countersKey("alice"), ownedCount: 1 });
    expect(s.committed).toBe(1);
  });

  it("is all-or-nothing and reports which condition failed", async () => {
    const s = new InMemoryTransactStore();
    s.seed({ ...nameGuardKey("alice", "trailer"), projectId: PID2 });
    const before = s.snapshot();
    const e = await cancelled(
      s.transactWrite([
        { kind: "put", item: meta(), condition: cond.notExists("PK") },
        { kind: "put", item: { ...nameGuardKey("alice", "trailer"), projectId: PID }, condition: cond.notExists("PK") },
        { kind: "update", key: countersKey("alice"), add: { ownedCount: 1 } },
      ]),
    );
    expect(e.reasons.map((r) => r.code)).toEqual(["None", "ConditionalCheckFailed", "None"]);
    expect(e.failedConditions).toEqual([1]);
    expect(e.hasConflict).toBe(false);
    expect(s.snapshot()).toEqual(before);
    expect(s.committed).toBe(0);
  });

  it("evaluates every condition against the pre-transaction state", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta({ memberCount: 1 }));
    // The update would make memberCount 2, but the check on the same item cannot see that; a second op
    // on another item conditioned on META is evaluated against the stored META only.
    await s.transactWrite([
      { kind: "update", key: metaKey(PID), add: { memberCount: 1 }, condition: cond.eq("memberCount", 1) },
      { kind: "put", item: { ...memberKey(PID, "bob"), role: "member" }, condition: cond.notExists("PK") },
    ]);
    expect(s.get(metaKey(PID))?.memberCount).toBe(2);
  });

  it("rejects a transaction that names one item twice, writing nothing", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta(), { ...nameGuardKey("alice", "trailer"), projectId: PID });
    const before = s.snapshot();
    // The case-only rename defect of the v4 model: delete old guard + put new guard on the same fold.
    const ops: TransactOp[] = [
      { kind: "update", key: metaKey(PID), set: { name: "TRAILER" } },
      { kind: "delete", key: nameGuardKey("alice", "trailer") },
      { kind: "put", item: { ...nameGuardKey("alice", "trailer"), projectId: PID }, condition: cond.notExists("PK") },
    ];
    await expect(s.transactWrite(ops)).rejects.toThrow(StoreValidationError);
    expect(s.snapshot()).toEqual(before);
  });

  it("rejects malformed requests as DynamoDB does", async () => {
    const s = new InMemoryTransactStore();
    const tooMany: TransactOp[] = Array.from({ length: 101 }, (_, i) => ({
      kind: "put",
      item: { PK: "X", SK: `S${i}` },
    }));
    await expect(s.transactWrite([])).rejects.toThrow(StoreValidationError);
    await expect(s.transactWrite(tooMany)).rejects.toThrow(StoreValidationError);
    await expect(s.transactWrite([{ kind: "update", key: metaKey(PID), set: { SK: "OTHER" } }])).rejects.toThrow(
      /part of the key/,
    );
    await expect(
      s.transactWrite([{ kind: "update", key: metaKey(PID), set: { n: 1 }, add: { n: 1 } }]),
    ).rejects.toThrow(/overlap/);
    await expect(s.transactWrite([{ kind: "update", key: metaKey(PID) }])).rejects.toThrow(StoreValidationError);
    expect(s.snapshot()).toEqual([]);
  });

  it("rejects ADD to a non-number attribute without writing any op", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta());
    const before = s.snapshot();
    await expect(
      s.transactWrite([
        { kind: "put", item: { ...memberKey(PID, "bob"), role: "member" } },
        { kind: "update", key: metaKey(PID), add: { name: 1 } },
      ]),
    ).rejects.toThrow(StoreValidationError);
    expect(s.snapshot()).toEqual(before);
  });

  it("supports delete, REMOVE, and condition checks", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta({ deletedAt: "x" }), { ...memberKey(PID, "bob"), role: "member" }, { ...mirrorKey("bob", PID), role: "member" });
    await s.transactWrite([
      { kind: "delete", key: memberKey(PID, "bob"), condition: cond.eq("role", "member") },
      { kind: "delete", key: mirrorKey("bob", PID) },
      { kind: "update", key: metaKey(PID), remove: ["deletedAt"], condition: cond.eq("status", "active") },
      { kind: "conditionCheck", key: countersKey("alice"), condition: cond.notExists("PK") },
    ]);
    expect(s.get(memberKey(PID, "bob"))).toBeNull();
    expect(s.get(mirrorKey("bob", PID))).toBeNull();
    expect(s.get(metaKey(PID))).not.toHaveProperty("deletedAt");
    expect(s.get(countersKey("alice"))).toBeNull(); // a condition check writes nothing
  });
});

describe("InMemoryTransactStore condition semantics", () => {
  const ownedLimit = cond.or(cond.notExists("ownedCount"), cond.lt("ownedCount", 2));
  const addOwned = (s: InMemoryTransactStore) =>
    s.transactWrite([{ kind: "update", key: countersKey("alice"), add: { ownedCount: 1 }, condition: ownedLimit }]);

  it("treats an absent item as having no attributes, and ADD on it creates the item from 0", async () => {
    const s = new InMemoryTransactStore();
    await addOwned(s);
    await addOwned(s);
    expect(s.get(countersKey("alice"))?.ownedCount).toBe(2);
    const e = await cancelled(addOwned(s));
    expect(e.failedConditions).toEqual([0]);
    expect(s.get(countersKey("alice"))?.ownedCount).toBe(2);
  });

  it("makes a comparison against an absent attribute or another type false", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta({ memberCount: "0" }));
    for (const c of [
      cond.lt("memberCount", 25), // string vs number
      cond.eq("personal", "false"), // boolean vs string
      cond.eq("missing", "x"),
      cond.exists("missing"),
    ]) {
      const e = await cancelled(s.transactWrite([{ kind: "conditionCheck", key: metaKey(PID), condition: c }]));
      expect(e.reasons[0]!.code).toBe("ConditionalCheckFailed");
    }
    await s.transactWrite([
      {
        kind: "conditionCheck",
        key: metaKey(PID),
        condition: cond.and(cond.eq("personal", false), cond.eq("ownerSub", "alice"), cond.notExists("missing")),
      },
    ]);
  });

  it("fails every condition but notExists on an absent item", async () => {
    const s = new InMemoryTransactStore();
    const e = await cancelled(
      s.transactWrite([{ kind: "update", key: metaKey(PID), add: { memberCount: 1 }, condition: cond.eq("status", "active") }]),
    );
    expect(e.failedConditions).toEqual([0]);
    expect(s.get(metaKey(PID))).toBeNull();
  });

  it("orders strings by UTF-8 bytes", async () => {
    const s = new InMemoryTransactStore();
    // U+FF5E is below U+1F600 in UTF-8 bytes, though its UTF-16 code unit sorts above the surrogate.
    s.seed({ PK: "X", SK: "S", v: "\uFF5E" });
    await s.transactWrite([{ kind: "conditionCheck", key: { PK: "X", SK: "S" }, condition: cond.lt("v", "\u{1F600}") }]);
  });
});

describe("InMemoryTransactStore conflict injection", () => {
  it("cancels the next transactions with TransactionConflict and writes nothing", async () => {
    const s = new InMemoryTransactStore();
    s.injectConflicts(2);
    const op: TransactOp = { kind: "put", item: meta(), condition: cond.notExists("PK") };
    for (let i = 0; i < 2; i++) {
      const e = await cancelled(s.transactWrite([op, { kind: "update", key: countersKey("alice"), add: { ownedCount: 1 } }]));
      expect(e.reasons.map((r) => r.code)).toEqual(["TransactionConflict", "TransactionConflict"]);
      expect(e.hasConflict).toBe(true);
    }
    expect(s.snapshot()).toEqual([]);
    await s.transactWrite([op]);
    expect(s.get(metaKey(PID))).not.toBeNull();
  });

  it("validates before injecting, so a double-named item is still a validation error", async () => {
    const s = new InMemoryTransactStore();
    s.injectConflicts(1);
    const put: TransactOp = { kind: "put", item: meta() };
    await expect(s.transactWrite([put, put])).rejects.toThrow(StoreValidationError);
    await expect(s.transactWrite([put])).rejects.toThrow(TransactionCancelledError); // still pending
  });

  it("lets an oracle mark only some ops", async () => {
    const s = new InMemoryTransactStore({ conflictOracle: (ops) => (ops.length > 1 ? [1] : false) });
    const e = await cancelled(
      s.transactWrite([
        { kind: "put", item: meta() },
        { kind: "put", item: { ...memberKey(PID, "alice"), role: "owner" } },
      ]),
    );
    expect(e.reasons.map((r) => r.code)).toEqual(["None", "TransactionConflict"]);
    await s.transactWrite([{ kind: "put", item: meta() }]);
    expect(s.committed).toBe(1);
  });
});

describe("InMemoryTransactStore reads", () => {
  it("queries one partition by SK prefix in SK order", async () => {
    const s = new InMemoryTransactStore();
    s.seed(
      meta(),
      { ...memberKey(PID, "carol"), role: "member" },
      { ...memberKey(PID, "alice"), role: "owner" },
      { ...memberKey(PID2, "bob"), role: "owner" },
    );
    const rows = await s.query(metaKey(PID).PK, "MEMBER#");
    expect(rows.map((r) => r.SK)).toEqual(["MEMBER#alice", "MEMBER#carol"]);
    expect((await s.query(metaKey(PID).PK)).map((r) => r.SK)).toEqual(["MEMBER#alice", "MEMBER#carol", "META"]);
  });

  it("batch-gets present items, omits absent ones, and rejects duplicate keys", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta());
    const got = await s.batchGet([metaKey(PID), memberKey(PID, "alice")]);
    expect(got).toEqual([meta()]);
    await expect(s.batchGet([metaKey(PID), metaKey(PID)])).rejects.toThrow(StoreValidationError);
  });

  it("returns copies, so a caller cannot mutate stored state", async () => {
    const s = new InMemoryTransactStore();
    s.seed(meta());
    const [row] = await s.batchGet([metaKey(PID)]);
    row!.status = "deleted";
    (await s.query(metaKey(PID).PK))[0]!.status = "deleted";
    expect(s.get(metaKey(PID))?.status).toBe("active");
  });
});
