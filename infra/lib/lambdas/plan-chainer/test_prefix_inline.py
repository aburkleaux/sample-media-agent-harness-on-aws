"""A downstream text-synthesis node (tools=[]) that depends on an async media job must receive
the job's actual transcript text, not just a pointer. Async jobs (speech-diarization) report only
the output PREFIX where result files land -- never the filename -- so _find_inlinable_candidates
(which matches an s3uri ENDING in a file extension) found nothing and the transcript was never
inlined. The node then got an un-fetchable pointer and refused to fabricate a translation, which
rolled up as a silent plan-level SUCCEEDED (t3_translate no-op).

These tests drive _inline_primary_text_artifact with a diarization-shaped output whose s3uri is a
PREFIX, and assert the transcript dialogue is listed-out of the prefix and inlined.

Importing `handler` requires its env + builds boto3 clients at import, so env is stubbed first.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
os.environ.setdefault("SKILLS_S3_BUCKET", "")

sys.path.insert(0, os.path.dirname(__file__))

import handler  # noqa: E402


class _FakeBody:
    def __init__(self, data: bytes):
        self._d = data

    def read(self, *_a, **_k):
        return self._d


class _FakeS3:
    """Minimal S3 stub: list a prefix, head/get objects from an in-memory map."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.listed_prefixes: list[str] = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=None, **_kw):
        self.listed_prefixes.append(Prefix)
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}

    def head_object(self, Bucket, Key, **_kw):
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key, **_kw):
        return {"Body": _FakeBody(self.objects[Key])}


_TRANSCRIPT = json.dumps({
    "segments": [
        {"speaker_id": "SPEAKER_00", "text": "Hola mundo"},
        {"speaker_id": "SPEAKER_01", "text": "Adios"},
    ]
}).encode()
_CONFIG = json.dumps({"model": "whisper", "language": "es"}).encode()


def _diarization_prefix_output():
    # The shape a diarization node persists: job metadata + an output PREFIX, no filename.
    return {
        "operation": "StateUsageTracking",
        "output": {"s3uri": "s3://bkt/artifacts/speech-diarization/66cc7498-job/"},
    }


def test_prefix_output_inlines_transcript_dialogue(monkeypatch):
    objs = {
        # config.json sorts before final_transcript.json — the fix must still pick the
        # dialogue-bearing file, not whatever lists first.
        "artifacts/speech-diarization/66cc7498-job/config.json": _CONFIG,
        "artifacts/speech-diarization/66cc7498-job/final_transcript.json": _TRANSCRIPT,
    }
    fake = _FakeS3(objs)
    monkeypatch.setattr(handler, "_s3", fake)

    out, inlined = handler._inline_primary_text_artifact(_diarization_prefix_output())

    assert inlined is True
    assert out["output"]["inline_content"] == "SPEAKER_00: Hola mundo\nSPEAKER_01: Adios"
    assert out["output"]["inline_source_key"].endswith("final_transcript.json")
    assert fake.listed_prefixes  # it actually listed the prefix


def test_prefix_with_no_inlinable_files_leaves_pointer(monkeypatch):
    # A prefix that holds only non-text artifacts must fall back to the bare pointer (no crash).
    objs = {"artifacts/speech-diarization/66cc7498-job/audio.wav": b"RIFF...."}
    monkeypatch.setattr(handler, "_s3", _FakeS3(objs))
    out, inlined = handler._inline_primary_text_artifact(_diarization_prefix_output())
    assert inlined is False
    assert "inline_content" not in out["output"]


def test_list_failure_is_fail_safe(monkeypatch):
    class _BoomS3(_FakeS3):
        def list_objects_v2(self, *a, **k):
            raise RuntimeError("AccessDenied: s3:ListBucket")

    monkeypatch.setattr(handler, "_s3", _BoomS3({}))
    out, inlined = handler._inline_primary_text_artifact(_diarization_prefix_output())
    assert inlined is False  # falls back to bare pointer rather than raising


def test_direct_file_still_wins_over_prefix(monkeypatch):
    # A dependency that DOES name a transcript file inlines it directly (no prefix listing).
    objs = {"artifacts/x/transcript.json": _TRANSCRIPT}
    fake = _FakeS3(objs)
    monkeypatch.setattr(handler, "_s3", fake)
    value = {"transcript_json": {"s3uri": "s3://bkt/artifacts/x/transcript.json"}}
    out, inlined = handler._inline_primary_text_artifact(value)
    assert inlined is True
    assert out["transcript_json"]["inline_content"] == "SPEAKER_00: Hola mundo\nSPEAKER_01: Adios"
    assert fake.listed_prefixes == []  # direct file: never listed a prefix
