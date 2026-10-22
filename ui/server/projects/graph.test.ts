// Knowledge graph on the proxy: the `graph` setting (written only while the feature is on), `enableGraph`,
// the `/projects/:pid/graph*` routes, and the reader client's failure mapping.
import { gunzipSync } from "node:zlib";
import { describe, expect, it, vi } from "vitest";
import type { AccessResult } from "./access";
import { createGraphServiceClient } from "./graphServiceClient";
import type { GraphCall, GraphReply } from "./graphServiceClient";
import { dispatchGraphRoute } from "./graphRoutes";
import { InMemoryTransactStore } from "./inMemoryStore";
import { ProjectRegistry } from "./registry";
import type { RegistryResult } from "./registry";
import { metaKey } from "./schema";
import { graphOn } from "./settings";

const pidOf = (n: number) => "p" + n.toString(16).padStart(32, "0");
const PID = pidOf(7);

function setup(graphFeature: boolean) {
  let n = 0;
  const store = new InMemoryTransactStore();
  const reg = new ProjectRegistry({
    store, now: () => new Date("2026-10-08T00:00:00.000Z"), sleep: async () => {}, random: () => 0,
    newId: () => pidOf(++n), graphFeature,
  });
  const rawMeta = async (pid: string) => (await store.batchGet([metaKey(pid)]))[0];
  return { store, reg, rawMeta };
}

function value<T>(r: RegistryResult<T>): T {
  if (!r.ok) throw new Error(`expected ok, got ${JSON.stringify(r.error)}`);
  return r.value;
}

describe("graph setting", () => {
  it("graphOn: only true is ON; absent is OFF", () => {
    expect(graphOn({ graph: true })).toBe(true);
    expect(graphOn({})).toBe(false);
    expect(graphOn({ graph: "true" })).toBe(false);
    expect(graphOn(null)).toBe(false);
  });

  it("feature off: create and Personal never write the attribute", async () => {
    const { reg, rawMeta } = setup(false);
    const p = value(await reg.create("alice", "Promo", { graph: true }));
    expect(p.graph).toBe(false);
    expect("graph" in (await rawMeta(p.projectId))!).toBe(false);
    const personal = value(await reg.ensurePersonal("alice"));
    expect("graph" in (await rawMeta(personal.projectId))!).toBe(false);
  });

  it("feature on: create defaults to ON, honours false, and Personal gets a graph", async () => {
    const { reg, rawMeta } = setup(true);
    expect(value(await reg.create("alice", "On by default")).graph).toBe(true);
    const off = value(await reg.create("alice", "Opted out", { graph: false }));
    expect(off.graph).toBe(false);
    expect((await rawMeta(off.projectId))!.graph).toBe(false);
    expect(value(await reg.ensurePersonal("bob")).graph).toBe(true);
  });

  it("refuses a non-boolean graph value", async () => {
    const { reg } = setup(true);
    expect(await reg.create("alice", "X", { graph: "yes" as unknown as boolean })).toMatchObject({
      ok: false, error: { code: "invalid_settings", field: "graph" },
    });
  });

  it("enableGraph: owner only, one way, idempotent, not for deleted or unknown projects", async () => {
    const { reg, rawMeta } = setup(true);
    const p = value(await reg.create("alice", "Later", { graph: false }));
    expect(await reg.enableGraph("mallory", p.projectId)).toMatchObject({ ok: false, error: { code: "project_not_found" } });
    const on = value(await reg.enableGraph("alice", p.projectId));
    expect(on.graph).toBe(true);
    expect(await rawMeta(p.projectId)).toMatchObject({ graph: true, graphEnabledBy: "alice" });
    expect(value(await reg.enableGraph("alice", p.projectId)).graph).toBe(true);
    value(await reg.delete("alice", p.projectId, "Later"));
    expect(await reg.enableGraph("alice", p.projectId)).toMatchObject({ ok: false, error: { code: "project_not_found" } });
  });

  it("enableGraph is refused while the feature is off", async () => {
    const { reg } = setup(false);
    const p = value(await reg.create("alice", "X"));
    expect(await reg.enableGraph("alice", p.projectId)).toMatchObject({ ok: false, error: { code: "graph_not_enabled" } });
  });
});

// ---- routes ----------------------------------------------------------------------------------------

