// Owner-scoped execution identity stamping for the external invoke path
// (POST /invoke/:agentId). See design.md Property 8 and Requirements 6.1,
// 6.2, 6.3, 6.5, 6.6, 19.1, 19.2.
//
// This is the pure seam extracted from `ui/server/app.ts` so the two-identity
// stamping rule can be property-tested in isolation. The rule:
//
//   * The Owner is the resource actor: `actor_id` is stamped to the Custom
//     Agent Owner's platform user id (the field `task_agent.invoke()` reads),
//     never a caller-supplied value and never the JWT `sub`.
//   * The issuer-qualified Principal `(issuer, sub)` is stamped as a SEPARATE
//     `principal` field, used only as the End_User for memory/session/audit,
//     and is never resolved as the resource actor.
//   * No caller-supplied input may override the stamped identity: only the
//     allowlisted caller fields (`CALLER_ALLOWED_FIELDS`, today just `prompt`)
//     survive; everything else is dropped before the proxy stamps its own values.
//
// The resulting resource actor is therefore a pure function of the resolved
// Owner, independent of any caller-supplied input.

/**
 * Caller-supplied fields that must never survive from the request body — the
 * proxy is the only writer of identity/session fields after edge JWT
 * verification (Req 6.6, 19.1). Includes both the fields the Task Agent reads
 * (`actor_id`, `actor_groups`, `principal`, `session_id`) and the legacy
 * double-underscore aliases the main agent historically honored
 * (`__actor_id`, `__actor_groups`) so a caller cannot smuggle identity via
 * either spelling.
 */
export const RESERVED_IDENTITY_FIELDS = [
  "actor_id",
  "actor_groups",
  "principal",
  "session_id",
  "__actor_id",
  "__actor_groups",
] as const;

/**
 * Caller-supplied CONFIG fields that must never survive either — the runtime
 * interprets these as execution configuration (persona, model, skills, tool
 * allowlist, resolved custom-agent config). On the external path the ONLY
 * authority for configuration is the published version referenced by
 * `agent_id`/`version`, resolved server-side by the Task Agent (custom-agents
 * seam fix). Letting any of these flow from the caller body would let an
 * external caller override the Owner's published config — including the
 * external tool opt-in (a confused-deputy hole, Req 11.1/6.6).
 */
export const RESERVED_CONFIG_FIELDS = [
  "custom_agent",
  "system_prompt",
  "model_id",
  "skills",
  "tools",
  "tool_collections",
  "agent_id",
  "version",
  // Plan-execution context (internal DAG dispatch only). `node_id` flips the
  // Task Agent into its plan-output mode: the answer is written to S3 and the
  // caller gets an `output_ref` it cannot fetch, plus a stray plan-state row.
  // Worse, `s3_bucket`/`ddb_table` are caller-overridable RESOURCE NAMES the
  // runtime would write to with its own IAM role. External callers must never
  // steer any of the three.
  "node_id",
  "s3_bucket",
  "ddb_table",
] as const;

/**
 * The ONLY caller-supplied fields that reach the runtime (an ALLOWLIST).
 *
 * The two lists above used to be the whole defence: a DENYLIST of the fields someone had thought of.
 * The Task Agent trusts many more than those — `agent_test`, `agent_source`, `delegated`, `plan_id`,
 * `user_token`, `delegation_grant`, `project_scope`, `external_inputs`, `producing_project_id`,
 * `__plan_node`, `__plan_session_id`... — and every new payload key it learns to read would have been
 * caller-controllable on this path by default. So the rule is inverted: a caller sends a PROMPT, and
 * everything else on the outbound payload is written by the proxy. `conversation_id` is consumed by
 * the route (sanitized into the session key and `principal`), never forwarded raw.
 *
 * Adding a field here makes it caller-controlled on the external surface: only add a field the Task
 * Agent treats as untrusted DATA, never one that selects identity, config, resources or a code path.
 */
export const CALLER_ALLOWED_FIELDS = ["prompt"] as const;

export interface StampIdentityContext {
  /** Route param — the Custom Agent lineage id (published-id). */
  agentId: string;
  /** Resolved resource actor: the Custom Agent Owner's platform user id. */
  ownerUserId: string;
  /** Resolved Owner groups (may be empty). */
  ownerGroups: string[];
  /** Issuer-qualified Principal: the token issuer URL. */
  issuer: string;
  /** Issuer-qualified Principal: the token subject claim. */
  sub: string;
  /** Segregated external-keyspace session id derived from (agentId, issuer, sub[, conversationId]). */
  sessionId: string;
  /** Optional per-conversation nonce (sanitized) folded into the session/memory key. */
  conversationId?: string;
  /** The resolved current-published AssetIndex row for the agent. */
  agentRow: Record<string, any>;
}

/**
 * Stamp the two-identity invocation payload for an external invoke dispatch.
 *
 * `callerBody` is the (already JSON-parsed) caller request body. Any non-object
 * / array / nullish value is treated as an empty body. All reserved identity
 * fields are stripped before the proxy stamps its own values, so no caller
 * input can override the stamped resource actor or principal.
 *
 * The returned object is the outbound payload SigV4-dispatched to the shared
 * Task Agent runtime.
 */
export function stampInvocationIdentity(
  callerBody: unknown,
  ctx: StampIdentityContext,
): Record<string, any> {
  const { agentId, ownerUserId, ownerGroups, issuer, sub, sessionId, conversationId, agentRow } = ctx;

  // Coerce the caller body to a plain object; anything else → empty body. Then keep ONLY the
  // allowlisted fields (see CALLER_ALLOWED_FIELDS): identity, session, config, resource names and
  // plan/delegation context are all proxy-written, whatever the caller sent. The reserved lists
  // above are a subset of what this drops, kept as named documentation (and for their tests).
  const callerPayload: Record<string, any> = {};
  if (callerBody && typeof callerBody === "object" && !Array.isArray(callerBody)) {
    const src = callerBody as Record<string, any>;
    for (const field of CALLER_ALLOWED_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(src, field)) callerPayload[field] = src[field];
    }
  }

  const outbound: Record<string, any> = {
    ...callerPayload,
    // --- Resource actor = Owner (Req 6.1, 6.2) ---
    actor_id: ownerUserId,
    actor_groups: ownerGroups,
    // --- Principal = issuer-qualified Caller, End_User only (Req 6.3, 19.1) ---
    // `conversationId` (when present) travels with the principal so the runtime
    // re-derives the SAME memory key the proxy used for `session_id`. It is a
    // sub-partition of the caller's own space, never an identity claim.
    principal: conversationId
      ? { issuer, sub, conversation_id: conversationId }
      : { issuer, sub },
    session_id: sessionId,
    // --- Published-version REFERENCE (not content) ---
    // The Task Agent resolves the referenced config itself (persona, skill
    // defs, tool collections, external opt-in) from the AssetIndex + lab
    // artifacts bucket, Owner-scoped via `actor_id`. The proxy deliberately
    // stamps no config content: it has no S3 access to lab assets, and a
    // single reference keeps the authorized version and the executed config
    // impossible to desynchronize.
    agent_id: agentId,
    version: agentRow.version ?? null,
  };

  return outbound;
}
