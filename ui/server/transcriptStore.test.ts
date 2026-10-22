import { describe, it, expect, vi } from "vitest";
import {
  normalizeUserPrompt,
  buildTurnMessages,
  finalizeTurn,
  readTranscriptFromS3,
  type StoredMessage,
} from "./transcriptStore";
import { normalizeUserTurnContent } from "../src/promptNormalize";
import type { CitationBlock } from "../src/api";

// normalizeUserPrompt (server) MUST stay in lockstep with the client's normalizeUserTurnContent, so
// a proxy-persisted user turn matches what the UI shows. This asserts parity across the augmentation
// shapes the agent/UI produce.
describe("normalizeUserPrompt parity with client normalizeUserTurnContent", () => {
  const cases = [
    "plain message",
    "do X\n\n[Current date: 2026-09-02]",
    "trim this [source: https://cf/x.mp4]",
    "extract frames [sources: a=https://cf/x.mp4 @0s]",
    "[File attached: clip.mp4 (1 MB)\nS3 key: k\nFull S3 URI: s3://b/k]\n\ntranscribe this",
    "\uD83D\uDCCE clip.mp4 (12.3 MB)\ntranscribe this",
    // Multi-file turns: one block and one chip line per attached file.
    "[File attached: a.mp4 (1 MB)\nS3 key: k1]\n\n[File attached: b.pdf (2 MB)\nS3 key: k2]\n\nsummarize both",
    "\uD83D\uDCCE a.mp4 (1.0 MB)\n\uD83D\uDCCE b.pdf (2.0 MB)\nsummarize both",
    "  spaced  ",
  ];
  for (const c of cases) {
    it(`matches for ${JSON.stringify(c.slice(0, 30))}`, () => {
      expect(normalizeUserPrompt(c)).toBe(normalizeUserTurnContent(c));
    });
  }
});

describe("buildTurnMessages", () => {
  it("prefers a clean display_text for the user turn", () => {
    const { userTurn } = buildTurnMessages({
      turnId: "t1", prompt: "seg [source: https://x]", displayText: "seg", text: "ok", steps: [], summary: null, startedAt: 100,
    });
    expect(userTurn.content).toBe("seg");
    expect(userTurn.role).toBe("user");
    expect(userTurn.turnId).toBe("t1");
  });

  it("falls back to normalizeUserPrompt when no display_text", () => {
    const { userTurn } = buildTurnMessages({
      turnId: "t1", prompt: "seg [source: https://x]", text: "ok", steps: [], summary: null, startedAt: 100,
    });
    expect(userTurn.content).toBe("seg");
  });

  it("carries an attachment onto the user turn", () => {
    const { userTurn } = buildTurnMessages({
      turnId: "t1", prompt: "p", attachment: { url: "https://cf/a.mp4", name: "a.mp4" },
      text: "ok", steps: [], summary: null, startedAt: 1,
    });
    expect(userTurn.attachment).toEqual({ url: "https://cf/a.mp4", name: "a.mp4" });
  });

  it("carries every attachment of a multi-file turn", () => {
    const refs = [{ url: "https://cf/a.mp4", name: "a.mp4" }, { url: "https://cf/b.pdf", name: "b.pdf" }];
    const { userTurn } = buildTurnMessages({
      turnId: "t1", prompt: "p", attachment: refs[0], attachments: refs,
      text: "ok", steps: [], summary: null, startedAt: 1,
    });
    expect(userTurn.attachment).toEqual(refs[0]);
    expect(userTurn.attachments).toEqual(refs);
  });

  it("builds an assistant turn with text, steps and summary", () => {
    const { userTurn, assistantTurn } = buildTurnMessages({
      turnId: "t1", prompt: "p", text: "answer",
      steps: [{ type: "step", status: "done", label: "X" }],
      summary: { type: "execution_summary", total_duration_ms: 5 }, startedAt: 10,
    });
    // IDs derive from turnId so the client's cached copy and this durable copy dedup by id on reload.
    expect(userTurn.id).toBe("t1-u");
    expect(assistantTurn?.id).toBe("t1-a");
    expect(assistantTurn?.content).toBe("answer");
    expect(assistantTurn?.steps).toHaveLength(1);
    expect(assistantTurn?.summary?.total_duration_ms).toBe(5);
    expect(assistantTurn?.timestamp).toBe(11);
  });

  it("omits the assistant turn when the turn produced nothing", () => {
    const { assistantTurn } = buildTurnMessages({
      turnId: "t1", prompt: "p", text: "", steps: [], summary: null, startedAt: 10,
    });
    expect(assistantTurn).toBeUndefined();
  });

  it("records an error turn (empty content, error field)", () => {
    const { assistantTurn } = buildTurnMessages({
      turnId: "t1", prompt: "p", text: "", steps: [], summary: null, error: "Connection error", startedAt: 10,
    });
    expect(assistantTurn?.error).toBe("Connection error");
    expect(assistantTurn?.content).toBe("");
  });
});

