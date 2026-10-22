/**
 * What the GROUPING layer owns, after the container fold.
 *
 * Staging session 9e977f30: the user marked a range and asked for shot detection. The plan
 * trimmed the range into a new 217.351s video and segmented THAT, so the segmentation keyed onto
 * the trim (`sourceKey 4b894089-…/transcoded.mp4`) — correctly. But the trim node was authored
 * `output_type: text`, reporting its result as an s3:// uri inside prose, so the range video never
 * became a media artifact, never got a manifest, and the segmentation was the ONLY manifest with
 * that sourceKey. One member means no composite.
 *
 * That used to fall through to a per-analysis view, and this layer promoted it. The promotion is
 * GONE: every time-based template name now routes to the one timeline container
 * (RendererRegistry), so a solo analysis renders on its media's timeline with no rewriting here.
 * templateFold.test.tsx pins the routing; this file pins that grouping leaves a solo tab ALONE and
 * still composites when siblings exist.
 */
import { describe, it, expect } from "vitest";
import { composeRenderTabs } from "./renderSession";

const RANGE_KEY = "4b894089-6279-5c90-a704-de5788be333a/transcoded.mp4";

/** The real sg2_segment.json shape (template/duration/tracks/markers/sourceKey). */
function segmentationOfRange() {
  return {
    key: "sg2_segment.json",
    createdAt: "2026-09-17T20:15:55Z",
    label: "Shot Segmentation",
    manifest: {
      id: "sg2_segment",
      template: "video-markers",
      duration: 217.351,
      sourceKey: RANGE_KEY,
      tracks: [{ id: "video-segment_0000", type: "video", src: "https://cdn/segment_0000.mp4" }],
      markers: Array.from({ length: 22 }, (_, i) => ({ id: `m-${i}`, time: i * 9 })),
      clips: [],
      captions: [],
    },
  };
}

describe("a solo analysis passes through untouched", () => {
  it("keeps the manifest exactly as stored — the container handles presentation", () => {
    const tabs = composeRenderTabs([segmentationOfRange()]);
    expect(tabs).toHaveLength(1);
    // No template rewriting here any more: `video-markers` is an alias for the timeline
    // container, so the stored value is what should survive a round trip.
    expect((tabs[0].manifest as { template?: string }).template).toBe("video-markers");
    expect((tabs[0].manifest as { markers?: unknown[] }).markers).toHaveLength(22);
    expect((tabs[0].manifest as { duration?: number }).duration).toBe(217.351);
  });

  it("keeps the tab's identity so nothing downstream re-identifies it", () => {
    const tabs = composeRenderTabs([segmentationOfRange()]);
    expect(tabs[0].key).toBe("sg2_segment.json");
    expect((tabs[0].manifest as { id?: string }).id).toBe("sg2_segment");
    expect((tabs[0].manifest as { sourceKey?: string }).sourceKey).toBe(RANGE_KEY);
  });

  it("is not marked composite — there are no members to absorb", () => {
    // `composite: true` + memberKeys tells the UI to drop absorbed sibling tabs. A solo tab
    // absorbed nothing, so claiming otherwise could delete an unrelated tab.
    const tabs = composeRenderTabs([segmentationOfRange()]);
    expect(tabs[0].composite).toBeUndefined();
    expect(tabs[0].memberKeys).toBeUndefined();
  });

  it("composites normally once the canvas DOES get its own manifest", () => {
    const canvas = {
      key: "sg1_trim.json", createdAt: "2026-09-17T20:12:20Z", label: "Video",
      manifest: {
        id: "sg1_trim", template: "nle-timeline", duration: 217.351, sourceKey: RANGE_KEY,
        tracks: [{ id: "video-transcoded", type: "video", src: "https://cdn/transcoded.mp4" }],
        clips: [],
      },
    };
    const tabs = composeRenderTabs([canvas, segmentationOfRange()]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].composite).toBe(true);
    expect(tabs[0].label).toBe("Video");
    expect((tabs[0].manifest as { markers?: unknown[] }).markers).toHaveLength(22);
  });
});

describe("a2ebd1e9 non-regression: a proxy canvas behind several analyses", () => {
  // The case intermediate_node_ids was written for: a proxy transcode feeding three analyses
  // must NOT become an extra "Media Processing" tab. With the canvas rendered it does not — it
  // MERGES into the one composite, which is strictly better than three analysis tabs.
  const PROXY = "5f7af2d0-f1a9-5989-8573-4dcd72b27bc4/transcoded.mp4";
  const analysis = (id: string, label: string) => ({
    key: id, createdAt: "2026-09-16T00:00:01Z", label,
    manifest: {
      id, template: "video-markers", duration: 600, sourceKey: PROXY,
      tracks: [{ id: `${id}-a`, type: "audio" }], markers: [{ id: `${id}-m` }],
    },
  });

  it("collapses the proxy canvas and its three analyses into ONE tab", () => {
    const tabs = composeRenderTabs([
      { key: "proxy", createdAt: "2026-09-16T00:00:00Z", label: "Video",
        manifest: { id: "proxy", template: "nle-timeline", duration: 600, sourceKey: PROXY,
                    tracks: [{ id: "proxy-v", type: "video" }] } },
      analysis("stems", "Audio"), analysis("diarize", "Diarization"), analysis("shots", "Shots"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].composite).toBe(true);
    expect((tabs[0].memberKeys as string[]).sort()).toEqual(["diarize", "proxy", "shots", "stems"]);
    expect(tabs[0].label).toBe("Video");
  });

  it("two DIFFERENT renditions never merge (they are different media)", () => {
    // Guard for the identity trap: 5f7af2d0/transcoded.mp4 and cf72bf95/transcoded.mp4 share a
    // basename AND a duration, so any basename+duration identity would wrongly fuse them.
    // Provenance (the producing execution) is what keeps them apart.
    const other = "cf72bf95-fabe-5a90-8f01-08ef5ee4fb3a/transcoded.mp4";
    const tabs = composeRenderTabs([
      { key: "t1", createdAt: "2026-09-16T00:00:00Z", label: "Proxy",
        manifest: { id: "t1", template: "nle-timeline", duration: 600, sourceKey: PROXY,
                    tracks: [{ id: "t1-v", type: "video" }] } },
      { key: "t2", createdAt: "2026-09-16T00:00:01Z", label: "Transcode 2",
        manifest: { id: "t2", template: "nle-timeline", duration: 600, sourceKey: other,
                    tracks: [{ id: "t2-v", type: "video" }] } },
    ]);
    expect(tabs).toHaveLength(2);
    expect(tabs.every((t) => t.composite === undefined)).toBe(true);
  });
});
