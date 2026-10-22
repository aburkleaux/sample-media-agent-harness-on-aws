import { createHash } from "node:crypto";

/**
 * Derive a stable AgentCore runtime session id for a `/lab` CRUD request.
 *
 * A `/lab` action is a pure DynamoDB read, but it travels through a runtime
 * invoke. Measured against sandbox: a cold session costs ~8.5s, a reused one
 * ~190ms. The 8.3s is one-time container init — `_init_agent_subsystems()` and
 * `_ensure_gateway_connected()` both run *before* `main.py`'s Lab bypass, so a
 * request needing zero tools still builds the Bedrock model and completes an
 * MCP handshake. Pinning a session lets consecutive Lab calls reuse the warm
 * microVM instead of paying that each time.
 *
 * **Per actor, never global.** `agent/src/main.py` binds the gateway
 * connection's `actor_id` once per container, on the reasoning that "a session
 * belongs to one user, so this container serves one actor for its lifetime". A
 * session id shared between actors would therefore hand one user a container
 * whose gateway connection asserts a *different* actor — the cross-actor tool
 * leak the gateway interceptor exists to prevent. The actor id is the only
 * input for exactly that reason.
 *
 * Returns `""` when there is no actor, which makes the caller omit the header
 * and keeps today's unpinned behavior rather than inventing a shared session.
 */
export function labRuntimeSessionId(actorId: string | undefined | null): string {
  if (!actorId) return "";
  // SHA-256 hex is 64 chars — comfortably over the runtime's 33-char floor —
  // and keeps the raw actor id (an email address) out of a header that lands in
  // AWS logs and traces. `lab-` namespaces it away from the `render-` sessions
  // minted on the render route.
  const digest = createHash("sha256").update(actorId, "utf8").digest("hex");
  return `lab-${digest}`;
}
