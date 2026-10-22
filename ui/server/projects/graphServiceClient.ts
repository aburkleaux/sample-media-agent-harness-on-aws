// Graph reader client: the proxy's only path to a Project's knowledge graph (the x86 graph-reader Lambda,
// `agent/src/project_graph/handler.py::reader_handler`).
//
//   { op: "overview" | "neighbourhood" | "stats" | "health", projectId, args, caller: "proxy" }
//   → { state: "ready" | "off" | "seeding" | "not_found" | "error", ... }
//
// The proxy has ALREADY authenticated the user and checked membership (`checkAccess`) before calling, so the
// event carries no user token; IAM limits invocation to the proxy task role (the function's resource policy).
// Deployed: `GRAPH_FN` (name or ARN), a SigV4-signed RequestResponse Invoke reusing the Asset_Service
// signer/transport. Every failure to reach the reader (no `GRAPH_FN`, timeout, non-2xx, function error,
// malformed body) is `{ok:false, status:503, code:"graph_service_unavailable"}` — it never throws.
import { nodeTransport, sigV4LambdaSigner } from "./assetServiceClient";
import type { IssuerTransport, LambdaSigner } from "./tokenIssuerClient";

export const GRAPH_FN_ENV = "GRAPH_FN";
/**
 * The reader Lambda's timeout is 60 s (a cold image + first S3 load of a large graph is the slow case). The
 * budget stops 2 s short of the 60 s CloudFront read timeout and ALB idle timeout in front of the proxy, so a
 * slow read still ends as this client's JSON `503 graph_service_unavailable`, never a CloudFront 504 page.
 */
export const GRAPH_SERVICE_BUDGET_MS = 58_000;

export type GraphOp = "overview" | "neighbourhood" | "stats" | "health";

export interface GraphCall {
  op: GraphOp;
  projectId: string;
  args?: Record<string, unknown>;
}

export type GraphReply =
  | { ok: true; body: Record<string, unknown> & { state: string } }
  | { ok: false; status: 503; code: "graph_service_unavailable"; reason: string };

export interface GraphClientOptions {
  env?: NodeJS.ProcessEnv;
  transport?: IssuerTransport;
  signer?: LambdaSigner;
  budgetMs?: number;
}

const unavailable = (reason: string): GraphReply => ({ ok: false, status: 503, code: "graph_service_unavailable", reason });

export function createGraphServiceClient(opts: GraphClientOptions = {}): { invoke: (c: GraphCall) => Promise<GraphReply> } {
  const env = opts.env ?? process.env;
  const transport = opts.transport ?? nodeTransport;
  const signer = opts.signer ?? sigV4LambdaSigner;
  const budgetMs = opts.budgetMs ?? GRAPH_SERVICE_BUDGET_MS;

  async function invoke(call: GraphCall): Promise<GraphReply> {
    const fn = (env[GRAPH_FN_ENV] ?? "").trim();
    if (!fn) return unavailable(`${GRAPH_FN_ENV} is not set`);
    const region = /^arn:aws[a-z-]*:lambda:([a-z0-9-]+):/.exec(fn)?.[1] || env.AWS_REGION || env.AWS_DEFAULT_REGION || "us-west-2";
    const hostname = `lambda.${region}.amazonaws.com`;
    const path = `/2015-03-31/functions/${encodeURIComponent(fn)}/invocations`;
    const body = JSON.stringify({ op: call.op, projectId: call.projectId, args: call.args ?? {}, caller: "proxy" });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), budgetMs);
    try {
      const headers = await signer({
        region, hostname, path, body,
        headers: { host: hostname, "content-type": "application/json", "x-amz-invocation-type": "RequestResponse" },
      });
      const res = await transport({ url: `https://${hostname}${path}`, method: "POST", headers, body, signal: controller.signal });
      if (res.status < 200 || res.status >= 300) return unavailable(`graph reader returned ${res.status}`);
      if (res.headers["x-amz-function-error"]) return unavailable("graph reader function error");
      let payload: unknown;
      try {
        payload = JSON.parse(res.body);
      } catch {
        return unavailable("graph reader reply is not JSON");
      }
      if (!payload || typeof payload !== "object" || typeof (payload as { state?: unknown }).state !== "string") {
        return unavailable("malformed graph reader reply");
      }
      return { ok: true, body: payload as Record<string, unknown> & { state: string } };
    } catch (err) {
      if (controller.signal.aborted) return unavailable("timeout");
      return unavailable(`request failed: ${(err instanceof Error ? err.message : String(err)).slice(0, 200)}`);
    } finally {
      clearTimeout(timer);
    }
  }

  return { invoke };
}

let defaultClient: ReturnType<typeof createGraphServiceClient> | undefined;

/** Invoke the configured graph reader (env read per call). Never throws. */
export function invokeGraphService(call: GraphCall): Promise<GraphReply> {
  if (!defaultClient) defaultClient = createGraphServiceClient();
  return defaultClient.invoke(call);
}
