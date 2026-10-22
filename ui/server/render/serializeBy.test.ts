import { describe, expect, it } from "vitest";
import { createSerializer } from "./serializeBy";

const tick = () => new Promise((r) => setTimeout(r, 0));

function deferred<T = void>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe("serializeBy", () => {
  it("(c70967c9) runs one task at a time per key, in arrival order", async () => {
    const serialize = createSerializer();
    const log: string[] = [];
    const a = deferred();
    const pa = serialize("s1", async () => { log.push("a:start"); await a.promise; log.push("a:end"); return "A"; });
    const pb = serialize("s1", async () => { log.push("b:start"); return "B"; });
    await tick();
    expect(log).toEqual(["a:start"]); // b waits for a: no second concurrent call on the session
    a.resolve();
    expect(await pa).toBe("A");
    expect(await pb).toBe("B");
    expect(log).toEqual(["a:start", "a:end", "b:start"]);
  });

  it("does not hold up other keys", async () => {
    const serialize = createSerializer();
    const a = deferred();
    const pa = serialize("s1", () => a.promise.then(() => "A"));
    expect(await serialize("s2", async () => "B")).toBe("B");
    a.resolve();
    expect(await pa).toBe("A");
  });

  it("a failed task rejects its own caller but never blocks the next one", async () => {
    const serialize = createSerializer();
    const pa = serialize("s1", async () => { throw new Error("boom"); });
    const pb = serialize("s1", async () => "B");
    await expect(pa).rejects.toThrow("boom");
    expect(await pb).toBe("B");
  });
});
