// Defense-in-depth for the broken raw manifest tab (session 368f1233): a manifest with no
// template, no sourceKey, and no errors is unroutable and must be DROPPED by the tab composer so
// it never surfaces as a broken "raw manifest" tab. Error manifests (errors[] non-empty) and any
// templated/sourceKey'd manifest are KEPT.
import { describe, it, expect, vi } from "vitest";

// composeTimeline is exercised by its own tests; here we only assert WHICH manifests survive.
vi.mock("../src/shared/composeTimeline", () => ({
  composeTimeline: (manifests: unknown[]) => ({ template: "composite-timeline", memberCount: manifests.length }),
}));

import { composeRenderTabs, isUnroutableRawManifest } from "./renderSession";

const item = (key: string, manifest: unknown) => ({ key, createdAt: "2026-09-29T00:00:00Z", manifest });

describe("isUnroutableRawManifest", () => {
  it("is true only for no-template + no-sourceKey + no-errors", () => {
    expect(isUnroutableRawManifest({ id: "x", tracks: [{ type: "video", src: "u" }], errors: [] })).toBe(true);
    expect(isUnroutableRawManifest({ id: "x", tracks: [] })).toBe(true); // no errors field at all
  });

  it("is false when a template, sourceKey, or non-empty errors is present", () => {
    expect(isUnroutableRawManifest({ template: "nle-timeline" })).toBe(false);
    expect(isUnroutableRawManifest({ sourceKey: "s/clip.mp4" })).toBe(false);
    expect(isUnroutableRawManifest({ errors: [{ code: "TIMEOUT", message: "x" }] })).toBe(false);
  });
});

describe("composeRenderTabs drops unroutable raw manifests", () => {
  it("drops the raw manifest AND the error-only record, keeps the templated solo", () => {
    const raw = item("raw.json", {
      id: "4b4e2fdc", duration: 47.2,
      tracks: [{ id: "video-0", type: "video", src: "https://cdn/input/Meridian.mp4" }],
      clips: [], captions: [], errors: [],
    });
    const errorManifest = item("err.json", {
      id: "e1", duration: 0.001, tracks: [], clips: [],
      errors: [{ code: "TIMEOUT", message: "pipeline exceeded budget" }],
    });
    const templated = item("good.json", {
      id: "g1", template: "nle-timeline", duration: 60,
      tracks: [{ id: "v", type: "video", src: "https://cdn/clip.mp4" }], clips: [],
    });

    const tabs = composeRenderTabs([raw, errorManifest, templated]);
    const keys = tabs.map((t) => t.key);
    expect(keys).not.toContain("raw.json");   // the broken raw tab is gone
    // An error-only record is a failure, not a view: there is no error view, so it was a junk tab
    // ("No previewable artifacts" / a link to its sourceUrl). Dropped since 2026-10-11 (a2d759c2).
    expect(keys).not.toContain("err.json");
    expect(keys).toContain("good.json");       // templated solo preserved
    expect(tabs).toHaveLength(1);
  });
});

// a2d759c2: the "Render Output · 1 data file" tab was a VALIDATION_FAILED record from a failed agentic render.
describe("error-only manifests are not tabs", () => {
  const a2d = {
    id: "6921bd12", duration: 0.001, tracks: [], clips: [], createdAt: "2026-10-11T13:18:33.000Z",
    errors: [{ code: "VALIDATION_FAILED", message: "Manifest validation failed after self-correction" }],
    sourceUrl: "https://m2c/x/conversation_analysis.json",
  };
  it("isErrorOnlyManifest: errors and nothing to show", async () => {
    const { isErrorOnlyManifest } = await import("./renderSession");
    expect(isErrorOnlyManifest(a2d)).toBe(true);
    expect(isErrorOnlyManifest({ ...a2d, template: "nle-timeline", tracks: [{ type: "video", src: "v.mp4" }] })).toBe(false);
    expect(isErrorOnlyManifest({ template: "nle-timeline" })).toBe(false);
  });
  it("composeRenderTabs drops the a2d759c2 record and keeps the real tabs", () => {
    const tabs = composeRenderTabs([
      { key: "artifacts/s/render/doc.json", createdAt: "2026-10-11T13:14:00Z", label: "Document",
        manifest: { template: "document-viewer", document: { previewUrl: "/api/artifact?key=r.pdf" } } },
      { key: "artifacts/s/render/err.json", createdAt: "2026-10-11T13:18:35Z", manifest: a2d },
    ]);
    expect(tabs.map((t) => t.key)).toEqual(["artifacts/s/render/doc.json"]);
  });
});
