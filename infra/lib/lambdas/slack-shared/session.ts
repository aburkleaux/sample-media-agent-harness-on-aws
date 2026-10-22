// Deterministic per-thread Slack session id.
// Design: Property 1 (determinism + isolation), Reqs 2.1–2.3.

import { createHash } from "node:crypto";

const SESSION_PREFIX = "slack-";
type SessionTuple = readonly [string, string, string, string];

function lengthPrefix(fields: SessionTuple): Buffer {
  const parts: Buffer[] = [];
  for (const field of fields) {
    const bytes = Buffer.from(field, "utf8");
    const len = Buffer.allocUnsafe(4);
    len.writeUInt32BE(bytes.length, 0);
    parts.push(len, bytes);
  }
  return Buffer.concat(parts);
}

export function deriveSlackSessionId(
  teamId: string,
  channelId: string,
  rootThreadTs: string,
  sessionSalt: string,
): string {
  for (const [name, value] of [
    ["teamId", teamId],
    ["channelId", channelId],
    ["rootThreadTs", rootThreadTs],
    ["sessionSalt", sessionSalt],
  ] as const) {
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`deriveSlackSessionId: ${name} must be a non-empty string`);
    }
  }
  const digest = createHash("sha256")
    .update(lengthPrefix([teamId, channelId, rootThreadTs, sessionSalt]))
    .digest("hex");
  return SESSION_PREFIX + digest;
}
