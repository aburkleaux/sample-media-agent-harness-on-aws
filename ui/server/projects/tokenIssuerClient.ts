// Token_Issuer client (media-harness-projects-access Req 4.1, 4.17; design Component 2 step 3, Component 3,
// D2).
//
// `issueProjectToken(userToken, projectId)` asks `TokenIssuerFn` for a Project_Token for one chat turn. The
// event is `{userToken, projectId}`; the Lambda answers `{"status":"ok","token","exp","jti"}`,
// `{"status":"not_found"}`, `{"status":"unauthenticated"}`, or `{"status":"error"}`. This client maps
// those to `ok(token, exp)`, `not_found`, `unauthenticated`, and `unavailable`. Everything else is
// `unavailable` too: a 2 s `AbortController` budget expiring (it covers credential resolution, signing,
// and the request), a non-2xx, a Lambda function error (`X-Amz-Function-Error`), a malformed body, or no
// configuration. The turn gate continues an `unavailable` turn without a token (Req 4.17).
//
// The proxy has no @aws-sdk/client-lambda dependency, so the Invoke REST call is SigV4-signed the same
// way the proxy signs other AWS JSON calls. The function comes from `TOKEN_ISSUER_FN` (name or ARN).
//
// DEV ONLY: `LOCAL_TOKEN_ISSUER_URL` (e.g. `http://127.0.0.1:8098`) sends the same event as JSON to
// `${LOCAL_TOKEN_ISSUER_URL}/issue`, unsigned, with the same budget and response mapping, for a local
// wrapper around the issuer's `make_issue_handler`. It is refused (with a warning) when
// `NODE_ENV=production`, as in the proxy image, like `PROJECTS_STORE=memory` in `access.ts`; the client
// then uses `TOKEN_ISSUER_FN`.
//
// The token, the User_Token, and response bodies are never logged or put into a `reason`.
import http from "node:http";
import https from "node:https";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { isProjectId } from "./ids";

/** The allowlisted Main_Agent runtime header that carries the Project_Token (never the body, Req 4.15). */
export const PROJECT_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken";

export const TOKEN_ISSUER_BUDGET_MS = 2_000;
export const TOKEN_ISSUER_FN_ENV = "TOKEN_ISSUER_FN";
export const LOCAL_TOKEN_ISSUER_URL_ENV = "LOCAL_TOKEN_ISSUER_URL";

const MAX_RESPONSE_BYTES = 64 * 1024;

export type IssueResult =
  | { status: "ok"; token: string; exp: number }
  | { status: "not_found" }
  | { status: "unauthenticated" }
  /** `reason` is for logs and carries no token or body content. */
  | { status: "unavailable"; reason: string };

export interface IssuerHttpRequest {
  url: string;
  method: "POST";
  headers: Record<string, string>;
  body: string;
  signal: AbortSignal;
}

export interface IssuerHttpResponse {
  status: number;
  /** Lower-cased header names. */
  headers: Record<string, string>;
  body: string;
}

/** Sends one request. Must reject (or settle) when `signal` aborts. */
export type IssuerTransport = (req: IssuerHttpRequest) => Promise<IssuerHttpResponse>;

/** Signs a Lambda Invoke request; returns the headers to send. */
export type LambdaSigner = (req: {
  region: string;
  hostname: string;
  path: string;
  headers: Record<string, string>;
  body: string;
}) => Promise<Record<string, string>>;

export interface TokenIssuerClientOptions {
  env?: NodeJS.ProcessEnv;
  transport?: IssuerTransport;
  signer?: LambdaSigner;
  budgetMs?: number;
  warn?: (msg: string) => void;
}

type Target =
  | { kind: "local"; url: string }
  | { kind: "lambda"; region: string; hostname: string; path: string }
  | { kind: "none"; reason: string };

