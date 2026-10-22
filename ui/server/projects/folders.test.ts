// The proxy's folder normalizer and Display_Name twin agree with the Python side over the shared fixture
// (`agent/src/ingestion/fixtures/folder_cases.json`, asserted in Python by `tests/test_ingestion_folders.py`).
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { deriveDisplayName, withoutCopySuffix } from "./displayNames";
import { encodeFolderMetadata, folderPathText, isFolderError, normalizeFolder } from "./folders";
import type { AssetKind } from "./schema";

const FIXTURE = path.resolve(__dirname, "../../../agent/src/ingestion/fixtures/folder_cases.json");
const cases = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  folder: { raw: unknown; folder?: string | null; error?: string }[];
  displayName: { name: string; kind: AssetKind; displayName: string }[];
};

describe("normalizeFolder (twin of folders.py normalize_folder)", () => {
  it("has a fixture with both outcomes", () => {
    expect(cases.folder.some((c) => c.error)).toBe(true);
    expect(cases.folder.some((c) => typeof c.folder === "string")).toBe(true);
  });

  it.each(cases.folder.map((c, i) => [i, c] as const))("case %i", (_i, c) => {
    const got = normalizeFolder(c.raw);
    if (c.error) expect(got).toEqual({ error: c.error });
    else expect(got).toBe(c.folder ?? null);
  });

  it("treats undefined as the root and is idempotent on its own output", () => {
    expect(normalizeFolder(undefined)).toBeNull();
    for (const c of cases.folder) {
      const once = normalizeFolder(c.raw);
      if (typeof once === "string") expect(normalizeFolder(once)).toBe(once);
    }
    expect(isFolderError(normalizeFolder("a/../b"))).toBe(true);
    expect(isFolderError("Footage")).toBe(false);
  });

  it("encodes metadata as ASCII and builds the search text", () => {
    expect(encodeFolderMetadata("Footage/Day 1 · ü")).toMatch(/^[\x21-\x7e]+$/);
    expect(decodeURIComponent(encodeFolderMetadata("Footage/Day 1 · ü"))).toBe("Footage/Day 1 · ü");
    expect(folderPathText({ folder: "Footage/Day 1", displayName: "clip.mp4" })).toBe("Footage/Day 1/clip.mp4");
    expect(folderPathText({ displayName: "clip.mp4" })).toBe("clip.mp4");
  });
});

describe("deriveDisplayName (twin of names.py derive_display_name)", () => {
  it.each(cases.displayName.map((c, i) => [i, c] as const))("case %i", (_i, c) => {
    expect(deriveDisplayName(c.name, c.kind)).toBe(c.displayName);
  });

  it("drops only a trailing copy suffix", () => {
    expect(withoutCopySuffix("clip.mp4 (2)")).toBe("clip.mp4");
    expect(withoutCopySuffix("take (3).mp4")).toBe("take (3).mp4");
    expect(withoutCopySuffix("clip.mp4 (x)")).toBe("clip.mp4 (x)");
  });
});
