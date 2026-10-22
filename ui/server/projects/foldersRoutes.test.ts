// Folders over the proxy routes (design D20): the uploads route threads `folder` into the object's signed
// metadata (and the grant), register passes an explicit folder, the asset views carry `folder`, `?q=`
// matches the folder path, and `POST …/assets/duplicates` finds already-uploaded files in one GSI pass.
import { Readable } from "node:stream";
import { S3Client } from "@aws-sdk/client-s3";
import { describe, expect, it } from "vitest";
import { ACCESS_DENIED } from "./access";
import type { AccessResult } from "./access";
import type { AssetServiceCall, AssetServiceReply } from "./assetServiceClient";
import { InMemoryTransactStore } from "./inMemoryStore";
import { DUPLICATES_MAX_FILES, dispatchAssetRoute } from "./ingestionRoutes";
import { ASSET_LIST_PK_ATTR, ASSET_LIST_SK_ATTR, assetKey, projectPk } from "./schema";
import type { Item } from "./store";
import { dispatchIngestionWriteRoute, s3UploadOps } from "./uploadRoutes";
import type { IngestionWriteDeps, UploadS3 } from "./uploadRoutes";

const PID = "p0123456789abcdef0123456789abcdef";
const ACTOR = "sub-alice";
const BUCKET = "ingest-bucket";
const MIB = 1024 * 1024;

const allow = async () => ({ ok: true } as AccessResult);
const deny = async () => ACCESS_DENIED;

function makeReq(method: string, body?: unknown, url = "/"): any {
  const payload = body === undefined ? [] : [Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8")];
  const req: any = Readable.from(payload);
  req.method = method;
  req.url = url;
  return req;
}

function makeRes() {
  let resolve!: (c: { status?: number; body: any }) => void;
  const done = new Promise<{ status?: number; body: any }>((r) => (resolve = r));
  const res: any = {
    headersSent: false,
    writeHead(status: number) {
      this._status = status;
      this.headersSent = true;
    },
    end(body?: string) {
      resolve({ status: this._status, body: body ? JSON.parse(body) : undefined });
    },
  };
  return { res, done };
}

class RecordingS3 implements Partial<UploadS3> {
  puts: unknown[][] = [];
  creates: unknown[][] = [];
  async presignPut(...args: unknown[]) {
    this.puts.push(args);
    return "https://s3.example/put";
  }
  async createMultipartUpload(...args: unknown[]) {
    this.creates.push(args);
    return "up-1";
  }
  async presignUploadPart() {
    return "https://s3.example/part";
  }
}

let seq = 0;
async function write(parts: string[], body: unknown, deps: IngestionWriteDeps) {
  const { res, done } = makeRes();
  await dispatchIngestionWriteRoute(makeReq("POST", body), res, { actor: ACTOR, pid: PID, parts, method: "POST", userToken: "jwt" }, {
    ingestBucket: BUCKET, env: {}, newUuid: () => `00000000-0000-4000-8000-${String(++seq).padStart(12, "0")}`, ...deps,
  });
  return done;
}

async function read(method: string, parts: string[], store: unknown, opts: { body?: unknown; query?: string; checkAccess?: any } = {}) {
  const { res, done } = makeRes();
  const handled = await dispatchAssetRoute(makeReq(method, opts.body, `/${parts.join("/")}${opts.query ?? ""}`), res,
    { actor: ACTOR, pid: PID, parts, method }, { checkAccess: opts.checkAccess ?? allow, store: store as any });
  return handled ? done : { status: undefined, body: undefined };
}

let rowSeq = 0;
function row(displayName: string, over: Partial<Item> = {}): Item {
  const n = ++rowSeq;
  const id = `video:00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
  const registeredAt = `2026-10-01T00:00:${String(n % 60).padStart(2, "0")}.${String(n).padStart(3, "0")}Z`;
  return {
    ...assetKey(PID, id),
    assetId: id,
    kind: "video",
    source: "upload",
    sourceUri: `s3://${BUCKET}/projects/${PID}/${id}/x.mp4`,
    sourceSize: 100,
    displayName,
    status: "stored",
    registeredAt,
    [ASSET_LIST_PK_ATTR]: projectPk(PID),
    [ASSET_LIST_SK_ATTR]: `${registeredAt}#${id}`,
    ...over,
  } as Item;
}

// ------------------------------------------------------------------------------------------- uploads

