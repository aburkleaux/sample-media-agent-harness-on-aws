// Deterministic reproduction of session a618f7ee's render-tab composition, built from the REAL
// sourceKeys observed in S3. Locks in the problem-2 regression + its fix direction, and documents
// that problem 1 (original vs proxy) is NOT a compose-layer collapse (distinct keys, distinct tabs).
//
// Real manifests (artifacts/a618f7ee-.../render/):
//   media-processing_5f7af2d0.json  render-video   sourceKey 5f7af2d0/transcoded.mp4   (the 480p proxy)
//   audiostem-separation_98d9ae12   render-audio   sourceKey 5f7af2d0/transcoded.mp4
//   diarize.json                    diarization    sourceKey 5f7af2d0/transcoded.mp4
//   shots.json                      shot-seg       sourceKey 5f7af2d0/transcoded.mp4
//   faces.json                      face-recog     sourceKey artifacts/durable/563edd7e...mp4  <-- diverged
//   b19bebdf...json (original)      render-video   sourceKey a618f7ee-.../1789517702222-nba.mp4
//   media-processing_cf72bf95.json  render-video   sourceKey cf72bf95/transcoded.mp4  (2nd transcode)
import { describe, it, expect } from "vitest";
import { composeRenderTabs } from "./renderSession";

const PROXY_KEY = "5f7af2d0-f1a9-5989-8573-4dcd72b27bc4/transcoded.mp4";
const FACES_DURABLE_KEY = "artifacts/durable/563edd7e558cb34095134e07bc0e88e2.mp4"; // what my fix stamped
const FACES_EXECUUID_KEY = PROXY_KEY; // what derive_source_key produced pre-fix (and post-revert)
const ORIGINAL_KEY = "a618f7ee-70a5-4d81-891e-3bb48c92bdc1/1789517702222-nba.mp4";
const TRANSCODE2_KEY = "cf72bf95-fabe-5a90-8f01-08ef5ee4fb3a/transcoded.mp4";

// Minimal timeline-composable manifests (template not in NON_TIMELINE_TEMPLATES + a sourceKey).
const vid = (id: string, sourceKey: string) => ({
  key: id, createdAt: "2026-09-16T00:00:00Z", label: "Video",
  manifest: { id, template: "video-timeline", sourceKey, tracks: [{ id: `${id}-v`, type: "video" }] },
});
const analysis = (id: string, sourceKey: string, label: string) => ({
  key: id, createdAt: "2026-09-16T00:00:01Z", label,
  manifest: { id, template: "video-annotations", sourceKey, tracks: [{ id: `${id}-a`, type: "audio" }], markers: [{ id: `${id}-m` }] },
});

function tabsFor(facesKey: string) {
  const renders = [
    vid("proxy", PROXY_KEY),
    analysis("audiostem", PROXY_KEY, "Audio"),
    analysis("diarize", PROXY_KEY, "Speaker Diarization"),
    analysis("shots", PROXY_KEY, "Shot Segmentation"),
    analysis("faces", facesKey, "Face Recognition"),
    vid("original", ORIGINAL_KEY),
    vid("transcode2", TRANSCODE2_KEY),
  ];
  return composeRenderTabs(renders);
}

describe("a618f7ee render composition", () => {
  it("PROBLEM 2 (regressed): faces with a durable sourceKey does NOT join the proxy composite", () => {
    const tabs = tabsFor(FACES_DURABLE_KEY);
    const composite = tabs.find((t) => t.key === `composite:${PROXY_KEY}`);
    expect(composite, "proxy composite exists").toBeTruthy();
    // proxy + audiostem + diarize + shots = 4 members; faces is NOT absorbed.
    expect((composite!.memberKeys as string[]).sort()).toEqual(
      ["audiostem", "diarize", "proxy", "shots"],
    );
    const facesTab = tabs.find((t) => (t.manifest as { id?: string })?.id === "faces");
    expect(facesTab, "faces stands alone (the solo-tab bug)").toBeTruthy();
    expect(facesTab!.key).toBe("faces");
  });

  it("FIXED (post-revert): faces with the execUUID sourceKey merges into the proxy composite", () => {
    const tabs = tabsFor(FACES_EXECUUID_KEY);
    const composite = tabs.find((t) => t.key === `composite:${PROXY_KEY}`);
    expect(composite, "proxy composite exists").toBeTruthy();
    expect((composite!.memberKeys as string[]).sort()).toEqual(
      ["audiostem", "diarize", "faces", "proxy", "shots"],
    );
    // no standalone faces tab
    expect(tabs.find((t) => (t.manifest as { id?: string })?.id === "faces")).toBeFalsy();
  });

  it("PROBLEM 1 boundary: original and proxy are DISTINCT keys -> distinct tabs (compose layer never collapses them)", () => {
    const tabs = tabsFor(FACES_EXECUUID_KEY);
    // proxy is the composite; original + transcode2 are their own solo Video tabs.
    expect(tabs.some((t) => t.key === `composite:${PROXY_KEY}`)).toBe(true);
    const original = tabs.find((t) => (t.manifest as { id?: string })?.id === "original");
    const transcode2 = tabs.find((t) => (t.manifest as { id?: string })?.id === "transcode2");
    expect(original, "original nba.mp4 is its own tab").toBeTruthy();
    expect(transcode2, "2nd transcode is its own tab").toBeTruthy();
    // The three source videos never share a key, so composeRenderTabs cannot merge/overwrite them:
    const keys = new Set([PROXY_KEY, ORIGINAL_KEY, TRANSCODE2_KEY]);
    expect(keys.size).toBe(3);
  });
});
