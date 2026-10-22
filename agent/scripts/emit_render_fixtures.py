"""Emit real Render Agent manifests as fixtures the UI tests render.

Agent-side tests assert what the manifest contains; UI-side tests assert what the
templates consume. Neither proves the ACTUAL manifest renders, because the agent and
the SPA are separate artifacts in different languages — a shape can satisfy both
suites independently and still fail to draw. These fixtures are the shared artifact
that binds the two.

Inputs are the sidecar shapes read from real completed jobs, restricted to the
artifacts that survive the job registry's renderable filter, so a fixture cannot
represent a request production never makes.

Run: python scripts/emit_render_fixtures.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
OUT = Path(__file__).resolve().parents[2] / "ui" / "src" / "components" / "renderer" / "__fixtures__"
sys.path.insert(0, str(SRC))

SHOTS = {
    "n_shots": 9, "actual_duration_sec": 30.03, "actual_frame_count": 900,
    "shots": [
        {"shot_id": 0, "start_time": 0.0, "end_time": 0.967633, "shot_type": "black"},
        {"shot_id": 1, "start_time": 1.001, "end_time": 5.172, "shot_type": "content"},
        {"shot_id": 2, "start_time": 5.205, "end_time": 9.176, "shot_type": "content"},
    ],
}
SPEAKERS = {
    "language": "en", "language_confidence": 0.983,
    "num_global_speakers": 2, "num_sentences": 3,
    "sentences": [
        {"id": 0, "text": "So these guys just disappeared?", "start": 26.978,
         "end": 29.318, "confidence": 0.94, "speaker_id": "SPEAKER_01"},
        {"id": 1, "text": "Yeah, on that stretch of road.", "start": 30.018,
         "end": 34.015, "confidence": 0.96, "speaker_id": "SPEAKER_00"},
        {"id": 2, "text": "With the big rock.", "start": 34.5,
         "end": 36.0, "confidence": 0.93, "speaker_id": "SPEAKER_01"},
    ],
}
FACEMETA = {
    "version": "1", "duration_sec": 300.0998,
    "stats": {"frames_analyzed": 30, "faces_detected": 4, "num_tracks": 4, "num_people": 2},
    "people": [
        {"person_id": 0, "name": "Kawhi Leonard", "named": True, "name_source": "celebrity",
         "confidence": 98.68563842773438, "gender": "Male", "age_range": [28, 36],
         "total_screen_time_sec": 1.6, "num_tracks": 2,
         "thumbnail_key": "debug/people/person_0000.jpg",
         "appearances": [{"begin_ms": 62996, "end_ms": 63796},
                         {"begin_ms": 63997, "end_ms": 64797}]},
        {"person_id": 1, "name": "Person_1", "named": False, "name_source": "",
         "confidence": 0, "gender": "Female", "age_range": [20, 28],
         "total_screen_time_sec": 0.8, "num_tracks": 2,
         "thumbnail_key": "debug/people/person_0001.jpg",
         "appearances": [{"begin_ms": 10000, "end_ms": 11000}]},
    ],
}

CASES = {
    # name: (artifact urls as production delivers them, sidecar payloads)
    "shot-segmentation": (
        ["https://cdn.example/proxy/segment_0000.mp4",
         "https://cdn.example/embeddings/shots.json"],
        {"https://cdn.example/embeddings/shots.json": SHOTS},
    ),
    "speech-diarization": (
        ["https://cdn.example/audio_16k_mono.wav",
         "https://cdn.example/transcripts/final_transcript.json"],
        {"https://cdn.example/transcripts/final_transcript.json": SPEAKERS},
    ),
    "face-recognition": (
        ["https://cdn.example/facemetadata.json",
         "https://cdn.example/debug/people/person_0000.jpg",
         "https://cdn.example/debug/people/person_0001.jpg"],
        {"https://cdn.example/facemetadata.json": FACEMETA},
    ),
    # NOTE: generated IMAGES are deliberately not a case here. _build_media_manifest
    # is the deterministic media projection and returns no template for image-only
    # input; production images are rendered by the render-generic SKILL path, which
    # this script cannot reproduce. That fixture is captured from production instead
    # (generated-images.manifest.json) and is marked as captured in the UI test.
    # Audio stem separation: three WAVs and no JSON. Added after the stem node
    # produced no render tab at all, because the resolver only looked for `.json`
    # deliverables and this shape has none.
    "audio-stems": (
        ["https://cdn.example/audio_speech.wav",
         "https://cdn.example/audio_music.wav",
         "https://cdn.example/audio_sfx.wav"],
        {},
    ),
}


def build(urls: list[str], payloads: dict) -> dict:
    import render_agent as ra
    ra._fetch_json = lambda url, timeout=5.0: payloads.get(url)
    # Mime by extension, not a blanket "video/mp4". The blanket stub was only
    # correct because every case happened to be video-ish; it made an image case
    # project as video and emit a manifest with no template at all.
    _MIMES = {
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".json": "application/json",
    }

    def _probe(url: str) -> str:
        base = url.lower().split("?")[0]
        for ext, mime in _MIMES.items():
            if base.endswith(ext):
                return json.dumps({"mime_type": mime})
        return json.dumps({"mime_type": "application/octet-stream"})

    ra.media_probe = _probe
    # Production peak extraction reports the WAV's real length, which is the
    # authority for an audio timeline. Stubbing it without one would make the
    # fixture show the generic 600s fallback and misrepresent a real render.
    ra.peak_extractor = lambda url, n: json.dumps({"peaks": [], "duration": 300.0})
    m = ra._build_media_manifest(urls, f"fixture-{len(urls)}", {})
    # Stable across runs so a fixture diff means a real behaviour change.
    m["id"] = "fixture"
    m["createdAt"] = "2026-01-01T00:00:00.000Z"
    return m


def main() -> int:
    check = "--check" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    for name, (urls, payloads) in CASES.items():
        manifest = build(urls, payloads)
        text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        path = OUT / f"{name}.manifest.json"
        if check:
            if not path.exists() or path.read_text() != text:
                drift.append(name)
        else:
            path.write_text(text)
            print(f"wrote {path.relative_to(OUT.parents[4])}: template={manifest['template']}")
    if check and drift:
        print("FIXTURES STALE: " + ", ".join(drift), file=sys.stderr)
        return 1
    if check:
        print("fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