describe("POST /projects/:pid/uploads with folder", () => {
  it("signs the normalized folder as metadata (header for a PUT, server-side for multipart) and echoes it", async () => {
    const s3 = new RecordingS3();
    const r = await write([PID, "uploads"], {
      files: [
        { name: "a.mp4", size: 10, contentType: "video/mp4", folder: " Footage / Día 1/" },
        { name: "b.mp4", size: 200 * MIB, contentType: "video/mp4", folder: "Footage/Day 2" },
        { name: "c.mp4", size: 10, contentType: "video/mp4" },
      ],
    }, { checkAccess: allow, s3: s3 as unknown as UploadS3 });
    expect(r.status).toBe(200);
    const [a, b, c] = r.body.uploads;
    expect(a.folder).toBe("Footage/Día 1");
    expect(a.key).toBe(`projects/${PID}/${a.assetId}/a.mp4`); // the key layout does not change
    expect(a.headers).toEqual({ "Content-Type": "video/mp4", "x-amz-meta-harness-folder": encodeURIComponent("Footage/Día 1") });
    expect(s3.puts[0][4]).toEqual({ "harness-folder": encodeURIComponent("Footage/Día 1") });
    expect(b.folder).toBe("Footage/Day 2");
    expect(s3.creates[0][3]).toEqual({ "harness-folder": encodeURIComponent("Footage/Day 2") });
    expect(c.folder).toBeUndefined();
    expect(c.headers).toEqual({ "Content-Type": "video/mp4" });
    expect(s3.puts[1][4]).toBeUndefined();
  });

  it("refuses the whole request with 400 invalid_folder before presigning anything", async () => {
    const s3 = new RecordingS3();
    const r = await write([PID, "uploads"], {
      files: [{ name: "a.mp4", size: 10, contentType: "video/mp4" }, { name: "b.mp4", size: 10, folder: "x/../y" }],
    }, { checkAccess: allow, s3: s3 as unknown as UploadS3 });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatchObject({ code: "invalid_folder", index: 1, reason: "dot_segment" });
    expect(s3.puts).toEqual([]);
    const notString = await write([PID, "uploads"], { files: [{ name: "a.mp4", size: 10, folder: 7 }] },
      { checkAccess: allow, s3: s3 as unknown as UploadS3 });
    expect(notString.body.error.code).toBe("invalid_folder");
  });

  it("the real presigner keeps the folder a signed header, not a query parameter", async () => {
    const client = new S3Client({ region: "us-west-2", credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "s" },
      requestChecksumCalculation: "WHEN_REQUIRED" });
    const url = new URL(await s3UploadOps(client).presignPut(BUCKET, "k", "video/mp4", 3600, { "harness-folder": "Footage%2FDay%201" }));
    expect(url.searchParams.get("X-Amz-SignedHeaders")).toBe("content-type;host;x-amz-meta-harness-folder");
    expect([...url.searchParams.keys()].some((k) => k.toLowerCase().startsWith("x-amz-meta-"))).toBe(false);
  });
});

