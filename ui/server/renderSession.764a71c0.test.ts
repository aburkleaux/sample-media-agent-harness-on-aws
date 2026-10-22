// Session 764a71c0: one trimmed clip opened TWO render tabs.
//
// The trim output was adopted into `artifacts/<sid>/output/<execUUID>/meridian_trim_0_84040.mp4`
// (the clip's own render keyed to `<execUUID>/meridian_trim_0_84040.mp4`). The render durable pass
// then mirrored the M2C proxy copy of the SAME bytes into the flat
// `artifacts/durable/<sha32>.mp4`, a name with no exec UUID and no basename, so analyses that
// played the mirror were keyed to that bare path and composeRenderTabs (exact-sourceKey grouping)
// opened a second tab.
//
// The agent fix keeps the identity in the durable key
// (`artifacts/durable/<digest>/<execUUID>/<basename>`), which `_canonical_source_identity` maps to
// the clip's own key. That mapping is pinned on the Python side
// (agent/tests/test_durable_identity_764a71c0.py); this test pins the UI half: analyses carrying
// the key that path produces fold into the clip's ONE composite tab.
import { describe, it, expect } from "vitest";
import { composeRenderTabs } from "./renderSession";

const CLIP_KEY = "5b3a3e4a-5744-5c57-8147-6fa3daaf9ab3/meridian_trim_0_84040.mp4";
// The identity the NEW durable url
//   artifacts/durable/<digest>/5b3a3e4a-5744-5c57-8147-6fa3daaf9ab3/meridian_trim_0_84040.mp4
// yields after `_norm_url` + `_canonical_source_identity`:
const NEW_DURABLE_IDENTITY = "5b3a3e4a-5744-5c57-8147-6fa3daaf9ab3/meridian_trim_0_84040.mp4";
// What the OLD flat mirror produced (no identity left to recover; illustrative digest).
const OLD_FLAT_DURABLE_KEY = "artifacts/durable/9c1f0d6a2b7e4c3f8a5d1e0b6c7f2a94.mp4";

const vid = (id: string, sourceKey: string) => ({
  key: id,
  createdAt: "2026-09-16T00:00:00Z",
  label: "Video",
  manifest: { id, template: "video-timeline", sourceKey, tracks: [{ id: `${id}-v`, type: "video" }] },
});
const analysis = (id: string, sourceKey: string, label: string) => ({
  key: id,
  createdAt: "2026-09-16T00:00:01Z",
  label,
  manifest: {
    id,
    template: "video-annotations",
    sourceKey,
    tracks: [{ id: `${id}-a`, type: "audio" }],
    markers: [{ id: `${id}-m` }],
  },
});

describe("764a71c0 render composition", () => {
  it("analyses keyed by the new durable identity fold into ONE composite with the clip", () => {
    expect(NEW_DURABLE_IDENTITY).toBe(CLIP_KEY);
    const tabs = composeRenderTabs([
      vid("clip", CLIP_KEY),
      analysis("shots", NEW_DURABLE_IDENTITY, "Shot Segmentation"),
      analysis("faces", NEW_DURABLE_IDENTITY, "Face Recognition"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe(`composite:${CLIP_KEY}`);
    expect((tabs[0].memberKeys as string[]).sort()).toEqual(["clip", "faces", "shots"]);
  });

  it("regression shape: the old flat durable key splits the clip into two tabs", () => {
    const tabs = composeRenderTabs([
      vid("clip", CLIP_KEY),
      analysis("shots", OLD_FLAT_DURABLE_KEY, "Shot Segmentation"),
      analysis("faces", OLD_FLAT_DURABLE_KEY, "Face Recognition"),
    ]);
    expect(tabs).toHaveLength(2);
    expect(tabs.some((t) => t.key === `composite:${OLD_FLAT_DURABLE_KEY}`)).toBe(true);
  });
});
