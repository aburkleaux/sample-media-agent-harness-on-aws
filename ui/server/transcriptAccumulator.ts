// Server-side SSE frame parser + turn accumulator.
//
// The proxy's /api handler pipes the runtime's SSE stream to the browser verbatim; the browser
// (ui/src/api.ts:invokeAgentStreaming) is what parses frames into text / step / execution_summary
// and assembles a rich transcript entry. For the proxy to become the AUTHORITATIVE transcript
// writer (docs/design-proxy-authoritative-transcript.md), it must reconstruct the SAME entry
// server-side. This module replicates the client frame grammar EXACTLY — including the
// double-JSON-encoding quirk — so a server-built turn is byte-comparable to the client-built one.
//
// Kept dependency-free and pure so it is trivially unit-testable and cannot drift on runtime
// concerns. It sees only genuine upstream frames: the heartbeat blank-lines the handler injects go
// to the client response, never to this accumulator.

import type { CitationBlock } from "../src/api";

/** A step event as it appears on the wire (mirrors ui/src/api.ts StepEvent). Stored verbatim. */
export interface WireStep {
  type: "step";
  status: string;
  label: string;
  duration_ms?: number;
  agent?: string;
  phase?: string;
  node_id?: string;
  plan_type?: string;
  plan_data?: Record<string, unknown>;
  gate_id?: string;
  render_manifest_key?: string;
  render_label?: string;
  render_request_id?: string;
  render_failed?: boolean;
  [k: string]: unknown;
}

export interface WireSummary {
  type: "execution_summary";
  total_duration_ms?: number;
  cycles?: number;
  tools?: Record<string, unknown>;
  usage?: Record<string, unknown>;
  model?: string;
  model_id?: string;
  /**
   * Turn-end citation verification block (media-harness-search-qa-rendering Req 2.6, 2.12). The
   * accumulator keeps the execution_summary frame verbatim, so this rides through unchanged; the
   * index signature below would already pass it, but it is named so buildTurnMessages/finalizeTurn
   * and readTranscriptFromS3 carry a typed CitationBlock end to end.
   */
  citations?: CitationBlock;
  [k: string]: unknown;
}

export interface AccumulatedTurn {
  /** Concatenated assistant text (frames appended in arrival order, mirroring `accumulatedText += text`). */
  text: string;
  /** Ordered step group, deduped by node_id (latest status wins), mirroring the client's recordStep. */
  steps: WireStep[];
  /** The terminal execution summary, if one arrived (last one wins). */
  summary: WireSummary | null;
}

// --- Frame grammar (verbatim from ui/src/api.ts:105-135) ---------------------------------------

export function tryParseStep(raw: string): WireStep | null {
  try {
    let parsed: unknown = JSON.parse(raw);
    if (typeof parsed === "string") {
      try { parsed = JSON.parse(parsed); } catch { return null; }
    }
    if (parsed && typeof parsed === "object" && (parsed as { type?: unknown }).type === "step") {
      return parsed as WireStep;
    }
  } catch { /* not JSON */ }
  return null;
}

export function tryParseSummary(raw: string): WireSummary | null {
  try {
    let parsed: unknown = JSON.parse(raw);
    if (typeof parsed === "string") {
      try { parsed = JSON.parse(parsed); } catch { return null; }
    }
    if (parsed && typeof parsed === "object" && (parsed as { type?: unknown }).type === "execution_summary") {
      return parsed as WireSummary;
    }
  } catch { /* not JSON */ }
  return null;
}

export function extractText(raw: string): string {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed === "string") return parsed;
    if (parsed && typeof parsed === "object" && (parsed as { type?: unknown }).type === "step") return "";
    return raw;
  } catch {
    return raw;
  }
}

/**
 * Streaming accumulator. Feed raw upstream bytes/strings via push(); it buffers, splits on `\n`,
 * and folds complete lines into a turn. Call finish() once at stream end to flush the trailing
 * partial line, then read result().
 */
export class TranscriptAccumulator {
  private buffer = "";
  private text = "";
  private steps: WireStep[] = [];
  private stepIndexByNode = new Map<string, number>();
  private summary: WireSummary | null = null;

  push(chunk: Buffer | string): void {
    this.buffer += typeof chunk === "string" ? chunk : chunk.toString("utf8");
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() ?? "";
    for (const line of lines) this.handleLine(line);
  }

  finish(): void {
    if (this.buffer.trim()) this.handleLine(this.buffer);
    this.buffer = "";
  }

  private handleLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return; // blank line (incl. heartbeat mirror) — no-op, matches the client
    const payload = trimmed.startsWith("data: ") ? trimmed.slice(6) : trimmed;
    const step = tryParseStep(payload);
    if (step) { this.recordStep(step); return; }
    const summary = tryParseSummary(payload);
    if (summary) { this.summary = summary; return; }
    const text = extractText(payload);
    if (text) this.text += text;
  }

  // Dedup by node_id (latest status wins) else append — mirrors the client's accumulatedSteps.
  private recordStep(step: WireStep): void {
    const nodeId = typeof step.node_id === "string" ? step.node_id : "";
    if (nodeId) {
      const existing = this.stepIndexByNode.get(nodeId);
      if (existing !== undefined) { this.steps[existing] = step; return; }
      this.stepIndexByNode.set(nodeId, this.steps.length);
    }
    this.steps.push(step);
  }

  result(): AccumulatedTurn {
    return {
      text: this.text,
      steps: this.steps,
      summary: this.summary,
    };
  }
}
