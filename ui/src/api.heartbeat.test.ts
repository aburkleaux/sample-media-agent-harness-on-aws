/**
 * The proxy keeps the chat stream alive by writing a blank line.
 *
 * An agentic turn can go minutes between bytes while the ALB (60s idle) and
 * CloudFront (60s read timeout) sit in front of it. When the connection drops
 * the turn still completes server-side, so a finished answer is silently
 * discarded — that is the "network error" seen on a turn whose answer had
 * already been generated.
 *
 * A blank line is the ONLY safe filler: the client splits on \n and skips empty
 * lines, whereas any other filler (": heartbeat") falls through to extractText
 * and would be rendered as assistant text. These tests pin that contract from
 * the client side, so a future change to the parser cannot silently start
 * printing heartbeats into the chat.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const authFetch = vi.fn();
vi.mock("./auth/authFetch", () => ({ authFetch: (...a: unknown[]) => authFetch(...a) }));

import { invokeAgentStreaming, type StreamCallback } from "./api";

/** The event union StreamCallback receives. */
type Ev = Parameters<StreamCallback>[0];

/** Serve a byte stream, optionally split at arbitrary chunk boundaries. */
function streamOf(chunks: string[]) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { done: false, value: encoder.encode(chunks[i++]) }
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

const STEP = JSON.stringify({ type: "step", status: "running", label: "Working" });

describe("heartbeat blank lines", () => {
  beforeEach(() => authFetch.mockReset());

  it("emits no event at all for a blank-line heartbeat", async () => {
    const events = await collect(["\n", "\n", "\n"]);
    expect(events).toEqual([]);
  });

  it("does not inject text between two steps", async () => {
    const events = await collect([`${STEP}\n`, "\n", "\n", `${STEP}\n`]);
    expect(events.filter((e) => e.type === "step")).toHaveLength(2);
    expect(events.filter((e) => e.type === "text")).toHaveLength(0);
  });

  it("leaves assistant text untouched", async () => {
    const events = await collect(["\n", "hello world\n", "\n"]);
    const text = events.filter((e) => e.type === "text").map((e: any) => e.text);
    expect(text).toEqual(["hello world"]);
  });

  it("a heartbeat at a record boundary keeps the next record parseable", async () => {
    // What the proxy actually does: only ever write \n when the previous chunk
    // ended with one.
    const events = await collect([`${STEP}\n`, "\n", `{"type":"step","label":"Second"}\n`]);
    const steps = events.filter((e) => e.type === "step") as any[];
    expect(steps.map((s) => s.step.label)).toEqual(["Working", "Second"]);
  });

  it("would corrupt a record if written mid-line — why the boundary guard exists", async () => {
    // This is the failure the guard prevents: a newline injected into a partial
    // JSON line splits it into two fragments, neither of which parses, and both
    // surface as assistant text.
    const half = `{"type":"step","la`;
    const rest = `bel":"Split"}\n`;
    const events = await collect([half, "\n", rest]);
    expect(events.filter((e) => e.type === "step")).toHaveLength(0);
    expect(events.filter((e) => e.type === "text").length).toBeGreaterThan(0);
  });

  it("a record split across chunks without a heartbeat still parses", async () => {
    const events = await collect([`{"type":"step","la`, `bel":"Whole"}\n`]);
    const steps = events.filter((e) => e.type === "step") as any[];
    expect(steps.map((s) => s.step.label)).toEqual(["Whole"]);
  });
});

describe("proxy heartbeat implementation", () => {
  it("writes only a newline, gated on a record boundary, and clears its timer", async () => {
    const fs = await import("node:fs/promises");
    const src = await fs.readFile(new URL("../server/app.ts", import.meta.url), "utf8");
    const relay = src.slice(src.indexOf("const heartbeat = setInterval"));
    // Filler must be a bare newline — never a comment line.
    expect(relay).toMatch(/res\.write\("\\n"\)/);
    expect(relay).not.toMatch(/res\.write\(": heartbeat/);
    // Never write into a partial record.
    expect(relay).toMatch(/if \(!atLineBoundary\) return;/);
    // Boundary tracked from the last byte of each forwarded chunk.
    expect(src).toMatch(/atLineBoundary = chunk\[chunk\.length - 1\] === 0x0a/);
    // Timer cleared on every exit path or the process leaks intervals.
    expect(src).toMatch(/const stopHeartbeat = \(\) => clearInterval\(heartbeat\)/);
    expect(src.match(/stopHeartbeat\(\)/g)?.length ?? 0).toBeGreaterThanOrEqual(2);
    expect(src).toMatch(/req\.on\("close", stopHeartbeat\)/);
  });

  it("beats well inside the 60s ALB and CloudFront windows", async () => {
    const fs = await import("node:fs/promises");
    const src = await fs.readFile(new URL("../server/app.ts", import.meta.url), "utf8");
    const ms = Number(/const STREAM_HEARTBEAT_MS = ([\d_]+)/.exec(src)?.[1].replace(/_/g, ""));
    expect(ms).toBeGreaterThan(0);
    expect(ms).toBeLessThan(30_000);
  });
});
