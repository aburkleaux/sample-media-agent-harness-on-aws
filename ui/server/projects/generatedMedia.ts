// Generated_Media turn message (media-harness-project-ingestion Req 9, design §16).
//
// At the end of a Projects turn the proxy enqueues one `{kind: "turn"}` message for GeneratedMediaFn, which
// registers the media the turn produced into the turn's Active_Project. The proxy is the producer because it
// alone holds both the turn's Active_Project (from the turn gate) and the verified Actor at turn end.
//
// `externalRefs` are the content-addressed `artifacts/external/<24 hex>[.ext]` keys (fal outputs copied by
// `agent/src/media/persist_media.py`) that the reply text, its steps, or its summary name. They are
// session-less, so a reference is the only way to know a turn produced them. Best effort: the Lambda also
// reads the render manifests the turn wrote, and session objects are found by time window, not by reference.
//
// Sending never blocks or fails the turn: an error is logged and dropped.

export const EXTERNAL_REF_RE = /artifacts\/external\/[0-9a-f]{24}(?:\.[A-Za-z0-9]{2,5})?(?![A-Za-z0-9])/g;
const MAX_REFS = 200;

export interface GeneratedMediaTurnMessage {
  kind: "turn";
  sessionId: string;
  turnId: string;
  projectId: string;
  actorSub: string;
  /** Epoch milliseconds. */
  startedAt: number;
  endedAt: number;
  externalRefs: string[];
  /** asset-lineage design §3: the turn's output → inputs ledger from the agent's execution_summary. */
  lineage?: LineageEntry[];
}

export interface LineageEntry {
  output: string;
  inputs: string[];
  tool?: string;
}

const MAX_LINEAGE = 100;
const MAX_LINEAGE_INPUTS = 20;
const MAX_URI = 2048;

/** The well-formed `lineage` entries of an execution_summary (`summary.lineage`); malformed ones are dropped. */
export function lineageIn(summary: unknown): LineageEntry[] {
  const raw = (summary as { lineage?: unknown } | null)?.lineage;
  if (!Array.isArray(raw)) return [];
  const out: LineageEntry[] = [];
  for (const e of raw) {
    if (out.length >= MAX_LINEAGE) break;
    const o = (e as { output?: unknown })?.output;
    if (typeof o !== "string" || !o || o.length > MAX_URI) continue;
    const ins = (e as { inputs?: unknown }).inputs;
    const inputs = Array.isArray(ins)
      ? [...new Set(ins.filter((i): i is string => typeof i === "string" && i.length > 0 && i.length <= MAX_URI))].slice(0, MAX_LINEAGE_INPUTS)
      : [];
    const tool = (e as { tool?: unknown }).tool;
    out.push(typeof tool === "string" && tool ? { output: o, inputs, tool: tool.slice(0, 64) } : { output: o, inputs });
  }
  return out;
}

/** Every external key named in `texts`, sorted and unique (capped so a message stays far under 256 KB). */
export function externalRefsIn(...texts: string[]): string[] {
  const found = new Set<string>();
  for (const t of texts) {
    for (const m of (t ?? "").matchAll(EXTERNAL_REF_RE)) found.add(m[0]);
  }
  return [...found].sort().slice(0, MAX_REFS);
}

export interface TurnEnd {
  sessionId: string | undefined;
  turnId: string | undefined;
  projectId: string | undefined;
  actorSub: string | undefined;
  startedAt: number;
  endedAt: number;
  text: string;
  steps: unknown;
  summary: unknown;
}

/** The message for a finished turn, or `null` when it was not a Projects turn of a known Actor. */
export function buildTurnMessage(t: TurnEnd): GeneratedMediaTurnMessage | null {
  if (!t.sessionId || !t.turnId || !t.projectId || !t.actorSub) return null;
  if (!(t.startedAt > 0 && t.endedAt >= t.startedAt)) return null;
  const lineage = lineageIn(t.summary);
  return {
    kind: "turn",
    sessionId: t.sessionId,
    turnId: t.turnId,
    projectId: t.projectId,
    actorSub: t.actorSub,
    startedAt: t.startedAt,
    endedAt: t.endedAt,
    externalRefs: externalRefsIn(t.text, JSON.stringify(t.steps ?? []), JSON.stringify(t.summary ?? null)),
    ...(lineage.length ? { lineage } : {}),
  };
}

/** `send` posts one SQS message body; the notifier never throws and never awaits on the turn's path. */
export function makeGeneratedMediaNotifier(send: (body: string) => Promise<unknown>) {
  return (t: TurnEnd): void => {
    const msg = buildTurnMessage(t);
    if (!msg) return;
    void send(JSON.stringify(msg)).catch((e: any) =>
      console.error("[agentcore-proxy] generated-media enqueue failed:", e?.name ?? e?.message ?? e));
  };
}
