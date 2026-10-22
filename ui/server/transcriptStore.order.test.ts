// The stored transcript stays in time order: a turn is written when it ENDS, so an action made while it ran was
// stored ahead of the turn's question (session 25d42705).
import { describe, it, expect } from "vitest";
import { orderByTime } from "./transcriptStore";

describe("orderByTime", () => {
  it("sorts by timestamp, stably, and keeps a message with none next to the one before it", () => {
    const m = (id: string, timestamp?: number) => ({ id, role: "user", content: "", ...(timestamp !== undefined ? { timestamp } : {}) }) as any;
    const out = orderByTime([m("a", 100), m("act", 760), m("u", 734), m("asst", 735), m("x"), m("b", 100)]);
    expect(out.map((x: any) => x.id)).toEqual(["a", "b", "u", "asst", "x", "act"]);
  });
});
