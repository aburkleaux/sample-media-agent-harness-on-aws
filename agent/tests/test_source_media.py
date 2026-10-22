"""The source-media extractor must recover the analysed media from EVERY media job's
input — the same rule for faces, shots, diarization and stems — with no per-tool logic.
This is the consistency contract behind design #1: capture once at registration, so the
async completion render can overlay the analysis on the media it describes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "source_media_under_test", Path(__file__).resolve().parents[1] / "src" / "media" / "source_media.py"
)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

VIDEO = "s3://m2cv6-proxy/micro-workflow/media-processing/46a387c9/proxy/transcoded.mp4"
AUDIO = "s3://m2cv6-proxy/micro-workflow/media-processing/46a387c9/proxy/audio_48k.wav"


def test_face_recognition_input_arg():
    # face-recognition___recognize_faces(input_s3_uri=...)
    assert sm.extract_source_media({"input_s3_uri": VIDEO, "collection_id": ""}) == [VIDEO]


def test_shot_segmentation_input_arg():
    # shot-segmentation start tool — a differently named media key, same rule.
    assert sm.extract_source_media({"video_uri": VIDEO, "frames_to_shots_cfg": {"segment_duration_secs": 1200}}) == [VIDEO]


def test_speaker_diarization_input_arg():
    assert sm.extract_source_media({"media_s3_uri": VIDEO, "language": "en"}) == [VIDEO]


def test_audio_stem_separation_input_arg():
    assert sm.extract_source_media({"audio_key": AUDIO}) == [AUDIO]


def test_plan_node_prompt_embeds_the_source_uri():
    # Plan nodes carry the source inside a free-text prompt, not a clean arg.
    prompt = (f"Start shot boundary and shot-group detection on {VIDEO} using "
              f"shot-segmentation___segment_shots. Return the job_id/execution ARN.")
    assert sm.extract_source_media({"prompt": prompt, "tools": ["shot-segmentation___segment_shots"]}) == [VIDEO]


def test_upstream_json_deliverable_is_not_a_source():
    # A consumer node handed an upstream .json deliverable must NOT treat it as media.
    val = {"input_s3_uri": VIDEO,
           "diarization_manifest": "s3://m2cv6-proxy/micro-workflow/diarize/exec/global_speakers.json"}
    assert sm.extract_source_media(val) == [VIDEO]


def test_non_s3_and_config_values_ignored():
    assert sm.extract_source_media({"language": "en", "threshold": 0.8, "url": "https://example.com/x.mp4"}) == []


def test_presigned_query_string_tolerated():
    signed = VIDEO + "?X-Amz-Signature=abc&X-Amz-Expires=3600"
    assert sm.extract_source_media({"input": signed}) == [signed]


def test_dedupes_repeated_source():
    assert sm.extract_source_media({"a": VIDEO, "b": {"c": VIDEO}}) == [VIDEO]


def test_exts_stay_in_sync_with_render_hook():
    # The extractor and the render hook's source recogniser must agree, or a source
    # captured here would be dropped there (or vice versa).
    prov = importlib.util.spec_from_file_location(
        "provider_ext_probe", Path(__file__).resolve().parents[1] / "src" / "hooks" / "provider.py")
    # Read the constant literally to avoid importing provider's heavy deps.
    text = (Path(__file__).resolve().parents[1] / "src" / "hooks" / "provider.py").read_text()
    assert all(ext.strip('"') in text for ext in [f'"{e}"' for e in sm.SOURCE_MEDIA_EXTS])


# --- P1: malformed opaque-id guard in a media source path ---------------------------------
# The uploaded-media convention is artifacts/<uuid>/input/<file>. A model reproducing that URI
# from context can corrupt the opaque UUID (doubled char), and the media job then 404s deep in
# probe_media_file. malformed_media_s3uris flags it at the source as a correctable signal.

_BAD = "s3://b/artifacts/1f0a39ee-4c64-4987-85b0-bcddb89ee4ce8/input/1789767653381-nba.mp4"   # 37-char id
_OK = "s3://b/artifacts/1f0a39ee-4c64-4987-85b0-bcddb89e4ce8/input/1789767653381-nba.mp4"     # 36-char id


def test_malformed_media_s3uris_flags_doubled_char_actor_id():
    assert sm.malformed_media_s3uris(_BAD) == [_BAD]
    assert sm.malformed_media_s3uris({"s3uri": _BAD}) == [_BAD]      # nested in input args
    assert sm.malformed_media_s3uris(f"analyse {_BAD} please") == [_BAD]  # embedded in a prompt


def test_malformed_media_s3uris_accepts_wellformed_uuid():
    assert sm.malformed_media_s3uris(_OK) == []
    assert sm.malformed_media_s3uris({"input_s3_uri": _OK}) == []


def test_malformed_media_s3uris_ignores_non_media_and_no_artifacts_segment():
    # a produced .json deliverable is not source media -> never inspected, even if its seg is bad
    assert sm.malformed_media_s3uris("s3://b/artifacts/BADSEG/output/final_transcript.json") == []
    # a media URI not under artifacts/<id>/ has no opaque id segment to validate -> not flagged
    assert sm.malformed_media_s3uris("s3://m2cv6-proxy/micro-workflow/media-processing/46a387c9/proxy/x.mp4") == []
