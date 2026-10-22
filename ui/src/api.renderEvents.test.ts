/**
 * The composing-render lifecycle rides on the SSE step stream: a leading marker
 * (render_request_id, no manifest) opens a per-tab placeholder, the manifest step
 * (same id) fills it, and a terminal render_failed (same id) removes a placeholder
 * that produced nothing. These tests pin that step -> StreamEvent mapping so the
 * "composing tab spins forever" regression cannot come back through the parser.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const authFetch = vi.fn();
vi.mock("./auth/authFetch", () => ({ authFetch: (...a: unknown[]) => authFetch(...a) }));

import { invokeAgentStreaming, type StreamCallback } from "./api";

type Ev = Parameters<StreamCallback>[0];

function streamOf(chunks: string[]) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { done: false, value: enc.encode(chunks[i++]) }
            : { done: true, value: undefined },
      }),
    },
  };
}

async function collect(chunks: string[]): Promise<Ev[]> {
  authFetch.mockResolvedValueOnce(streamOf(chunks));
  const events: Ev[] = [];
  await invokeAgentStreaming({ prompt: "x" } as never, (e) => events.push(e));
  return events;
}

const line = (extra: Record<string, unknown>) =>
  `${JSON.stringify({ type: "step", status: "running", label: "\u21b3 x", ...extra })}\n`;

describe("render event mapping (composing lifecycle)", () => {
  beforeEach(() => authFetch.mockReset());

  it("leading marker -> render_pending (opens a composing tab)", async () => {
    const evs = await collect([line({ agent: "render", render_request_id: "R1", render_label: "Run Model" })]);
    expect(evs).toContainEqual({ type: "render_pending", id: "R1", label: "Run Model" });
  });

  it("manifest -> render carrying the same id (fills the tab in place)", async () => {
    const evs = await collect([
      line({ render_manifest_key: "artifacts/s/render/m.json", render_request_id: "R1", render_label: "Run Model" }),
    ]);
    expect(evs).toContainEqual({ type: "render", manifestKey: "artifacts/s/render/m.json", label: "Run Model", id: "R1" });
  });

  it("no-manifest terminal -> render_failed and NOT render_pending (removes stuck tab)", async () => {
    const evs = await collect([line({ agent: "render", render_failed: true, render_request_id: "R1" })]);
    expect(evs).toContainEqual({ type: "render_failed", id: "R1" });
    expect(evs.find((e) => e.type === "render_pending")).toBeUndefined();
  });
});
