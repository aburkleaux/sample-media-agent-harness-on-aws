// Canonical external-keyspace session-id derivation for the external invoke
// path (POST /invoke/:agentId). See design.md Component 4 (step 6), Component 5,
// Property 12, Property 17, and Requirements 10.1, 19.1, 19.3.
//
// ── Why this exists ───────────────────────────────────────────────────────
// External invocations carry TWO identities. The Owner is the resource actor
// (`actor_id`, drives hooks/jobs/skill-tool-model resolution). The
// issuer-qualified caller `(issuer, sub)` is a SEPARATE `principal`, used only
// as the End_User for memory/session/audit. Per-caller memory and session must
// be scoped to a keyspace that is:
//
//   * a PURE, COLLISION-FREE function of the triple `(agentId, issuer, sub)`
//     — identical triples yield identical keys; any triple differing in
//     `agentId`, `issuer`, OR `sub` yields a distinct key (Property 12);
//   * SEGREGATED under an `EXT#` prefix, DISJOINT from the internal platform
//     actor keyspace `USER#<cognitoSub>` — no `(issuer, sub)` can ever alias an
//     internal `USER#` id (Property 17, Req 19.2/19.3);
//   * NEVER a function of the Owner alone and NEVER a function of `sub` alone —
//     the issuer is always folded in (hashed) so two different IdPs minting the
//     same `sub` produce different keys (Req 10.x, Property 12).
//
// ── Cross-language contract (MUST match agent/src/identity.py, task 12.2) ───
// The runtime (Python Task Agent) derives its AgentCore-Memory actor id,
// namespace, and session id from the SAME triple via
// `identity.derive_external_memory_key`, and it RE-DERIVES that key from the
// stamped `principal` rather than trusting the proxy's `session_id`. So strict
// byte-equality is not required for memory isolation — but we nonetheless make
// the proxy-stamped `session_id` (and the
// `x-amzn-bedrock-agentcore-runtime-session-id` dispatch header) BYTE-FOR-BYTE
// identical to the Python memory key so the runtime session, the durable-memory
// key, and the audit key are one and the same value. The canonical format below
// is the single source of truth for both languages:
//
//     EXT_<sha256(issuer)[:16]>_<sha256(canonical(agentId, issuer, sub, convId))>
//
//   * prefix:        the literal ASCII string  "EXT_"  (was "EXT#"; changed to
//                    `_` because AgentCore Memory rejects `#` in actor/session
//                    ids — `EXT_` is still disjoint from internal `USER#`).
//   * issuer digest: lowercase hex SHA-256 of the RAW issuer string, TRUNCATED
//                    to the first 16 hex characters (64 bits). Kept for
//                    operability/greppability; the collision-free guarantee does
//                    NOT rely on it (it rests on the full-tuple digest below),
//                    and hashing keeps this segment delimiter-free.
//   * tuple digest:  full lowercase hex SHA-256 of the INJECTIVE, length-
//                    prefixed encoding of the tuple (see `canonicalExternalMaterial`),
//                    in the field order  agentId → issuer → sub → conversationId.
//   * delimiter:     the single ASCII character  "_"  between the two segments.
//
// The length-prefixed canonical encoding (`"<len>:<value>"` per component) makes
// the derivation collision-free regardless of a component's contents — including
// the delimiter, empty strings, or a `sub` crafted to look like another tuple —
// because every component is unambiguously delimited by its own length. This is
// strictly more robust than a raw joined form.
//
// `conversationId` is an OPTIONAL 4th component (default "") that sub-partitions a
// single caller's own memory so a "new conversation" starts a fresh thread WITHIN
// the caller's `(agentId, issuer, sub)` space — never crossing callers. An empty
// conversationId reproduces the original 3-component key byte-for-byte.
//
// The Python reproduction (agent/src/identity.py, VERIFIED to match) is:
//
//     import hashlib
//     def _canonical(agent_id, issuer, sub, conv=""):
//         return "".join(f"{len(p)}:{p}" for p in (agent_id, issuer, sub, conv))
//     def derive_external_session_id(agent_id, issuer, sub, conv=""):
//         issuer_hash = hashlib.sha256(issuer.encode("utf-8")).hexdigest()[:16]
//         tuple_hash = hashlib.sha256(_canonical(agent_id, issuer, sub, conv).encode("utf-8")).hexdigest()
//         return f"EXT_{issuer_hash}_{tuple_hash}"
//
// The length prefix counts Unicode CODE POINTS (Python `len(str)`); the TS side
// uses `Array.from(part).length` to count code points identically (not UTF-16
// code units), so the two agree for all inputs, not just ASCII. Any change to
// the algorithm, truncation length, field order, delimiter, prefix, or length
// semantics here MUST be mirrored in identity.py or the proxy-derived session and
// the runtime-derived Memory key will disagree.