// Fake S3/DDB to exercise finalizeTurn's read-modify-write + idempotency without AWS.
function makeFakes(existing?: StoredMessage[]) {
  const store: { body: string | null } = { body: existing ? JSON.stringify({ version: 1, sessionId: "s", messages: existing }) : null };
  const puts: any[] = [];
  const ddbUpdates: any[] = [];
  const s3 = {
    send: vi.fn(async (cmd: any) => {
      const name = cmd.constructor.name;
      if (name === "GetObjectCommand") {
        if (store.body === null) throw new Error("NoSuchKey");
        return { Body: { transformToString: async () => store.body } };
      }
      if (name === "PutObjectCommand") { store.body = cmd.input.Body; puts.push(cmd.input); return {}; }
      return {};
    }),
  } as any;
  const ddb = { send: vi.fn(async (cmd: any) => { ddbUpdates.push(cmd.input); return {}; }) } as any;
  return { s3, ddb, store, puts, ddbUpdates };
}

const byteCap = (t: unknown) => (Array.isArray(t) ? t.slice(-100) : []);

describe("finalizeTurn", () => {
  const base = { uploadBucket: "b", sessionsTable: "tbl", sessionId: "s", byteCap };

  it("creates the transcript on the first turn and mirrors the DDB tail", async () => {
    const { s3, ddb, store, ddbUpdates } = makeFakes();
    await finalizeTurn({
      ...base, s3, ddb, turnId: "t1",
      userTurn: { id: "u", role: "user", content: "hi", timestamp: 1, turnId: "t1" },
      assistantTurn: { id: "a", role: "assistant", content: "yo", timestamp: 2, turnId: "t1" },
    });
    const doc = JSON.parse(store.body!);
    expect(doc.messages.map((m: StoredMessage) => m.content)).toEqual(["hi", "yo"]);
    expect(ddbUpdates).toHaveLength(1);
    expect(ddbUpdates[0].ExpressionAttributeValues[":tr"]).toHaveLength(2);
    // Only an existing, live row: never upsert an ownerless row or write to a deleted session.
    expect(ddbUpdates[0].ConditionExpression).toContain("attribute_exists(sessionId)");
    expect(ddbUpdates[0].ConditionExpression).toContain("active = :one");
  });

  it("a deleted or unknown session is a quiet no-op, not an error", async () => {
    const { s3 } = makeFakes();
    const ddb = { send: vi.fn(async () => {
      const e: any = new Error("conditional"); e.name = "ConditionalCheckFailedException"; throw e;
    }) } as any;
    await expect(finalizeTurn({
      ...base, s3, ddb, turnId: "t1",
      userTurn: { id: "u", role: "user", content: "hi", timestamp: 1, turnId: "t1" },
      assistantTurn: { id: "a", role: "assistant", content: "yo", timestamp: 2, turnId: "t1" },
    })).resolves.toBeUndefined();
  });

  it("appends to an existing transcript", async () => {
    const { s3, ddb, store } = makeFakes([{ id: "u0", role: "user", content: "old", timestamp: 0, turnId: "t0" }]);
    await finalizeTurn({
      ...base, s3, ddb, turnId: "t1",
      userTurn: { id: "u", role: "user", content: "new", timestamp: 1, turnId: "t1" },
    });
    const doc = JSON.parse(store.body!);
    expect(doc.messages.map((m: StoredMessage) => m.content)).toEqual(["old", "new"]);
  });

  it("is idempotent per turnId (no double-append on re-finalize)", async () => {
    const { s3, ddb, store } = makeFakes();
    const turn = {
      ...base, s3, ddb, turnId: "t1",
      userTurn: { id: "u", role: "user" as const, content: "hi", timestamp: 1, turnId: "t1" },
    };
    await finalizeTurn(turn);
    await finalizeTurn(turn); // e.g. end + error both fired
    const doc = JSON.parse(store.body!);
    expect(doc.messages).toHaveLength(1);
  });

  it("does nothing when there is neither a user nor assistant turn", async () => {
    const { s3, ddb, ddbUpdates } = makeFakes();
    await finalizeTurn({ ...base, s3, ddb, turnId: "t1" });
    expect(ddbUpdates).toHaveLength(0);
    expect(s3.send).not.toHaveBeenCalled();
  });
});

