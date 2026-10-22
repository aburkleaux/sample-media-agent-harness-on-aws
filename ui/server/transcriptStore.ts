// Authoritative transcript persistence (proxy-side).
//
// docs/design-proxy-authoritative-transcript.md: the FULL rich conversation lives in S3
// (artifacts/<sessionId>/transcript.json) as the source of truth, and a lean, byte-capped TAIL is
// mirrored onto the DynamoDB session row for the fast session-list preview + instant paint. This
// mirrors the existing renders design (S3 manifests authoritative, row holds lean refs), so a long
// conversation can never hit DynamoDB's 400 KB item limit and no turn is lost when the browser
// navigates away mid-stream.
//
// This module is the WRITER only (P1, dark behind TRANSCRIPT_AUTHORITY=proxy). The read-flip
// (GET /sessions/:id/transcript reads S3-first) lands in P2.

import { GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";
import type { S3Client } from "@aws-sdk/client-s3";
import { UpdateCommand } from "@aws-sdk/lib-dynamodb";
import type { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";
import type { WireStep, WireSummary } from "./transcriptAccumulator";

/** A persisted chat message. Mirrors the client ChatMessage (ui/src/lab-api.ts) + a turnId for idempotency. */
export interface StoredMessage {
  id: string;
  /** `action`: a direct timeline action (Frame / Clip / Crop) the user ran — recorded, never sent as a turn. */
  role: "user" | "assistant" | "action";
  /** The action line's record (mediaActions.ts `ActionLineRecord`), on `role: "action"` only. */
  action?: unknown;
  content: string;
  steps?: WireStep[];
  summary?: WireSummary | null;
  error?: string;
  timestamp: number;
  attachment?: { url: string; name: string };
  /** Every file attached to a user turn, in order (`attachment` is the first). */
  attachments?: { url: string; name: string }[];
  /** Correlates the user+assistant pair of one turn; used to dedup a re-finalized turn. */
  turnId?: string;
}

interface TranscriptDoc {
  version: 1;
  sessionId: string;
  messages: StoredMessage[];
}

const transcriptKey = (sessionId: string) => `artifacts/${sessionId}/transcript.json`;

/**
 * Strip model-only prompt augmentation so a persisted USER turn shows what the user typed, not the
 * wire prompt. Mirrors ui/src/promptNormalize.ts:normalizeUserTurnContent (kept in sync by test).
 * Used only as a fallback when the client did not send a clean display_text.
 */
export function normalizeUserPrompt(text: string): string {
  if (typeof text !== "string") return "";
  return text
    .replace(/^(?:\s*\[File attached:[\s\S]*?\]\s*)+/i, "") // one block per attached file
    .replace(/^(?:\s*\uD83D\uDCCE[^\n]*\n?)+/, "") // one 📎 chip line per attached file
    .replace(/\s*\[sources?:[^\]]*\]\s*$/i, "")
    .replace(/\s*\n+\[Current date:\s*\d{4}-\d{2}-\d{2}\]\s*$/i, "")
    .trim();
}

async function readTranscriptDoc(s3: S3Client, bucket: string, sessionId: string): Promise<TranscriptDoc> {
  try {
    const obj = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: transcriptKey(sessionId) }));
    const body = await obj.Body?.transformToString();
    if (body) {
      const parsed = JSON.parse(body) as Partial<TranscriptDoc>;
      if (parsed && Array.isArray(parsed.messages)) {
        return { version: 1, sessionId, messages: parsed.messages as StoredMessage[] };
      }
    }
  } catch {
    // No object yet (first turn) or unreadable — start fresh. A single unreadable transcript must
    // not wedge the session; the DDB tail still carries recent turns.
  }
  return { version: 1, sessionId, messages: [] };
}

/**
 * Read the authoritative FULL transcript from S3 for the read path (GET /sessions/:id/transcript).
 * Returns null when there is no S3 object yet (so the caller can fall back to the DynamoDB row for
 * old sessions written before the proxy became authoritative). Distinct from readTranscriptDoc,
 * which returns an empty doc so the writer can start appending.
 */
export async function readTranscriptFromS3(
  s3: S3Client, bucket: string, sessionId: string,
): Promise<StoredMessage[] | null> {
  try {
    const obj = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: transcriptKey(sessionId) }));
    const body = await obj.Body?.transformToString();
    if (!body) return null;
    const parsed = JSON.parse(body) as Partial<TranscriptDoc>;
    return parsed && Array.isArray(parsed.messages) ? (parsed.messages as StoredMessage[]) : null;
  } catch {
    return null; // no object / unreadable → caller falls back to the DDB row
  }
}

/** `messages` sorted by timestamp, stably; one without a timestamp sorts as the one before it. */
export function orderByTime(messages: StoredMessage[]): StoredMessage[] {
  let last = -Infinity;
  const keyed = messages.map((m, i) => {
    const t = typeof (m as { timestamp?: unknown }).timestamp === "number" ? (m as { timestamp: number }).timestamp : last;
    last = t;
    return { m, t, i };
  });
  keyed.sort((a, b) => (a.t - b.t) || (a.i - b.i));
  return keyed.map((k) => k.m);
}

