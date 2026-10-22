/**
 * `POST /agent-test` — owner-side Custom Agent test invoke (internal realm).
 *
 * Lets an owner run one of their own Custom Agents from the dev portal and see
 * the real response, with the agent's persona / model / tools / skills actually
 * bound. Companion to the Lab `ResolveAgentConfig` action, which answers "what
 * WOULD bind" without spending tokens; this answers "what does it actually do".
 *
 * ## Why a dedicated route rather than reusing `/dispatch`
 *
 * `/dispatch` forwards a caller's body verbatim to the Task Agent with no auth
 * and no field stripping, so a caller controls `tools` (omit it and *every*
 * gateway tool binds), `actor_id`, `system_prompt`, and `model_id`. That route
 * is a tracked Critical finding slated for removal. This one is safe by
 * construction instead:
 *
 *   * It sits behind the Cognito `authPaths` middleware, so `req.actorId` is a
 *     verified subject before the handler runs.
 *   * The caller body is **allowlisted**, not filtered — only `agentId`,
 *     `source`, `version`, `prompt`, and `sessionId` are read, and the outbound
 *     payload is built field by field. Nothing else can reach the runtime, so
 *     the config fields that make `/dispatch` dangerous are unreachable here.
 *   * `actor_id` is stamped from the JWT. A body-supplied `actor_id` is ignored
 *     (it is simply never read), so an authenticated user cannot test *as*
 *     another owner.
 *   * The agent config is resolved **runtime-side** from `agent_id` +
 *     `agent_source`, exactly as the external path resolves from the published
 *     reference. The proxy never sends persona/model/tools content, so the
 *     console cannot diverge from a real invocation — and cannot be used to
 *     inject a config the owner never saved.
 *   * The request body is size-capped. `/dispatch`'s unbounded `Buffer.concat`
 *     is the other half of its finding and is deliberately not reproduced.
 *
 * Ownership is enforced where the data lives: the runtime's resolver keys the
 * draft lookup on the stamped actor's OWNED partition and applies the
 * owner-scope prefix check on published rows, so a non-owner resolves nothing
 * and the invocation proceeds bare rather than leaking another owner's config.
 */

import type { IncomingMessage } from "http";

/** Max accepted request body. Prompts are text; this is generous for that and
 *  small enough that a single 1 GiB proxy task can't be exhausted by one call. */
export const AGENT_TEST_MAX_BODY_BYTES = 128 * 1024;

/** AgentCore requires a runtime session id of at least 33 characters. */
const MIN_SESSION_ID_LEN = 33;

export type AgentTestSource = "draft" | "published";

export interface AgentTestDeps {
  /** Full AgentCore invoke URL for the Task Agent runtime. */
  invokeTaskUrl: string;
  /** SigV4 streaming dispatch (the same helper `/api` and `/invoke` use). */
  signedRequestStream: (
    url: string,
    body: Buffer,
    extraHeaders?: Record<string, string>,
  ) => Promise<IncomingMessage>;
  /** Session-id factory; injected so tests are deterministic. */
  makeSessionId?: () => string;
}

const defaultSessionId = (): string =>
  `agenttest-${Date.now()}-${Math.random().toString(36).slice(2)}`.padEnd(
    MIN_SESSION_ID_LEN,
    "0",
  );

const sendError = (res: any, status: number, code: string, message: string): void => {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: { code, message } }));
};

/**
 * Read the request body with a hard byte cap.
 *
 * Rejects as soon as the cap is exceeded rather than after buffering, so an
 * oversized upload cannot pin proxy memory even transiently.
 */
export function readCappedBody(req: any, maxBytes: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let total = 0;
    let done = false;

    const fail = (err: Error & { code?: string }) => {
      if (done) return;
      done = true;
      req.destroy?.();
      reject(err);
    };

    req.on("data", (c: Buffer) => {
      if (done) return;
      total += c.length;
      if (total > maxBytes) {
        const err = new Error("Request body too large") as Error & { code?: string };
        err.code = "payload_too_large";
        fail(err);
        return;
      }
      chunks.push(c);
    });
    req.on("error", fail);
    req.on("end", () => {
      if (done) return;
      done = true;
      resolve(Buffer.concat(chunks));
    });
  });
}

/**
 * Build the outbound Task Agent payload for an owner test invoke.
 *
 * Exported for testing: this function is the trust boundary. It takes the
 * untrusted caller body plus the verified actor id and returns only
 * proxy-authored fields — every value is either a literal, derived from the
 * verified identity, or one of the five allowlisted caller inputs.
 */