function fakeRes() {
  const r: any = { status: 0, headers: {} as Record<string, string>, body: Buffer.alloc(0), headersSent: false };
  r.writeHead = (s: number, h: Record<string, string>) => { r.status = s; r.headers = h; r.headersSent = true; };
  r.end = (b?: Buffer | string) => { if (b) r.body = Buffer.isBuffer(b) ? b : Buffer.from(b); };
  r.json = () => JSON.parse((r.headers["content-encoding"] === "gzip" ? gunzipSync(r.body) : r.body).toString());
  return r;
}

const allow = async (): Promise<AccessResult> => ({ ok: true } as unknown as AccessResult);
const deny = async (): Promise<AccessResult> => ({ ok: false } as unknown as AccessResult);

async function call(path: string, opts: { method?: string; access?: typeof allow; reply?: GraphReply; headers?: Record<string, string>;
  enableGraph?: (a: string, p: string) => Promise<RegistryResult<any>> } = {}) {
  const invoke = vi.fn(async (_c: GraphCall) => opts.reply ?? { ok: true, body: { state: "ready", generation: 3 } } as GraphReply);
  const res = fakeRes();
  const parts = new URL(`http://x/${path}`).pathname.split("/").filter(Boolean);
  const handled = await dispatchGraphRoute({ url: `/${path}`, headers: opts.headers ?? {} }, res,
    { actor: "alice", pid: PID, parts: parts.slice(0), method: opts.method ?? "GET" },
    { checkAccess: opts.access ?? allow, invoke, enableGraph: opts.enableGraph });
  return { handled, res, invoke };
}

describe("graph routes", () => {
  it("ignores paths it does not own", async () => {
    expect((await call(`${PID}/assets`)).handled).toBe(false);
    expect((await call(`${PID}/graph`, { method: "DELETE" })).handled).toBe(false);
  });

  it("a non-member gets the uniform project_not_found and the reader is never called", async () => {
    const { res, invoke } = await call(`${PID}/graph`, { access: deny });
    expect(res.status).toBe(404);
    expect(res.json()).toEqual({ error: { code: "project_not_found", message: "Project not found" } });
    expect(invoke).not.toHaveBeenCalled();
  });

  it("overview: passes reader states through, sets a generation ETag and answers 304 on a match", async () => {
    const ok = await call(`${PID}/graph`);
    expect(ok.res.status).toBe(200);
    expect(ok.res.headers.etag).toBe('W/"g3"');
    expect(ok.invoke).toHaveBeenCalledWith({ op: "overview", projectId: PID });
    const cached = await call(`${PID}/graph`, { headers: { "if-none-match": 'W/"g3"' } });
    expect(cached.res.status).toBe(304);
    const seeding = await call(`${PID}/graph`, { reply: { ok: true, body: { state: "seeding" } } });
    expect(seeding.res.status).toBe(200);
    expect(seeding.res.json()).toEqual({ state: "seeding" });
  });

  it("gzips a large overview when the client accepts it", async () => {
    const nodes = Array.from({ length: 2000 }, (_, i) => [`sample:Person:${i}`, 6, `Person ${i}`, 1, 2]);
    const { res } = await call(`${PID}/graph`, {
      headers: { "accept-encoding": "gzip, br" }, reply: { ok: true, body: { state: "ready", generation: 1, nodes } },
    });
    expect(res.headers["content-encoding"]).toBe("gzip");
    expect(res.json().nodes).toHaveLength(2000);
  });

  it("maps an unreachable reader and reader errors to 503, node errors to 404", async () => {
    const down = await call(`${PID}/graph`, { reply: { ok: false, status: 503, code: "graph_service_unavailable", reason: "x" } });
    expect(down.res.status).toBe(503);
    expect(down.res.json().error.code).toBe("graph_service_unavailable");
    const boom = await call(`${PID}/graph/stats`, { reply: { ok: true, body: { state: "error", error: { code: "graph_read_failed" } } } });
    expect(boom.res.status).toBe(503);
    const gone = await call(`${PID}/graph/nodes/${encodeURIComponent("asset:nope")}`,
      { reply: { ok: true, body: { state: "error", error: { code: "node_not_found" } } } });
    expect(gone.res.status).toBe(404);
    expect(gone.res.json().error.code).toBe("node_not_found");
    const notFound = await call(`${PID}/graph`, { reply: { ok: true, body: { state: "not_found" } } });
    expect(notFound.res.status).toBe(404);
  });

  it("neighbourhood: validates the node id before calling the reader, and passes hops", async () => {
    const bad = await call(`${PID}/graph/nodes/${encodeURIComponent("../../etc")}`);
    expect(bad.res.status).toBe(404);
    expect(bad.invoke).not.toHaveBeenCalled();
    const ok = await call(`${PID}/graph/nodes/${encodeURIComponent(`project:${PID}`)}?hops=2`);
    expect(ok.invoke).toHaveBeenCalledWith({ op: "neighbourhood", projectId: PID, args: { nodeId: `project:${PID}`, hops: 2 } });
  });

  it("enable: relays the registry result and its status", async () => {
    const ok = await call(`${PID}/graph/enable`, { method: "POST", enableGraph: async () => ({ ok: true, value: { graph: true } }) });
    expect(ok.res.status).toBe(200);
    expect(ok.res.json()).toEqual({ project: { graph: true } });
    const member = await call(`${PID}/graph/enable`, { method: "POST", enableGraph: async () =>
      ({ ok: false, error: { code: "owner_only", action: "settings", message: "m" } }) });
    expect(member.res.status).toBe(403);
  });
});