import crypto from "crypto";

/** Literal prefix that segregates the external keyspace from internal `USER#`.
 * Uses `_` (not `#`) because AgentCore Memory's actor_id/session_id reject `#`;
 * `EXT_` is still disjoint from internal `USER#` ids. MUST match
 * `identity.EXTERNAL_KEYSPACE_PREFIX`. */
export const EXTERNAL_SESSION_PREFIX = "EXT_";

/** Number of leading lowercase-hex characters of the issuer SHA-256 kept. */
export const ISSUER_HASH_LENGTH = 16;

/** Lowercase-hex SHA-256 of a UTF-8 string. */
const sha256Hex = (value: string): string =>
  crypto.createHash("sha256").update(value, "utf8").digest("hex");

/**
 * Hash an issuer URL to the fixed-width, `#`-free digest segment used in the
 * external session id. Pure; lowercase hex; truncated to
 * {@link ISSUER_HASH_LENGTH} characters. Exported so tests and any Python
 * cross-check can assert the exact digest contract.
 */
export const hashIssuer = (issuer: string): string =>
  sha256Hex(issuer).slice(0, ISSUER_HASH_LENGTH);

/**
 * Injective, length-prefixed encoding of the identity triple, matching Python's
 * `identity._canonical_external_material`. Each component is encoded as
 * `<codePointLen>:<value>` and concatenated in the order agentId → issuer → sub.
 * Because every component is prefixed by its exact code-point length, decoding is
 * unambiguous no matter what characters a component contains, which is what makes
 * the derived key a collision-free function of the whole triple.
 *
 * `Array.from(s).length` counts Unicode code points (like Python `len(str)`),
 * NOT UTF-16 code units, so the encoding is byte-identical to the Python side
 * for all inputs.
 */
const canonicalExternalMaterial = (
  agentId: string,
  issuer: string,
  sub: string,
  conversationId = "",
): string =>
  [agentId, issuer, sub, conversationId]
    .map((p) => `${Array.from(p).length}:${p}`)
    .join("");

/**
 * Derive the canonical external-keyspace session id for a `(agentId, issuer,
 * sub)` triple.
 *
 * This is the CANONICAL, collision-free derivation (Req 10.1, 19.1, 19.3) and is
 * BYTE-FOR-BYTE identical to Python `identity.derive_external_memory_key`, so the
 * proxy-stamped `session_id`, the `x-amzn-bedrock-agentcore-runtime-session-id`
 * dispatch header, and the runtime's durable-memory actor/session/namespace are
 * all the same value.
 *
 * Purity/isolation guarantees (see module header and Properties 12 & 17):
 *   * deterministic in exactly `(agentId, issuer, sub)` — no clock, randomness,
 *     or ambient state;
 *   * always `EXT#`-prefixed and thus disjoint from internal `USER#` ids;
 *   * the issuer is folded into BOTH the issuer-hash segment and the full-triple
 *     digest, so it is never a function of `sub` alone (two issuers minting the
 *     same `sub` yield distinct keys) and never a function of the Owner;
 *   * collision-free across the whole triple via the length-prefixed canonical
 *     encoding, so no `#` in a `sub`/`agentId` can forge another triple's key.
 *
 * @param agentId Custom-Agent lineage id (the `/invoke/:agentId` route param).
 * @param issuer  Validated JWT `iss` (the token issuer URL).
 * @param sub     Validated JWT `sub` (the caller's subject claim).
 * @param conversationId Optional per-conversation nonce that sub-partitions the
 *   caller's own memory (lets "new conversation" start a fresh thread WITHIN
 *   the caller's `(agentId, issuer, sub)` space — never crossing callers).
 *   Empty string (default) reproduces the caller's default-conversation key.
 */
export const deriveExternalSessionId = (
  agentId: string,
  issuer: string,
  sub: string,
  conversationId = "",
): string =>
  `${EXTERNAL_SESSION_PREFIX}${hashIssuer(issuer)}_${sha256Hex(
    canonicalExternalMaterial(agentId, issuer, sub, conversationId),
  )}`;
