"""Render manifests fold onto their SOURCE (asset-lineage R7.1-R7.3, design §4).

* Analysis renders (shots / diarization / faces) put the source video FIRST:
  ``timeline_media = source_video_urls + media_urls``, and the audio-only trim does not apply
  when the skill projects segments/markers (staging 833a079f: the diarization tab was audio-only,
  faces were drawn on a still with 0 of 181 boxes).
* Shot markers are built in CODE from shots.json, never the "JSON is data, not a view" bail-out or
  an LLM marker (staging 6ac33e9e: one ``{track_id, source: shots.json, type: shots}`` marker).
* Manifests carry ``sourceLocation``: the canonical ``s3://bucket/key`` of the source media.
* Richer wins: a manifest write never replaces one with more markers+segments+annotations.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render_agent as ra  # noqa: E402
import render.artifacts as render_artifacts  # noqa: E402
from render import richness  # noqa: E402

BUCKET = "skills-bucket"
CDN = "d123.cloudfront.net"
SID = "833a079f-1111-4222-8333-444455556666"
INVENTED = "e34ebb5c-aaaa-4bbb-8ccc-ddddeeeeffff"
CLIP = f"https://{CDN}/artifacts/{INVENTED}/output/clip_00_12.mp4"
CLIP_LOC = f"s3://{BUCKET}/artifacts/{INVENTED}/output/clip_00_12.mp4"
M2C = "https://m2c-proxy.s3.us-west-2.amazonaws.com/micro-workflow"

SHOTS = {"n_shots": 3, "actual_duration_sec": 12.5, "shots": [
    {"shot_id": 0, "start_time": 0.0, "end_time": 4.0, "shot_type": "content"},
    {"shot_id": 1, "start_time": 4.0, "end_time": 8.2, "shot_type": "content"},
    {"shot_id": 2, "start_time": 8.2, "end_time": 12.5, "shot_type": "black"},
]}
TRANSCRIPT = {"num_global_speakers": 2, "sentences": [
    {"start": 0.5, "end": 3.0, "speaker_id": "SPEAKER_00", "text": "hello"},
    {"start": 3.5, "end": 6.0, "speaker_id": "SPEAKER_01", "text": "hi"},
]}
FACEGROUP = {"stats": {"num_people": 1, "num_tracks": 1, "faces_detected": 2}, "people": [
    {"name": "Kevin Kilner", "tracks": [{"faces": [
        {"timestamp": 1.0, "frame_key": "frames/f_0001.jpg", "box": {"l": 0.1, "t": 0.1, "w": 0.2, "h": 0.3}},
        {"timestamp": 2.0, "frame_key": "frames/f_0002.jpg", "box": {"l": 0.2, "t": 0.1, "w": 0.2, "h": 0.3}},
    ]}], "appearances": [{"begin_ms": 1000, "end_ms": 2000}]},
]}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CDN)
    monkeypatch.setattr(ra, "_container_duration", lambda url, timeout=4.0: 12.5)
    monkeypatch.setattr(ra, "media_probe", lambda url: "{}")
    monkeypatch.setattr(ra, "peak_extractor", lambda url, n: "{}")


def _assemble(artifacts, sources, tool, fetched):
    m, _projected, _skill = ra.assemble_manifest(
        artifact_urls=artifacts, source_urls=sources, tool=tool, manifest_id="m", fetched=fetched)
    return m


def _video_srcs(m):
    return [t["src"] for t in m.get("tracks") or [] if t.get("type") == "video"]


# --------------------------------------------------------------------- source-first analysis tracks

def test_shots_render_puts_the_source_video_first_over_its_proxy_segment():
    seg = f"{M2C}/shot/abc/segment_0000.mp4?X-Amz-Signature=s"
    shots = f"{M2C}/shot/abc/shots.json?X-Amz-Signature=s"
    m = _assemble([seg, shots], [CLIP], "shot-segmentation___get_job_status", {shots: SHOTS})
    assert m["tracks"][0]["src"] == CLIP
    assert len(m["markers"]) == 3
    assert m["sourceLocation"] == CLIP_LOC


def test_diarization_keeps_the_source_video_beside_speaker_audio():
    """833a079f: speaker channel audio + transcript used to render audio-only."""
    wav = f"{M2C}/diar/abc/speaker_00.wav"
    tr = f"{M2C}/diar/abc/final_transcript.json"
    m = _assemble([wav, tr], [CLIP], "speech-diarization___get_job_status", {tr: TRANSCRIPT})
    assert _video_srcs(m) == [CLIP], m.get("tracks")
    assert m["tracks"][0]["src"] == CLIP
    assert len(m["segments"]) == 2
    assert m["sourceLocation"] == CLIP_LOC


def test_a_plain_audio_deliverable_still_trims_a_co_present_video():
    """Non-analysis renders (stems) keep the audio-only rule: the video is the input."""
    stem = f"{M2C}/stems/abc/vocals.wav"
    m = _assemble([stem, f"{M2C}/stems/abc/original.mp4"], [], None, {})
    assert _video_srcs(m) == []


def test_face_boxes_are_drawn_on_the_source_video_when_one_exists():
    fg = f"{M2C}/faces/abc/facegroup.json"
    stills = [f"{M2C}/faces/abc/frames/f_0001.jpg", f"{M2C}/faces/abc/frames/f_0002.jpg"]
    m = _assemble([fg, *stills], [CLIP], "face-recognition___get_job_status", {fg: FACEGROUP})
    assert m.get("template") != "image-annotated"
    assert _video_srcs(m)[:1] == [CLIP]
    assert len(m.get("annotations") or []) == 2
    assert m["sourceLocation"] == CLIP_LOC


def test_deterministic_entrypoint_diarization_keeps_the_source_video(monkeypatch):
    cap = {}
    monkeypatch.setattr(ra, "_write_manifest_to_s3",
                        lambda mj, sid, mid: (cap.update(json.loads(mj)), f"artifacts/{sid}/render/{mid}.json")[1])
    wav = f"{M2C}/diar/abc/speaker_00.wav"
    tr = f"{M2C}/diar/abc/final_transcript.json"
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: (TRANSCRIPT if u == tr else None) for u in urls})
    prompt = (f"Render these:\n{wav}\n{tr}\n\n{render_artifacts.SOURCE_MEDIA_PROMPT_HEADING}\n{CLIP}\n")
    _run_det({"prompt": prompt, "session_id": SID, "manifest_id": "diar-1",
              "tool": "speech-diarization___get_job_status"})
    assert cap["tracks"][0]["src"] == CLIP
    assert len(cap["segments"]) == 2
    assert cap["sourceLocation"] == CLIP_LOC


# --------------------------------------------------------------------- shots.json -> markers in code

def _run_det(payload):
    async def run():
        return [ev async for ev in ra._render_deterministic(payload, {})]
    events = asyncio.new_event_loop().run_until_complete(run())
    return [json.loads(e) for e in events if isinstance(e, str) and e.strip().startswith("{")]


@pytest.mark.parametrize("catalog", ["discovered", "unavailable"])
def test_6ac33e9e_shots_json_alone_renders_shot_markers_not_a_bail_out(monkeypatch, catalog):
    """6ac33e9e: a shots.json-only render bailed 'JSON is data, not a view' and an LLM fallback
    wrote ONE undrawable marker. The markers are built in code, catalog or no catalog."""
    if catalog == "unavailable":
        monkeypatch.setattr(ra, "_discover_render_skills", lambda: [])
    shots = f"{M2C}/shot/abc/shots.json?X-Amz-Signature=s"
    cap = {}
    monkeypatch.setattr(ra, "_write_manifest_to_s3",
                        lambda mj, sid, mid: (cap.update(json.loads(mj)), f"artifacts/{sid}/render/{mid}.json")[1])
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: SHOTS for u in urls})
    events = _run_det({"prompt": f"Render these:\n{shots}\n", "session_id": SID,
                       "manifest_id": "shots-1", "tool": "shot-segmentation___get_job_status"})
    refs = [e for e in events if e.get("type") == "output_ref"]
    assert refs and not refs[0].get("error"), events
    assert not any("JSON is data" in str(e.get("label")) for e in events)
    assert cap["markers"] == [
        {"time": 0.0, "endTime": 4.0, "label": "0", "category": "content", "id": "m-0"},
        {"time": 4.0, "endTime": 8.2, "label": "1", "category": "content", "id": "m-1"},
        {"time": 8.2, "endTime": 12.5, "label": "2", "category": "black", "id": "m-2"},
    ]
    assert cap["duration"] == 12.5
    assert cap["template"] in ("nle-timeline", "composite-timeline")


def test_shot_markers_helper_matches_the_declared_mapping():
    markers, duration = ra._shot_markers_from_shots_json(SHOTS)
    assert [(m["time"], m.get("endTime"), m["label"], m["category"]) for m in markers] == [
        (0.0, 4.0, "0", "content"), (4.0, 8.2, "1", "content"), (8.2, 12.5, "2", "black")]
    assert duration == 12.5
    assert ra._shot_markers_from_shots_json({"faces": []}) == ([], None)


# --------------------------------------------------------------------- sourceLocation

@pytest.mark.parametrize("source,expected", [
    (CLIP, CLIP_LOC),
    (f"https://{BUCKET}.s3.amazonaws.com/projects/p1/a1/v.mp4?X-Amz-Signature=x", f"s3://{BUCKET}/projects/p1/a1/v.mp4"),
    (f"{M2C}/trim/abc/clip.mp4?X-Amz-Signature=x", "s3://m2c-proxy/micro-workflow/trim/abc/clip.mp4"),
])
def test_analysis_manifest_source_location_is_canonical(source, expected):
    shots = f"{M2C}/shot/abc/shots.json"
    m = _assemble([shots], [source], "shot-segmentation___get_job_status", {shots: SHOTS})
    assert m["sourceLocation"] == expected


def test_a_plain_clip_render_locates_itself():
    m = _assemble([CLIP], [f"https://{CDN}/projects/p1/a1/source.mp4"], None, {})
    assert m["sourceLocation"] == CLIP_LOC


def test_an_unlocatable_source_stamps_no_location():
    m = _assemble(["https://v3.fal.media/files/x.mp4"], [], None, {})
    assert "sourceLocation" not in m


# --------------------------------------------------------------------- richer wins

class _S3:
    def __init__(self, existing=None):
        self.objects = {} if existing is None else dict(existing)
        self.puts = []

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self.puts.append(Key)
        self.objects[Key] = json.loads(Body)


RICH = {"id": "shots", "markers": [{"time": float(i)} for i in range(23)]}
POOR = {"id": "shots", "markers": [{"track_id": "t", "source": "shots.json", "type": "shots"}]}


def test_populated_items_counts_markers_segments_annotations():
    assert richness.populated_items({"markers": [1, 2], "segments": [1], "annotations": [1], "rows": [1]}) == 4
    assert richness.populated_items(None) == 0


def test_render_agent_writer_keeps_a_richer_existing_manifest(monkeypatch):
    key = f"artifacts/{SID}/render/shots.json"
    s3 = _S3({key: RICH})
    monkeypatch.setattr(ra, "_s3", s3)
    monkeypatch.setattr(ra, "S3_BUCKET", BUCKET)
    assert ra._write_manifest_to_s3(json.dumps(POOR), SID, "shots") == key  # still announced
    assert s3.puts == [] and len(s3.objects[key]["markers"]) == 23


def test_render_agent_writer_replaces_a_poorer_or_equal_or_absent_manifest(monkeypatch):
    key = f"artifacts/{SID}/render/shots.json"
    s3 = _S3({key: POOR})
    monkeypatch.setattr(ra, "_s3", s3)
    monkeypatch.setattr(ra, "S3_BUCKET", BUCKET)
    assert ra._write_manifest_to_s3(json.dumps(RICH), SID, "shots") == key
    assert ra._write_manifest_to_s3(json.dumps(RICH), SID, "shots") == key  # equal -> write
    assert ra._write_manifest_to_s3(json.dumps({"id": "new"}), SID, "new") == f"artifacts/{SID}/render/new.json"
    assert s3.puts == [key, key, f"artifacts/{SID}/render/new.json"]


def test_session_manifest_writer_keeps_a_richer_existing_manifest(monkeypatch):
    key = f"artifacts/{SID}/render/shots.json"
    s3 = _S3({key: RICH})
    monkeypatch.setattr(render_artifacts, "_s3_client", lambda: s3)
    monkeypatch.setattr(render_artifacts, "_SKILLS_BUCKET", BUCKET)
    assert render_artifacts.write_session_manifest(SID, "shots", POOR) == key
    assert s3.puts == []
    assert render_artifacts.write_session_manifest(SID, "moments", {"id": "moments"}) \
        == f"artifacts/{SID}/render/moments.json"


def test_an_unreadable_existing_manifest_never_blocks_the_write(monkeypatch):
    class _Broken(_S3):
        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": io.BytesIO(b"not json")}
    s3 = _Broken()
    monkeypatch.setattr(ra, "_s3", s3)
    monkeypatch.setattr(ra, "S3_BUCKET", BUCKET)
    assert ra._write_manifest_to_s3(json.dumps(POOR), SID, "shots")
    assert s3.puts == [f"artifacts/{SID}/render/shots.json"]


VIEW = {"range": {"start": 1.0, "end": 4.0}, "frameMs": 1500, "crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}}


def test_a_rerender_keeps_the_users_saved_view_state(monkeypatch):
    """A richer re-render replaces the manifest but never wipes the user's range/marker/crop (design §6)."""
    key = f"artifacts/{SID}/render/shots.json"
    s3 = _S3({key: {**POOR, "viewState": VIEW}})
    monkeypatch.setattr(ra, "_s3", s3)
    monkeypatch.setattr(ra, "S3_BUCKET", BUCKET)
    assert ra._write_manifest_to_s3(json.dumps(RICH), SID, "shots") == key
    assert len(s3.objects[key]["markers"]) == 23 and s3.objects[key]["viewState"] == VIEW