describe("graph reader client", () => {
  it("no GRAPH_FN, a function error, a non-JSON body, a timeout: all graph_service_unavailable", async () => {
    const signer = async () => ({});
    expect(await createGraphServiceClient({ env: {} }).invoke({ op: "stats", projectId: PID }))
      .toMatchObject({ ok: false, status: 503, code: "graph_service_unavailable" });
    const fnErr = createGraphServiceClient({ env: { GRAPH_FN: "g" }, signer,
      transport: async () => ({ status: 200, headers: { "x-amz-function-error": "Unhandled" }, body: "{}" }) });
    expect((await fnErr.invoke({ op: "stats", projectId: PID })).ok).toBe(false);
    const notJson = createGraphServiceClient({ env: { GRAPH_FN: "g" }, signer,
      transport: async () => ({ status: 200, headers: {}, body: "<html>" }) });
    expect((await notJson.invoke({ op: "stats", projectId: PID })).ok).toBe(false);
    const slow = createGraphServiceClient({ env: { GRAPH_FN: "g" }, signer, budgetMs: 5,
      transport: ({ signal }) => new Promise((_r, rej) => signal?.addEventListener("abort", () => rej(new Error("aborted")))) });
    expect(await slow.invoke({ op: "stats", projectId: PID })).toMatchObject({ ok: false, reason: "timeout" });
  });

  it("signs a RequestResponse invoke of GRAPH_FN with the op and project only", async () => {
    let sent: any;
    const client = createGraphServiceClient({
      env: { GRAPH_FN: "mediaworks-harness-graph-reader", AWS_REGION: "us-west-2" }, signer: async ({ headers }) => headers,
      transport: async (req) => { sent = req; return { status: 200, headers: {}, body: JSON.stringify({ state: "ready" }) }; },
    });
    expect(await client.invoke({ op: "stats", projectId: PID })).toEqual({ ok: true, body: { state: "ready" } });
    expect(sent.url).toBe("https://lambda.us-west-2.amazonaws.com/2015-03-31/functions/mediaworks-harness-graph-reader/invocations");
    expect(sent.headers["x-amz-invocation-type"]).toBe("RequestResponse");
    expect(JSON.parse(sent.body)).toEqual({ op: "stats", projectId: PID, args: {}, caller: "proxy" });
  });
});

describe("turn context", () => {
  it("carries graph: true to the agent only when the Project has a graph", async () => {
    const { buildTurnContext } = await import("./turnContext");
    const base = { turnId: "t", webSearch: false, projectAccess: "ok" as const };
    const on = buildTurnContext({ ...base, activeProject: { id: PID, name: "P", personal: false, graph: true } });
    const off = buildTurnContext({ ...base, activeProject: { id: PID, name: "P", personal: false, graph: false } });
    expect((on as any).active_project.graph).toBe(true);
    expect("graph" in (off as any).active_project).toBe(false);
  });
});