describe("POST /projects/:pid/assets (register) with folder", () => {
  const AID = "video:00000000-0000-4000-8000-0000000000aa";
  function service() {
    const calls: AssetServiceCall[] = [];
    const invoke = async (call: AssetServiceCall): Promise<AssetServiceReply> => {
      calls.push(call);
      return { ok: true, value: { asset: { assetId: AID, kind: "video", displayName: "a.mp4", status: "queued", folder: call.args.folder } } };
    };
    return { calls, invoke };
  }

  it("passes a normalized explicit folder, omits an absent one, refuses an invalid one", async () => {
    const svc = service();
    const base = { assetId: AID, fileName: "a.mp4", contentType: "video/mp4", size: 10 };
    const r = await write([PID, "assets"], { ...base, folder: "Footage//Day 1/" }, { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(r.status).toBe(200);
    expect(svc.calls[0].args).toEqual({ ...base, folder: "Footage/Day 1" });
    expect(r.body.folder).toBe("Footage/Day 1"); // the asset view carries it
    await write([PID, "assets"], base, { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(svc.calls[1].args).toEqual(base);
    const bad = await write([PID, "assets"], { ...base, folder: "a[1]" }, { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(bad.status).toBe(400);
    expect(bad.body.error.code).toBe("invalid_folder");
    expect(svc.calls).toHaveLength(2);
  });
});

// ------------------------------------------------------------------------------------ list and search

describe("asset list/search with folders", () => {
  it("list and detail views carry folder; ?q= matches the folder path", async () => {
    const store = new InMemoryTransactStore();
    const inDay1 = row("a.mp4", { folder: "Footage/Day 1" });
    const inDay2 = row("b.mp4", { folder: "Footage/Day 2" });
    const atRoot = row("day 1 notes.mp4");
    store.seed(inDay1, inDay2, atRoot);
    const list = await read("GET", [PID, "assets"], store);
    const byName = Object.fromEntries(list.body.assets.map((a: any) => [a.displayName, a]));
    expect(byName["a.mp4"].folder).toBe("Footage/Day 1");
    expect("folder" in byName["day 1 notes.mp4"]).toBe(false);
    const detail = await read("GET", [PID, "assets", String(inDay2.assetId)], store);
    expect(detail.body.folder).toBe("Footage/Day 2");

    const q = async (s: string) => (await read("GET", [PID, "assets"], store, { query: `?q=${encodeURIComponent(s)}` })).body.assets
      .map((a: any) => a.displayName).sort();
    expect(await q("DAY 1")).toEqual(["a.mp4", "day 1 notes.mp4"]);
    expect(await q("day 1/a")).toEqual(["a.mp4"]);
    expect(await q("footage")).toEqual(["a.mp4", "b.mp4"]);
  });
});

// ------------------------------------------------------------------------------------------ duplicates

describe("POST /projects/:pid/assets/duplicates", () => {
  const dup = (store: unknown, body: unknown, checkAccess: any = allow) =>
    read("POST", [PID, "assets", "duplicates"], store, { body, checkAccess });

  it("matches name (registry-derived, case-insensitive, any ` (n)`) AND size; prefers the unsuffixed original", async () => {
    const store = new InMemoryTransactStore();
    const original = row("Clip.mp4", { folder: "Footage/Day 1" });
    const copy = row("clip.mp4 (2)"); // newer than the original
    const otherSize = row("notes.pdf", { sourceSize: 5 });
    const onlyCopy = row("take.mov (3)", { sourceSize: 7 });
    const removed = row("gone.wav", { status: "removed" });
    const literal = row("draft (2)", { sourceSize: 9 }); // a file whose own name ends in " (2)"
    store.seed(original, copy, otherSize, onlyCopy, removed, literal);
    const r = await dup(store, {
      files: [
        { name: "  [CLIP].mp4", size: 100 }, // derives to "CLIP.mp4"
        { name: "notes.pdf", size: 6 }, // same name, other size → not a duplicate
        { name: "take.mov", size: 7 }, // only a ` (n)` copy exists → that copy
        { name: "gone.wav", size: 100 }, // removed → not a duplicate
        { name: "new.mp4", size: 100 },
        { name: "draft (2)", size: 9 },
        { name: "[]@#", size: 100 }, // nothing left → never a duplicate
      ],
    });
    expect(r.status).toBe(200);
    expect(r.body.duplicates.map((d: any) => [d.index, d.asset.assetId])).toEqual([
      [0, original.assetId],
      [2, onlyCopy.assetId],
      [5, literal.assetId],
    ]);
    expect(r.body.duplicates[0].asset).toMatchObject({ displayName: "Clip.mp4", folder: "Footage/Day 1", sourceSize: 100 });
    expect(r.body.duplicates[0].asset.sourceUri).toBeUndefined();
    expect(r.body.truncated).toBeUndefined();
  });

  it("is one pass over the newest-first GSI, stopping once every file has its original", async () => {
    const pages = [
      { items: [row("a.mp4")], lastEvaluatedKey: { k: 1 } },
      { items: [row("b.mp4")], lastEvaluatedKey: { k: 2 } },
      { items: [row("c.mp4")] },
    ];
    const seen: any[] = [];
    const store = { batchGet: async () => [], queryIndex: async (o: any) => (seen.push(o), pages[seen.length - 1]) };
    const r = await dup(store, { files: [{ name: "a.mp4", size: 100 }, { name: "b.mp4", size: 100 }] });
    expect(r.body.duplicates.map((d: any) => d.index)).toEqual([0, 1]);
    expect(seen).toHaveLength(2); // never read page 3
    expect(seen[0]).toMatchObject({ indexName: "assetsByRegistered", ascending: false, limit: 50 });
    expect(seen[1].exclusiveStartKey).toEqual({ k: 1 });
  });

  it("is the uniform 404 on denial (reading nothing) and validates the body", async () => {
    let reads = 0;
    const store = { batchGet: async () => [], queryIndex: async () => (reads++, { items: [] }) };
    const denied = await dup(store, { files: [{ name: "a.mp4", size: 1 }] }, deny);
    expect(denied.status).toBe(404);
    expect(denied.body.error.code).toBe("project_not_found");
    expect(reads).toBe(0);
    expect((await dup(store, { files: [] })).status).toBe(400);
    expect((await dup(store, { files: [{ name: "a", size: -1 }] })).status).toBe(400);
    expect((await dup(store, "{nope")).status).toBe(400);
    const many = Array.from({ length: DUPLICATES_MAX_FILES + 1 }, (_, i) => ({ name: `f${i}`, size: 1 }));
    const tooMany = await dup(store, { files: many });
    expect(tooMany.status).toBe(400);
    expect(tooMany.body.error.code).toBe("too_many_files");
    const ok = await dup(store, { files: many.slice(0, DUPLICATES_MAX_FILES) });
    expect(ok.status).toBe(200);
    const huge = await dup(store, { files: [{ name: "x".repeat(2 * 1024 * 1024), size: 1 }] });
    expect(huge.status).toBe(413);
  });

  it("GET on the path is not the duplicates route", async () => {
    const r = await read("GET", [PID, "assets", "duplicates"], new InMemoryTransactStore());
    expect(r.status).toBe(404); // a detail lookup of a malformed id
  });
});