/** Which issuer this env selects. Exported for tests and for the turn gate's startup log. */
export function resolveIssuerTarget(env: NodeJS.ProcessEnv, warn: (msg: string) => void = console.warn): Target {
  const local = (env[LOCAL_TOKEN_ISSUER_URL_ENV] ?? "").trim();
  if (local) {
    if (env.NODE_ENV === "production") {
      warn(`[projects] ${LOCAL_TOKEN_ISSUER_URL_ENV} is dev-only and is ignored when NODE_ENV=production`);
    } else {
      let parsed: URL | null = null;
      try {
        parsed = new URL(local);
      } catch {
        parsed = null;
      }
      if (!parsed || (parsed.protocol !== "http:" && parsed.protocol !== "https:")) {
        return { kind: "none", reason: `${LOCAL_TOKEN_ISSUER_URL_ENV} is not an http(s) URL` };
      }
      return { kind: "local", url: `${local.replace(/\/+$/, "")}/issue` };
    }
  }
  const fn = (env[TOKEN_ISSUER_FN_ENV] ?? "").trim();
  if (!fn) return { kind: "none", reason: `${TOKEN_ISSUER_FN_ENV} is not set` };
  // An ARN names its own region (arn:aws:lambda:<region>:<acct>:function:<name>).
  const arnRegion = /^arn:aws[a-z-]*:lambda:([a-z0-9-]+):/.exec(fn)?.[1];
  const region = arnRegion || env.AWS_REGION || env.AWS_DEFAULT_REGION || "us-west-2";
  return {
    kind: "lambda",
    region,
    hostname: `lambda.${region}.amazonaws.com`,
    path: `/2015-03-31/functions/${encodeURIComponent(fn)}/invocations`,
  };
}

/** Maps an issuer response body (already JSON-parsed) to a result. */
export function mapIssuerPayload(payload: unknown): IssueResult {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return { status: "unavailable", reason: "malformed issuer response" };
  }
  const p = payload as Record<string, unknown>;
  switch (p.status) {
    case "ok": {
      const { token, exp } = p;
      if (typeof token !== "string" || token === "" || typeof exp !== "number" || !Number.isInteger(exp) || exp <= 0) {
        return { status: "unavailable", reason: "malformed issuer ok response" };
      }
      return { status: "ok", token, exp };
    }
    case "not_found":
      return { status: "not_found" };
    case "unauthenticated":
      return { status: "unauthenticated" };
    case "error":
      return { status: "unavailable", reason: "issuer error" };
    default:
      return { status: "unavailable", reason: "unknown issuer status" };
  }
}

function mapHttpResponse(res: IssuerHttpResponse, kind: "local" | "lambda"): IssueResult {
  if (res.status < 200 || res.status >= 300) {
    return { status: "unavailable", reason: `${kind} issuer returned ${res.status}` };
  }
  // Invoke returns 200 even when the function threw; the error is flagged in this header.
  if (kind === "lambda" && res.headers["x-amz-function-error"]) {
    return { status: "unavailable", reason: "issuer function error" };
  }
  let payload: unknown;
  try {
    payload = JSON.parse(res.body);
  } catch {
    return { status: "unavailable", reason: "issuer response is not JSON" };
  }
  return mapIssuerPayload(payload);
}

class BudgetExceeded extends Error {
  constructor() {
    super("token issuer budget exceeded");
  }
}

/** Resolves with `p`, or rejects with `BudgetExceeded` as soon as `signal` aborts. */
function withinBudget<T>(p: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new BudgetExceeded());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new BudgetExceeded());
    signal.addEventListener("abort", onAbort, { once: true });
    p.then(
      (v) => {
        signal.removeEventListener("abort", onAbort);
        resolve(v);
      },
      (e) => {
        signal.removeEventListener("abort", onAbort);
        reject(e);
      },
    );
  });
}

function describeError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  return msg.slice(0, 200);
}