// media-harness-search-qa-rendering Req 2.6, 2.12: a turn-end `citations` block rides on the
// execution_summary and is persisted verbatim on the stored assistant message. It carries typed
// fields only (no URL), and classification is final once stored — nothing re-verifies it. This
// asserts a summary carrying `citations` survives buildTurnMessages → finalizeTurn (S3 write) →
// readTranscriptFromS3 (S3 read) byte-for-byte, including both the verified and unverified shapes.
describe("citations round-trip through buildTurnMessages/finalizeTurn/readTranscriptFromS3", () => {
  const base = { uploadBucket: "b", sessionsTable: "tbl", sessionId: "s", byteCap };
  const citations: CitationBlock = {
    v: 1,
    project_id: "prj-123",
    truncated: false,
    items: [
      {
        key: "c1",
        text: "She says the budget doubled.",
        display_name: "interview.mp4 @01:02–01:05",
        start_ms: 62000,
        end_ms: 65000,
        verified: true,
        ref: { project_id: "prj-123", asset_id: "ast-9", frame_s3_key: null },
        kind: "video",
        asset_display_name: "interview.mp4",
      },
      {
        key: "c2",
        text: "An unmatched claim.",
        display_name: "unknown",
        start_ms: 0,
        end_ms: 0,
        verified: false,
        reason: "no_matching_hit",
      },
    ],
  };

  it("persists and reads back the summary.citations unchanged", async () => {
    const summary = {
      type: "execution_summary" as const,
      total_duration_ms: 1234,
      cycles: 2,
      citations,
    };
    const { userTurn, assistantTurn } = buildTurnMessages({
      turnId: "t1", prompt: "what did she say about the budget?", displayText: "what did she say about the budget?",
      text: "She says the budget doubled.", steps: [], summary, startedAt: 100,
    });
    // The summary (with its citations) is carried onto the assistant turn verbatim.
    expect(assistantTurn?.summary?.citations).toEqual(citations);

    const { s3, ddb } = makeFakes();
    await finalizeTurn({ ...base, s3, ddb, turnId: "t1", userTurn, assistantTurn });

    const messages = await readTranscriptFromS3(s3, "b", "s");
    expect(messages).not.toBeNull();
    const assistant = messages!.find((m) => m.role === "assistant");
    // Deep-equal proves every typed field (verified ref/kind/asset_display_name AND unverified
    // reason) round-tripped with no loss or mutation.
    expect(assistant?.summary?.citations).toEqual(citations);
  });
});