def test_session_writer_keeps_view_state_and_a_new_view_state_wins(monkeypatch):
    key = f"artifacts/{SID}/render/moments.json"
    s3 = _S3({key: {"id": "moments", "viewState": VIEW}})
    monkeypatch.setattr(render_artifacts, "_s3_client", lambda: s3)
    monkeypatch.setattr(render_artifacts, "_SKILLS_BUCKET", BUCKET)
    render_artifacts.write_session_manifest(SID, "moments", {"id": "moments", "items": [1]})
    assert s3.objects[key]["viewState"] == VIEW
    newer = {"range": {"start": 0.0, "end": 2.0}}
    render_artifacts.write_session_manifest(SID, "moments", {"id": "moments", "viewState": newer})
    assert s3.objects[key]["viewState"] == newer


def test_a_richer_manifest_of_another_source_never_blocks_the_write(monkeypatch):
    """Staging session d6a560a8: NBA's 97-sentence diarization was kept over Meridian's 86 at one key."""
    key = f"artifacts/{SID}/render/sg2.json"
    nba = {"id": "sg2", "sourceKey": "video:a089f125/nba.mp4", "segments": [{"t": i} for i in range(97)],
           "viewState": VIEW}
    meridian = {"id": "sg2", "sourceKey": "video:4b8511d6/meridian.mp4", "segments": [{"t": i} for i in range(86)]}
    s3 = _S3({key: nba})
    monkeypatch.setattr(ra, "_s3", s3)
    monkeypatch.setattr(ra, "S3_BUCKET", BUCKET)
    assert ra._write_manifest_to_s3(json.dumps(meridian), SID, "sg2") == key
    assert s3.objects[key]["sourceKey"] == "video:4b8511d6/meridian.mp4"
    assert "viewState" not in s3.objects[key]  # another video's range/crop is not this one's


def test_same_source_compares_the_first_identity_both_carry():
    assert richness.same_source({"sourceAssetId": "a", "sourceKey": "x"}, {"sourceAssetId": "a", "sourceKey": "y"})
    assert not richness.same_source({"sourceLocation": "s3://b/1.mp4"}, {"sourceLocation": "s3://b/2.mp4"})
    assert richness.same_source({"sourceKey": "x"}, {"markers": []})  # unknown counts as the same
    assert richness.same_source(None, {"sourceKey": "x"})