export interface FinalizeTurnArgs {
  s3: S3Client;
  ddb: DynamoDBDocumentClient;
  uploadBucket: string;
  sessionsTable: string;
  sessionId: string;
  turnId: string;
  /** The user turn to record (already display-clean if the client sent display_text). */
  userTurn?: StoredMessage;
  /** The assistant turn to record (may be omitted if the turn produced nothing). */
  assistantTurn?: StoredMessage;
  /** byteCapTranscript from app.ts — injected to avoid a circular import. */
  byteCap: (transcript: unknown) => unknown[];
}

/**
 * UPSERT one turn (user + assistant) into the authoritative S3 transcript by turnId, and mirror a
 * lean capped tail onto the DynamoDB row. Upsert (not append) so that:
 *   - a re-finalize (end + error both firing, or a retry) does not double-write, and
 *   - a throttled PROVISIONAL write during a long stream is REPLACED by the final one on completion
 *     (both share the turnId), preserving mid-turn-reload recovery without duplicating the turn.
 * Best-effort; the caller logs failures and never fails the invoke.
 */
export async function finalizeTurn(args: FinalizeTurnArgs): Promise<void> {
  const { s3, ddb, uploadBucket, sessionsTable, sessionId, turnId, userTurn, assistantTurn, byteCap } = args;
  if (!userTurn && !assistantTurn) return;

  const doc = await readTranscriptDoc(s3, uploadBucket, sessionId);
  // Replace any prior messages for this turn (a provisional write, or a duplicate finalize), then
  // append this turn's messages — keeps one copy per turn regardless of how many times we write.
  if (turnId) doc.messages = doc.messages.filter((m) => m.turnId !== turnId);
  if (userTurn) doc.messages.push(userTurn);
  if (assistantTurn) doc.messages.push(assistantTurn);
  // Keep the document in time order: a turn is written when it ENDS, so a timeline action made while it ran was
  // stored ahead of the turn's own question (session 25d42705). Stable; a message with no timestamp keeps its place.
  doc.messages = orderByTime(doc.messages);

  await s3.send(new PutObjectCommand({
    Bucket: uploadBucket,
    Key: transcriptKey(sessionId),
    Body: JSON.stringify(doc),
    ContentType: "application/json",
  }));

  // Lean capped tail on the row (same cap the client-driven PATCH uses today). No lock guard: a
  // locked session is rejected before the runtime is invoked, so finalize never runs for one.
  const now = new Date().toISOString();
  // Only an existing, live row: an unconditioned update recreated a partial ownerless row for an unknown id
  // and kept writing to a session the user had deleted (so it lived on, and its 30-day TTL then wiped it).
  try {
    await ddb.send(new UpdateCommand({
      TableName: sessionsTable,
      Key: { sessionId },
      UpdateExpression: "SET transcript = :tr, lastActivityAt = :now",
      ConditionExpression: "attribute_exists(sessionId) AND (attribute_not_exists(active) OR active = :one)",
      ExpressionAttributeValues: { ":tr": byteCap(doc.messages), ":now": now, ":one": 1 },
    }));
  } catch (err: any) {
    if (err?.name === "ConditionalCheckFailedException") return; // deleted or never created: nothing to keep
    throw err;
  }
}

/** Build the two StoredMessages for a completed turn from the accumulated stream + request fields. */
export function buildTurnMessages(opts: {
  turnId: string;
  prompt: string;
  displayText?: string;
  attachment?: { url: string; name: string };
  attachments?: { url: string; name: string }[];
  text: string;
  steps: WireStep[];
  summary: WireSummary | null;
  error?: string;
  startedAt: number;
}): { userTurn: StoredMessage; assistantTurn?: StoredMessage } {
  const { turnId, prompt, displayText, attachment, attachments, text, steps, summary, error, startedAt } = opts;
  const userContent = typeof displayText === "string" && displayText.length > 0
    ? displayText
    : normalizeUserPrompt(prompt);
  // IDs derive from turnId so they MATCH the client's on-screen/cached message ids (`<turnId>-u` /
  // `<turnId>-a`); mergeSessionMessages dedups by id on reload, so the client copy and this durable
  // copy of a turn collapse instead of rendering twice.
  const userTurn: StoredMessage = {
    id: `${turnId}-u`,
    role: "user",
    content: userContent,
    timestamp: startedAt,
    turnId,
    ...(attachment ? { attachment } : {}),
    ...(attachments && attachments.length > 0 ? { attachments } : {}),
  };
  const hasAssistant = !!text || steps.length > 0 || !!summary || !!error;
  const assistantTurn: StoredMessage | undefined = hasAssistant
    ? {
        id: `${turnId}-a`,
        role: "assistant",
        content: text || (error ? "" : "Stopped."),
        timestamp: startedAt + 1,
        turnId,
        ...(steps.length > 0 ? { steps } : {}),
        ...(summary ? { summary } : {}),
        ...(error ? { error } : {}),
      }
    : undefined;
  return { userTurn, assistantTurn };
}