export function buildAgentTestPayload(
  callerBody: any,
  opts: { actorId: string; sessionId: string },
): Record<string, unknown> {
  const agentId = String(callerBody?.agentId ?? "");
  const source: AgentTestSource =
    callerBody?.source === "draft" ? "draft" : "published";
  const prompt = String(callerBody?.prompt ?? "");
  const version =
    typeof callerBody?.version === "number" ? callerBody.version : undefined;

  const payload: Record<string, unknown> = {
    prompt,
    // Identity is proxy-authored. A body-supplied actor_id is never read.
    actor_id: opts.actorId,
    session_id: opts.sessionId,
    // Reference only — the runtime resolves the config itself.
    agent_id: agentId,
    agent_source: source,
    // Explicit opt-in marker the runtime keys the internal resolve on.
    agent_test: true,
  };
  if (source === "published" && version !== undefined) {
    payload.version = version;
  }
  return payload;
}

/** Validate the allowlisted caller inputs. Returns an error message or null. */
export function validateAgentTestBody(callerBody: any): string | null {
  if (!callerBody || typeof callerBody !== "object" || Array.isArray(callerBody)) {
    return "Body must be a JSON object";
  }
  if (!callerBody.agentId || typeof callerBody.agentId !== "string") {
    return "agentId is required";
  }
  if (!callerBody.prompt || typeof callerBody.prompt !== "string") {
    return "prompt is required";
  }
  if (
    callerBody.source !== undefined &&
    callerBody.source !== "draft" &&
    callerBody.source !== "published"
  ) {
    return "source must be 'draft' or 'published'";
  }
  if (
    callerBody.version !== undefined &&
    (typeof callerBody.version !== "number" || !Number.isInteger(callerBody.version))
  ) {
    return "version must be an integer";
  }
  return null;
}

/**
 * Create the `POST /agent-test` handler.
 *
 * DI-shaped (like `invoke/invokeRoute.ts`) so the auth/stripping/cap behavior
 * is unit-testable without AWS or a full `buildApp()`.
 */
export function createAgentTestHandler(deps: AgentTestDeps) {
  const { invokeTaskUrl, signedRequestStream, makeSessionId = defaultSessionId } = deps;

  return async function agentTestHandler(req: any, res: any): Promise<void> {
    if (req.method !== "POST") {
      sendError(res, 405, "method_not_allowed", "Use POST");
      return;
    }
    if (!invokeTaskUrl) {
      sendError(res, 500, "not_configured", "TASK_AGENT_RUNTIME_ARN is not configured");
      return;
    }

    // The Cognito middleware runs first (this path is in `authPaths`), so a
    // missing actorId means the route was mounted without it — fail closed
    // rather than invoking with an unattributed identity.
    const actorId: string | undefined = req.actorId;
    if (!actorId) {
      sendError(res, 401, "missing_token", "Authentication is required");
      return;
    }

    let raw: Buffer;
    try {
      raw = await readCappedBody(req, AGENT_TEST_MAX_BODY_BYTES);
    } catch (err: any) {
      if (err?.code === "payload_too_large") {
        sendError(res, 413, "payload_too_large", "Request body too large");
      } else {
        sendError(res, 400, "bad_request", "Could not read request body");
      }
      return;
    }

    let callerBody: any;
    try {
      callerBody = JSON.parse(raw.toString("utf8") || "{}");
    } catch {
      sendError(res, 400, "bad_request", "Body must be valid JSON");
      return;
    }

    const invalid = validateAgentTestBody(callerBody);
    if (invalid) {
      sendError(res, 400, "bad_request", invalid);
      return;
    }

    const sessionId =
      typeof callerBody.sessionId === "string" &&
      callerBody.sessionId.length >= MIN_SESSION_ID_LEN
        ? callerBody.sessionId
        : makeSessionId();

    const outbound = Buffer.from(
      JSON.stringify(buildAgentTestPayload(callerBody, { actorId, sessionId })),
      "utf8",
    );

    let upstream: IncomingMessage;
    try {
      upstream = await signedRequestStream(invokeTaskUrl, outbound, {
        "x-amzn-bedrock-agentcore-runtime-session-id": sessionId,
      });
    } catch (err: any) {
      console.error("[agentcore-proxy] /agent-test dispatch error:", err?.message ?? err);
      sendError(res, 502, "dispatch_failed", "Could not reach the agent runtime");
      return;
    }

    res.writeHead(upstream.statusCode ?? 502, {
      "content-type": upstream.headers["content-type"] ?? "text/event-stream",
      "cache-control": "no-cache",
      "x-accel-buffering": "no",
    });
    upstream.on("data", (chunk: Buffer) => res.write(chunk));
    upstream.on("end", () => res.end());
    upstream.on("error", (err: Error) => {
      console.error("[agentcore-proxy] /agent-test stream error:", err.message);
      res.end();
    });
    // A client disconnect must tear down the upstream, or the runtime keeps
    // streaming into a dead socket (same contract as `/api`).
    req.on("close", () => upstream.destroy?.());
  };
}
