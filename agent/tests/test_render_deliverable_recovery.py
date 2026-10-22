"""Deliverable recovery for the dropped-sidecar adoption race (session 08c4f282).

A shot-segmentation render arrived with the segment + keyframes but no `shots.json` (the plan node
adopted the media before the gateway's sidecar landed), so the projection produced 0 markers and
the timeline was empty — while a sibling job folder held the real 23-shot `shots.json`. The render
now recovers the declared deliverable from the session's own output tree and re-projects.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import render_agent as ra

SHOTS = {"n_shots": 3, "actual_duration_sec": 30.03, "shots": [
    {"shot_id": 0, "start_time": 0.0, "end_time": 0.967, "shot_type": "black"},
    {"shot_id": 1, "start_time": 0.967, "end_time": 12.5, "shot_type": "content"},
    {"shot_id": 2, "start_time": 12.5, "end_time": 30.03, "shot_type": "content"}]}

SHOT_SEG_SKILL = {"name": "render-shot-segmentation", "template": "video-markers",
                  "declaration": {"deliverable": "shots.json", "detect": "shots",
                                  "markers": "shots[] time=start_time end=end_time label=shot_id category=shot_type"}}

PLAIN_VIDEO_SKILL = {"name": "render-video", "template": "nle-timeline",
                     "declaration": {"deliverable": "transcoded.mp4"}}


class _FakeS3:
    """Minimal S3 stub: one shots.json under a sibling job folder of the session."""
    def __init__(self, objs: dict):
        self._objs = objs  # key -> bytes

    def get_paginator(self, _op):
        objs = self._objs
        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "LastModified": k} for k in objs if k.startswith(Prefix)]}
        return _P()

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda s: self._objs[Key]})()}


def _wire(monkeypatch, objs):
    monkeypatch.setattr(ra, "_s3", _FakeS3(objs))
    monkeypatch.setattr(ra, "S3_BUCKET", "bkt")
    monkeypatch.setattr(ra, "S3_PREFIX", "artifacts/")
    monkeypatch.setattr(ra, "ARTIFACTS_DOMAIN", "cdn.example")


SID = "08c4f282-5324-45a5-8885-6451e2fd8f5c"
MEDIA = f"https://cdn.example/artifacts/{SID}/output/39ae22f1/sg1_shot_segmentation/segment_0000.mp4"


def test_recovers_sibling_shots_json(monkeypatch):
    _wire(monkeypatch, {f"artifacts/{SID}/output/5b944718/shots.json": json.dumps(SHOTS).encode()})
    out = ra._recover_missing_deliverable(SHOT_SEG_SKILL, [MEDIA])
    assert len(out) == 1
    (url, data), = out.items()
    assert url.endswith("/5b944718/shots.json") and data["n_shots"] == 3


def test_noop_for_plain_video_skill(monkeypatch):
    # render-video declares transcoded.mp4 (not .json) + no projection -> never discovers.
    _wire(monkeypatch, {f"artifacts/{SID}/output/5b944718/shots.json": json.dumps(SHOTS).encode()})
    assert ra._recover_missing_deliverable(PLAIN_VIDEO_SKILL, [MEDIA]) == {}


def test_noop_when_deliverable_already_present(monkeypatch):
    _wire(monkeypatch, {f"artifacts/{SID}/output/5b944718/shots.json": json.dumps(SHOTS).encode()})
    cands = [MEDIA, f"https://cdn.example/artifacts/{SID}/output/39ae22f1/sg1_shot_segmentation/shots.json"]
    assert ra._recover_missing_deliverable(SHOT_SEG_SKILL, cands) == {}


def test_noop_without_s3(monkeypatch):
    monkeypatch.setattr(ra, "_s3", None)
    assert ra._recover_missing_deliverable(SHOT_SEG_SKILL, [MEDIA]) == {}


def test_recovered_shots_project_to_markers(monkeypatch):
    # End state: the recovered shots.json, fed to the media builder, projects markers (was 0).
    _wire(monkeypatch, {f"artifacts/{SID}/output/5b944718/shots.json": json.dumps(SHOTS).encode()})
    recovered = ra._recover_missing_deliverable(SHOT_SEG_SKILL, [MEDIA])
    url = next(iter(recovered))
    manifest = ra._build_media_manifest([MEDIA, url], "mid-x", {}, prefetched=recovered)
    assert len(manifest.get("markers") or []) == 3
