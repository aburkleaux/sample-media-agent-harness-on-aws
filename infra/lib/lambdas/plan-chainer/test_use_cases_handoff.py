"""END-TO-END USE-CASE HARNESS — handoff side (design §3 A1/A2 + §5 recovery).

Local, deterministic reproduction of every user use case at the inter-node handoff boundary — NO
LLM, NO gateway, NO deploy. Each test builds the realistic producer output shape and asserts the
consumer is handed a DETERMINISTIC reference: A1 always yields the producer's persisted-file uri +
a read instruction; A2 yields the EXACT named deliverable uris (JSON transcripts/faces/shots AND
media stems/video) so the consumer never guesses a filename; §5 fails a genuinely missing input
fast instead of retrying by guessing.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "t")
os.environ.setdefault("JOB_REGISTRY_TABLE", "t")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402


class _FileS3:
    def __init__(self, files):
        self.files = files

    def get_object(self, Bucket, Key):
        import io
        if Key in self.files:
            return {"Body": io.BytesIO(self.files[Key].encode("utf-8"))}
        raise Exception("NoSuchKey")

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        keys = [k for k in self.files if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bkt")


def _inject(dep_id, value, consumer="consumer", s3_files=None, key=None):
    key = key or f"plans/S/P/{dep_id}/output"
    handler._s3 = _FileS3(s3_files or {key: "{}"})
    node = {"id": consumer, "depends_on": [dep_id]}
    outputs = {dep_id: {"status": "completed", "value": value}}
    states = {dep_id: {"result_s3_key": key}}
    return handler._dependency_injection_text(node, outputs, states, "S", "P")


def _gateway(prefix, data):
    return {"output": {"s3uri": prefix}, "data": data}


def _deliverables(text):
    return text.split("DELIVERABLE FILES", 1)[1] if "DELIVERABLE FILES" in text else ""


# ---------------------------------------------------------------------------
# 1. Research -> PDF: the author's 21 KB doc lives in the persisted file; its value
#    is a small placeholder. format_pdf must get the file reference (A1).
# ---------------------------------------------------------------------------
def test_uc_research_to_pdf_gets_file_reference():
    key = "plans/S/P/author_summary/output"
    full_doc = "# Report\n\n" + ("Body paragraph. " * 2000)
    placeholder = {"summary_document_text": "…full document delivered in the response above."}
    text = _inject("author_summary", placeholder, consumer="format_pdf",
                   s3_files={key: full_doc}, key=key)
    assert key in text and "read" in text.lower()          # deterministic file reference + read instr


# ---------------------------------------------------------------------------
# 2. Extract stems: consumer of a stem job gets the EXACT .wav stem uris (A2 media).
# ---------------------------------------------------------------------------
def test_uc_stems_handoff_exact_wav_uris():
    value = _gateway("s3://bkt/mw/audio-stem-separation/EXEC/", {
        "audiostem_sep_output": {
            "speech_key": "mw/audio-stem-separation/EXEC/speech.wav",
            "music_key": "mw/audio-stem-separation/EXEC/music.wav",
            "sfx_key": "mw/audio-stem-separation/EXEC/sfx.wav",
        },
    })
    deliverables = _deliverables(_inject("stem_separation", value, consumer="remix"))
    for stem in ("speech.wav", "music.wav", "sfx.wav"):
        assert f"s3://bkt/mw/audio-stem-separation/EXEC/{stem}" in deliverables


# ---------------------------------------------------------------------------
# 3. Detect people / faces: consumer gets exact JSON deliverable uris (A2).
# ---------------------------------------------------------------------------
def test_uc_faces_handoff_exact_json_uris():
    value = _gateway("s3://bkt/mw/face-recognition/EXEC/", {
        "facemetadata_key": "mw/face-recognition/EXEC/facemetadata.json",
        "face_tracks_key": "mw/face-recognition/EXEC/face_tracks.json",
    })
    deliverables = _deliverables(_inject("face_recognition", value, consumer="annotate"))
    assert "s3://bkt/mw/face-recognition/EXEC/facemetadata.json" in deliverables
    assert "s3://bkt/mw/face-recognition/EXEC/face_tracks.json" in deliverables


# ---------------------------------------------------------------------------
# 4. Shot segmentation: consumer gets the exact shots.json uri (A2).
# ---------------------------------------------------------------------------
def test_uc_shots_handoff_exact_json_uri():
    value = _gateway("s3://bkt/mw/shot-segmentation/EXEC/", {
        "shots_key": "mw/shot-segmentation/EXEC/shots.json",
    })
    deliverables = _deliverables(_inject("shot_segmentation", value, consumer="chapterize"))
    assert "s3://bkt/mw/shot-segmentation/EXEC/shots.json" in deliverables


# ---------------------------------------------------------------------------
# 5. Multi-step transition: node B (apply transition) depends on node A (rendered
#    clip). B must get A's exact video deliverable uri, not guess it (A2 media).
# ---------------------------------------------------------------------------
def test_uc_multistep_transition_chain_hands_over_exact_video():
    value = _gateway("s3://bkt/mw/render-video/EXEC/", {
        "render_output": {"video_key": "mw/render-video/EXEC/scene_a.mp4"},
    })
    deliverables = _deliverables(_inject("render_scene_a", value, consumer="apply_transition"))
    assert "s3://bkt/mw/render-video/EXEC/scene_a.mp4" in deliverables


# ---------------------------------------------------------------------------
# 6. List-a-bucket fan-out: each fanned consumer gets ITS producer's file reference
#    (references are per-dependency, so N parallel files never cross-contaminate).
# ---------------------------------------------------------------------------
def test_uc_bucket_fanout_each_consumer_gets_its_own_producer_reference():
    for i in range(4):
        dep = f"probe_file_{i}"
        key = f"plans/S/P/{dep}/output"
        text = _inject(dep, {"note": f"probe {i}"}, consumer=f"summarize_{i}",
                       s3_files={key: f"probe result {i}"}, key=key)
        assert key in text                                 # its own producer, deterministically
        assert all(f"probe_file_{j}/output" not in text for j in range(4) if j != i)  # no cross-talk


# ---------------------------------------------------------------------------
# 7. Self-recovery: a genuinely missing input fails fast (no retry-by-guessing);
#    a correctable parameter error still retries (design §5).
# ---------------------------------------------------------------------------
def test_uc_missing_input_not_retried_but_bad_param_is():
    assert handler._should_retry_node({"status": "failed", "retries": 0,
                                       "error": "input final_transcript.json does not exist"}) is False
    assert handler._should_retry_node({"status": "failed", "retries": 0,
                                       "error": "duration must be >= 10"}) is True
