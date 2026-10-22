// artifactOwner.ts — owner tags for artifact keys that have no session to own them.
//
// Most artifact keys are owned through their session (`artifacts/<sid>/…` → the session row's
// `actorId`). Two kinds of write have no session row to point at, and used to land in namespaces
// every signed-in user could read:
//
//   * a SESSION-LESS upload (`POST /upload` before the chat's row exists, or any external
//     `POST /invoke/:agentId/uploads`), which went to `artifacts/uploads/input/<ts>-<name>`;
//   * a SESSION-LESS render (`POST /render` with no session the caller owns), whose Render Agent
//     session was a random `render-<ts>-<rand>`.
//
// Both now carry an OWNER TAG in the key — `artifacts/uploads/<tag>/input/…` and
// `artifacts/render-<tag>-…/…` — so the read policy (`authorizeArtifactKey`) can answer "is this the
// caller's?" from the key alone, with no lookup and no state to keep in step. The tag is a
// one-way hash, so a key never reveals the actor's id.
//
//   * `u<32 hex>` — an internal (Cognito) actor: `artifactOwnerTag(actorId)`.
//   * `x<32 hex>` — an external caller of a Custom Agent, per (agent, issuer, sub):
//     `externalArtifactOwnerTag(agentId, issuer, sub)`. Never equal to any `u…` tag, so an internal
//     user can never read an external caller's upload, nor the reverse.

import { createHash } from "crypto";

const TAG_HEX = 32;

/** An internal actor's tag: `u` + 32 hex. */
export const ACTOR_OWNER_TAG_RE = /^u[0-9a-f]{32}$/;
/** An external caller's tag: `x` + 32 hex. */
export const EXTERNAL_OWNER_TAG_RE = /^x[0-9a-f]{32}$/;

function digest(domain: string, material: string): string {
  return createHash("sha256").update(`${domain}\0${material}`, "utf8").digest("hex").slice(0, TAG_HEX);
}

/** The owner tag of an internal actor (the verified Cognito `sub` the proxy calls `actorId`). */
export function artifactOwnerTag(actorId: string): string {
  if (typeof actorId !== "string" || !actorId) throw new Error("artifactOwnerTag needs an actor id");
  return `u${digest("artifact-owner", actorId)}`;
}

/** The owner tag of an external caller, stable across that caller's conversations with one agent. */
export function externalArtifactOwnerTag(agentId: string, issuer: string, sub: string): string {
  if (!agentId || !issuer || !sub) throw new Error("externalArtifactOwnerTag needs agentId, issuer and sub");
  // Length-prefixed so no `|` inside a field can forge another triple's material.
  const material = [agentId, issuer, sub].map((s) => `${s.length}:${s}`).join("|");
  return `x${digest("artifact-owner-ext", material)}`;
}

/** The Render Agent session id for a render that has no session the caller owns. >= 33 chars. */
export function ownerTaggedRenderSession(actorId: string, now: number = Date.now()): string {
  const rand = Math.random().toString(36).slice(2, 10).padEnd(8, "0");
  return `render-${artifactOwnerTag(actorId)}-${now.toString(36)}${rand}`;
}

const RENDER_SESSION_RE = /^render-(u[0-9a-f]{32})-[a-z0-9]+$/;

/** The owner tag a `render-<tag>-…` session id carries, else null (legacy `render-<ts>-…` ids carry none). */
export function ownerTagOfRenderSession(sessionId: string): string | null {
  const m = RENDER_SESSION_RE.exec(sessionId);
  return m ? m[1] : null;
}