export function createTokenIssuerClient(opts: TokenIssuerClientOptions = {}): {
  issueProjectToken: (userToken: string, projectId: string) => Promise<IssueResult>;
} {
  const env = opts.env ?? process.env;
  const transport = opts.transport ?? nodeTransport;
  const signer = opts.signer ?? sigV4LambdaSigner;
  const budgetMs = opts.budgetMs ?? TOKEN_ISSUER_BUDGET_MS;
  const sink = opts.warn ?? ((m: string) => console.warn(m));
  // The production refusal is re-evaluated per call (env may change) but logged once per client.
  let warned = false;
  const warn = (m: string) => {
    if (!warned) {
      warned = true;
      sink(m);
    }
  };

  async function issueProjectToken(userToken: string, projectId: string): Promise<IssueResult> {
    // The issuer would answer these without reading or signing anything; skip the round trip.
    if (typeof userToken !== "string" || userToken === "") return { status: "unauthenticated" };
    if (!isProjectId(projectId)) return { status: "not_found" };

    const target = resolveIssuerTarget(env, warn);
    if (target.kind === "none") return { status: "unavailable", reason: target.reason };

    const body = JSON.stringify({ userToken, projectId });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), budgetMs);
    try {
      if (target.kind === "local") {
        const res = await withinBudget(
          transport({
            url: target.url,
            method: "POST",
            headers: { "content-type": "application/json" },
            body,
            signal: controller.signal,
          }),
          controller.signal,
        );
        return mapHttpResponse(res, "local");
      }
      const baseHeaders = {
        host: target.hostname,
        "content-type": "application/json",
        "x-amz-invocation-type": "RequestResponse",
      };
      const headers = await withinBudget(
        signer({ region: target.region, hostname: target.hostname, path: target.path, headers: baseHeaders, body }),
        controller.signal,
      );
      const res = await withinBudget(
        transport({
          url: `https://${target.hostname}${target.path}`,
          method: "POST",
          headers,
          body,
          signal: controller.signal,
        }),
        controller.signal,
      );
      return mapHttpResponse(res, "lambda");
    } catch (err) {
      if (err instanceof BudgetExceeded || controller.signal.aborted) {
        return { status: "unavailable", reason: "timeout" };
      }
      return { status: "unavailable", reason: `request failed: ${describeError(err)}` };
    } finally {
      clearTimeout(timer);
    }
  }

  return { issueProjectToken };
}

// ---- default pieces ------------------------------------------------------------------------------

const lambdaSigners = new Map<string, SignatureV4>();

const sigV4LambdaSigner: LambdaSigner = async ({ region, hostname, path, headers, body }) => {
  let s = lambdaSigners.get(region);
  if (!s) {
    s = new SignatureV4({ service: "lambda", region, credentials: defaultProvider(), sha256: Sha256 });
    lambdaSigners.set(region, s);
  }
  const signed = await s.sign(new HttpRequest({ method: "POST", protocol: "https:", hostname, path, headers, body }));
  return signed.headers as Record<string, string>;
};

const nodeTransport: IssuerTransport = (req) =>
  new Promise<IssuerHttpResponse>((resolve, reject) => {
    const lib = req.url.startsWith("https:") ? https : http;
    const r = lib.request(
      req.url,
      { method: req.method, headers: { ...req.headers, "content-length": String(Buffer.byteLength(req.body)) }, signal: req.signal },
      (res) => {
        const chunks: Buffer[] = [];
        let size = 0;
        res.on("data", (c: Buffer) => {
          size += c.length;
          if (size > MAX_RESPONSE_BYTES) {
            res.destroy(new Error("issuer response too large"));
            return;
          }
          chunks.push(c);
        });
        res.on("error", (e) => reject(e));
        res.on("end", () => {
          const headers: Record<string, string> = {};
          for (const [k, v] of Object.entries(res.headers)) {
            if (v !== undefined) headers[k.toLowerCase()] = Array.isArray(v) ? v.join(", ") : v;
          }
          resolve({ status: res.statusCode ?? 500, headers, body: Buffer.concat(chunks).toString("utf8") });
        });
      },
    );
    r.on("error", (e) => reject(e));
    r.end(req.body);
  });

const defaultClient = createTokenIssuerClient();

/** Asks the Token_Issuer for a Project_Token for one turn (2 s budget). Never throws. */
export function issueProjectToken(userToken: string, projectId: string): Promise<IssueResult> {
  return defaultClient.issueProjectToken(userToken, projectId);
}
