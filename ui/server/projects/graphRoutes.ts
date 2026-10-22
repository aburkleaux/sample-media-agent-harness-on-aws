// `/projects/:pid/graph*` — the knowledge-graph routes (dispatched from `routes.ts`).
//
//   GET  /projects/:pid/graph                     whole-graph overview (compact arrays; gzip when accepted;
//                                                 `ETag: W/"g<generation>"`, `If-None-Match` → 304)
//   GET  /projects/:pid/graph/stats               counts, generation, updatedAt
//   GET  /projects/:pid/graph/nodes/:nodeId?hops= neighbourhood of one node (hops 1|2)
//   POST /projects/:pid/graph/enable              owner-only, one way (registry `enableGraph`)
//
// Every route runs `checkAccess(actor, pid)` first; any denial is the uniform `404 project_not_found`, so
// these routes cannot be used to learn whether another Project exists. The reader Lambda is reached only from
// here (its resource policy names the proxy role) and re-checks that the graph is on.
//
// Reader `state`s pass through as 200 bodies — `ready`, `off` (graph not enabled), `seeding` (first build in
// progress) — because the SPA renders each of them. `not_found` is the uniform 404; `invalid_node` /
// `node_not_found` are 404s with their own code; anything else is `503 graph_service_unavailable`.
import { gzipSync } from "node:zlib";
import { PROJECT_NOT_FOUND_BODY, checkAccess as realCheckAccess, projectRegistry } from "./access";
import type { AccessResult } from "./access";
import { invokeGraphService } from "./graphServiceClient";
import type { GraphCall, GraphReply } from "./graphServiceClient";
import { registryErrorStatus } from "./registry";
import type { ProjectMeta, RegistryResult } from "./registry";

/** Node ids are `project:` / `user:` / `tag:` / `asset:` / `sample:` + text (agent `project_graph/ids.py`). */
const NODE_ID_RE = /^(project|user|tag|asset|sample):[^\u0000-\u001f\u007f]{1,190}$/u;
/** Gzip bodies above this size (an overview of a few hundred nodes is already ~20 KB). */
const GZIP_MIN_BYTES = 8 * 1024;

export interface GraphRouteContext {
  actor: string;
  pid: string;
  parts: string[];
  method: string;
}

export interface GraphRouteDeps {
  checkAccess?: (actor: string, pid: string) => Promise<AccessResult>;
  invoke?: (call: GraphCall) => Promise<GraphReply>;
  enableGraph?: (actor: string, pid: string) => Promise<RegistryResult<ProjectMeta>>;
}

function send(req: any, res: any, status: number, body: unknown, extra: Record<string, string> = {}): void {
  if (res.headersSent) return;
  const json = JSON.stringify(body);
  const headers: Record<string, string> = { "content-type": "application/json", "cache-control": "no-store", ...extra };
  const accepts = /\bgzip\b/i.test(String(req.headers?.["accept-encoding"] ?? ""));
  if (accepts && Buffer.byteLength(json) >= GZIP_MIN_BYTES) {
    const gz = gzipSync(json);
    res.writeHead(status, { ...headers, "content-encoding": "gzip", vary: "accept-encoding", "content-length": String(gz.length) });
    res.end(gz);
    return;
  }
  res.writeHead(status, headers);
  res.end(json);
}

function decodeSegment(seg: string): string | null {
  try {
    return decodeURIComponent(seg);
  } catch {
    return null;
  }
}

function relay(req: any, res: any, reply: GraphReply, etag?: string): void {
  if (!reply.ok) {
    send(req, res, 503, { error: { code: reply.code, message: "The knowledge graph is unavailable; try again" } });
    return;
  }
  const b = reply.body;
  if (b.state === "not_found") {
    send(req, res, 404, PROJECT_NOT_FOUND_BODY);
    return;
  }
  if (b.state === "error") {
    const code = String((b.error as { code?: unknown } | undefined)?.code ?? "graph_read_failed");
    if (code === "invalid_node" || code === "node_not_found") {
      send(req, res, 404, { error: { code, message: code === "invalid_node" ? "Malformed node id" : "No such node" } });
      return;
    }
    send(req, res, 503, { error: { code: "graph_service_unavailable", message: "The knowledge graph is unavailable; try again" } });
    return;
  }
  send(req, res, 200, b, etag ? { etag } : {});
}

export async function dispatchGraphRoute(req: any, res: any, ctx: GraphRouteContext, deps: GraphRouteDeps = {}): Promise<boolean> {
  const { parts, method, actor, pid } = ctx;
  if (parts[1] !== "graph") return false;
  const isOverview = method === "GET" && parts.length === 2;
  const isStats = method === "GET" && parts.length === 3 && parts[2] === "stats";
  const isNode = method === "GET" && parts.length === 4 && parts[2] === "nodes";
  const isEnable = method === "POST" && parts.length === 3 && parts[2] === "enable";
  if (!isOverview && !isStats && !isNode && !isEnable) return false;

  const checkAccess = deps.checkAccess ?? realCheckAccess;
  const invoke = deps.invoke ?? invokeGraphService;
  try {
    if (isEnable) {
      const r = await (deps.enableGraph ?? ((a, p) => projectRegistry().enableGraph(a, p)))(actor, pid);
      if (r.ok) send(req, res, 200, { project: r.value });
      else send(req, res, registryErrorStatus(r.error), { error: r.error });
      return true;
    }
    const access = await checkAccess(actor, pid);
    if (!access.ok) {
      send(req, res, 404, PROJECT_NOT_FOUND_BODY);
      return true;
    }
    if (isOverview) {
      const reply = await invoke({ op: "overview", projectId: pid });
      const gen = reply.ok && reply.body.state === "ready" ? Number(reply.body.generation) : NaN;
      const etag = Number.isFinite(gen) ? `W/"g${gen}"` : undefined;
      if (etag && String(req.headers?.["if-none-match"] ?? "") === etag) {
        res.writeHead(304, { etag, "cache-control": "no-store" });
        res.end();
        return true;
      }
      relay(req, res, reply, etag);
      return true;
    }
    if (isStats) {
      relay(req, res, await invoke({ op: "stats", projectId: pid }));
      return true;
    }
    const nodeId = decodeSegment(parts[3]);
    if (!nodeId || !NODE_ID_RE.test(nodeId)) {
      send(req, res, 404, { error: { code: "invalid_node", message: "Malformed node id" } });
      return true;
    }
    const url = new URL(String(req.url || "/"), "http://localhost");
    const hops = url.searchParams.get("hops") === "2" ? 2 : 1;
    relay(req, res, await invoke({ op: "neighbourhood", projectId: pid, args: { nodeId, hops } }));
    return true;
  } catch (e) {
    console.error("[projects] graph route error:", (e as Error)?.message ?? e);
    send(req, res, 500, { error: { code: "internal_error", message: "Internal error" } });
    return true;
  }
}
