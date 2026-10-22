import { describe, it, expect } from "vitest";
import { parseRenderOutcome } from "./renderOutcome";

const sse = (...evs: unknown[]) => evs.map((e) => `data: ${JSON.stringify(JSON.stringify(e))}`).join("\n");

describe("parseRenderOutcome", () => {
  it("a clean output_ref is the manifest (the last clean one wins)", () => {
    expect(parseRenderOutcome(sse(
      { type: "output_ref", s3_key: "artifacts/s/render/a.json" },
      { type: "output_ref", s3_key: "artifacts/s/render/b.json" },
    ))).toEqual({ kind: "manifest", s3Key: "artifacts/s/render/b.json" });
  });

  it("a deliberate decline is no_view, not a failure (a2d759c2)", () => {
    expect(parseRenderOutcome(sse({ type: "step" }, { type: "no_view", reason: "data_only" })))
      .toEqual({ kind: "no_view", reason: "data_only" });
  });

  it("an error record is never a manifest — flagged or under render/_errors/", () => {
    expect(parseRenderOutcome(sse({ type: "output_ref", s3_key: "artifacts/s/render/x.json", error: true, error_code: "TIMEOUT" })))
      .toEqual({ kind: "error", code: "TIMEOUT" });
    expect(parseRenderOutcome(sse({ type: "output_ref", s3_key: "artifacts/s/render/_errors/x.json" })).kind).toBe("error");
  });

  it("a clean manifest beats an earlier error record; nothing is none", () => {
    expect(parseRenderOutcome(sse(
      { type: "output_ref", s3_key: "artifacts/s/render/_errors/e.json", error: true },
      { type: "output_ref", s3_key: "artifacts/s/render/ok.json" },
    ))).toEqual({ kind: "manifest", s3Key: "artifacts/s/render/ok.json" });
    expect(parseRenderOutcome("garbage\ndata: {nope")).toEqual({ kind: "none" });
  });
});
