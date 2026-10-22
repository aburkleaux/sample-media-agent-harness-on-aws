import { describe, it, expect } from "vitest";
import { TranscriptAccumulator, tryParseStep, tryParseSummary, extractText } from "./transcriptAccumulator";

// The runtime emits SSE lines `data: <json>`; some payloads are double-JSON-encoded (a JSON string
// that itself decodes to an object). These tests pin the server accumulator to the SAME grammar the
// client uses (ui/src/api.ts), because the two must reconstruct byte-identical turns.

describe("frame grammar", () => {
  it("parses a step frame", () => {
    expect(tryParseStep(JSON.stringify({ type: "step", status: "running", label: "Thinking" }))?.label).toBe("Thinking");
  });
  it("parses a double-JSON-encoded step", () => {
    const inner = JSON.stringify({ type: "step", status: "done", label: "X" });
    expect(tryParseStep(JSON.stringify(inner))?.status).toBe("done");
  });
  it("parses an execution_summary", () => {
    expect(tryParseSummary(JSON.stringify({ type: "execution_summary", total_duration_ms: 5 }))?.total_duration_ms).toBe(5);
  });
  it("extractText returns the string for a JSON string payload, empty for a step", () => {
    expect(extractText(JSON.stringify("hello"))).toBe("hello");
    expect(extractText(JSON.stringify({ type: "step", status: "x", label: "y" }))).toBe("");
  });
  it("extractText passes raw non-JSON through", () => {
    expect(extractText("plain text")).toBe("plain text");
  });
});

describe("TranscriptAccumulator", () => {
  const feed = (frames: string[]): void => void frames; // doc helper
  void feed;

  it("concatenates assistant text across frames in order", () => {
    const acc = new TranscriptAccumulator();
    acc.push(`data: ${JSON.stringify("Hello ")}\n`);
    acc.push(`data: ${JSON.stringify("world")}\n`);
    acc.finish();
    expect(acc.result().text).toBe("Hello world");
  });

  it("handles frames split across chunk boundaries", () => {
    const acc = new TranscriptAccumulator();
    const line = `data: ${JSON.stringify("split me")}\n`;
    acc.push(line.slice(0, 10));
    acc.push(line.slice(10));
    acc.finish();
    expect(acc.result().text).toBe("split me");
  });

  it("flushes a trailing partial line with no newline on finish()", () => {
    const acc = new TranscriptAccumulator();
    acc.push(`data: ${JSON.stringify("no trailing newline")}`);
    acc.finish();
    expect(acc.result().text).toBe("no trailing newline");
  });

  it("ignores blank heartbeat lines", () => {
    const acc = new TranscriptAccumulator();
    acc.push("\n");
    acc.push(`data: ${JSON.stringify("real")}\n`);
    acc.push("\n\n");
    acc.finish();
    expect(acc.result().text).toBe("real");
  });

  it("collects steps and dedups by node_id (latest status wins)", () => {
    const acc = new TranscriptAccumulator();
    acc.push(`data: ${JSON.stringify({ type: "step", status: "running", label: "A", node_id: "n1" })}\n`);
    acc.push(`data: ${JSON.stringify({ type: "step", status: "running", label: "B", node_id: "n2" })}\n`);
    acc.push(`data: ${JSON.stringify({ type: "step", status: "done", label: "A done", node_id: "n1" })}\n`);
    acc.finish();
    const steps = acc.result().steps;
    expect(steps.map((s) => `${s.node_id}:${s.status}`)).toEqual(["n1:done", "n2:running"]);
  });

  it("appends node-less steps without dedup", () => {
    const acc = new TranscriptAccumulator();
    acc.push(`data: ${JSON.stringify({ type: "step", status: "done", label: "one" })}\n`);
    acc.push(`data: ${JSON.stringify({ type: "step", status: "done", label: "two" })}\n`);
    acc.finish();
    expect(acc.result().steps.map((s) => s.label)).toEqual(["one", "two"]);
  });

  it("captures the last execution_summary and separates it from text", () => {
    const acc = new TranscriptAccumulator();
    acc.push(`data: ${JSON.stringify("answer")}\n`);
    acc.push(`data: ${JSON.stringify({ type: "execution_summary", total_duration_ms: 42, model_id: "m" })}\n`);
    acc.finish();
    const r = acc.result();
    expect(r.text).toBe("answer");
    expect(r.summary?.total_duration_ms).toBe(42);
    expect(r.summary?.model_id).toBe("m");
  });

  it("reconstructs a realistic mixed turn (text + steps + summary)", () => {
    const acc = new TranscriptAccumulator();
    const frames = [
      JSON.stringify({ type: "step", status: "running", label: "Planning", node_id: "p" }),
      JSON.stringify("I'll do it. "),
      JSON.stringify({ type: "step", status: "done", label: "Planning", node_id: "p", duration_ms: 1200 }),
      JSON.stringify("Done."),
      JSON.stringify({ type: "execution_summary", total_duration_ms: 3400, cycles: 2 }),
    ];
    for (const f of frames) acc.push(`data: ${f}\n`);
    acc.finish();
    const r = acc.result();
    expect(r.text).toBe("I'll do it. Done.");
    expect(r.steps).toHaveLength(1);
    expect(r.steps[0].status).toBe("done");
    expect(r.steps[0].duration_ms).toBe(1200);
    expect(r.summary?.cycles).toBe(2);
  });
});
