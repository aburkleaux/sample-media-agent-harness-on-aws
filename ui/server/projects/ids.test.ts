import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { isProjectId, newProjectId } from "./ids";

const cases = JSON.parse(
  readFileSync(join(__dirname, "../../../infra/lib/projects/fixtures/project-id-cases.json"), "utf8"),
);

describe("project ids", () => {
  it("accepts and rejects the shared fixture cases", () => {
    for (const v of cases.valid) expect(isProjectId(v)).toBe(true);
    for (const v of cases.invalid) expect(isProjectId(v)).toBe(false);
    for (const v of cases.invalidNonStrings) expect(isProjectId(v)).toBe(false);
  });
  it("generates 33-char ids that are valid collection ids and unique", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 1000; i++) {
      const id = newProjectId();
      expect(id).toHaveLength(33);
      expect(isProjectId(id)).toBe(true);
      expect(/^[a-z0-9][a-z0-9_-]{2,47}$/.test(id)).toBe(true);
      seen.add(id);
    }
    expect(seen.size).toBe(1000);
  });
});
