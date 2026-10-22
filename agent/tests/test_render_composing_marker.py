"""Tests for the pre-render composing animation fix + manifest stable references.

Animation: the composing marker is emitted at BUFFER time (loop free) so the progress
poller flushes it live, instead of at the blocking session_end dispatch (which starves
the poller and batches marker+manifest, so the animation never paints). Every emitted
marker must get a terminal signal so a deduped render can't orphan a spinning tab.

Stable references: a render manifest's expiring presigned deliverable URLs are rewritten to
stable artifact references, so a reloaded session never holds a dead URL.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration.progress_channel import ProgressChannel, set_active_channel  # noqa: E402
import hooks.actions as A  # noqa: E402
import render.artifacts as R  # noqa: E402
import pytest  # noqa: E402

# Captured at import (before any test runs): another suite reassigns
# render_artifacts.dispatch_render directly (not via monkeypatch), which leaks. Restore
# the real one per-test so this suite is order-independent.
_REAL_DISPATCH_RENDER = R.dispatch_render


@pytest.fixture(autouse=True)
def _isolate_render_state():
    """Reset module-level render state so these tests never leak into others
    (a left-open batch makes render_action buffer instead of dispatch)."""
    R.dispatch_render = _REAL_DISPATCH_RENDER
    A._render_batch.clear()
    A._batch_open = False
    R._rendered_url_sets.clear()
    set_active_channel(None)
    yield
    A._render_batch.clear()
    A._batch_open = False
    R._rendered_url_sets.clear()
    set_active_channel(None)


# --- Animation: buffer-time marker + rid threading ---

def test_buffer_render_emits_marker_with_rid_and_stores_it():
    ch = ProgressChannel()
    set_active_channel(ch)
    try:
        A.open_render_batch()
        A._render_batch.clear()
        A._buffer_render(["https://ex/clip.mp4"], [], "Detect Faces")
        markers = [e for e in ch.drain()
                   if e.status == "running" and e.extra.get("render_request_id")]
        assert len(markers) == 1, "exactly one composing marker at buffer time"
        rid = markers[0].extra["render_request_id"]
        assert markers[0].extra.get("render_label") == "Detect Faces"
        slot = next(iter(A._render_batch.values()))
        assert slot["rid"] == rid, "the slot carries the same rid for correlation"
    finally:
        set_active_channel(None)
        A._render_batch.clear()


def test_buffer_render_one_marker_per_slot_not_per_url():
    ch = ProgressChannel()
    set_active_channel(ch)
    try:
        A._render_batch.clear()
        A._buffer_render(["https://ex/a.mp4", "https://ex/b.mp4"], [], "Segment Shots")
        markers = [e for e in ch.drain()
                   if e.status == "running" and e.extra.get("render_request_id")]
        assert len(markers) == 1, "two urls in one (label, class) slot -> one marker/tab"
    finally:
        set_active_channel(None)
        A._render_batch.clear()


# --- Animation: rid contract in dispatch_render ---

def test_dispatch_render_with_rid_does_not_double_announce_and_resolves_orphan():
    ch = ProgressChannel()
    set_active_channel(ch)
    urls = ["https://ex/dup.png"]
    try:
        R._rendered_url_sets.add(frozenset(urls))  # force the dedup bail
        out = R.dispatch_render(urls, render_request_id="RID123")
        assert out is None
        evs = ch.drain()
        # The buffer-time caller already announced the marker; dispatch must NOT emit a
        # second "running" composing marker for the same rid.
        running = [e for e in evs if e.status == "running"
                   and e.extra.get("render_request_id") == "RID123"
                   and not e.extra.get("render_failed")]
        assert running == [], "no duplicate composing marker when a rid is supplied"
        # But it MUST resolve the orphaned marker with a terminal render_failed.
        failed = [e for e in evs if e.extra.get("render_failed")
                  and e.extra.get("render_request_id") == "RID123"]
        assert len(failed) == 1, "a supplied rid always gets a terminal signal"
    finally:
        set_active_channel(None)
        R._rendered_url_sets.discard(frozenset(urls))


# --- Stable references: manifest rewrite ---
#
# Phase 2 (asset-lineage D4/R4): the pass no longer COPIES bytes. A presigned M2C deliverable
# becomes an M2C_REFERENCE (`artifacts/m2c/<bucket>/<key>`) that the proxy presigns at view time,
# so the manifest holds an identifier that never expires and the M2C original is never duplicated.

M2C_BUCKET = "m2cv6-proxy-bucket"


def _fake_s3(manifest: dict, puts: list):
    class _Body:
        def read(self, *a):
            return json.dumps(manifest).encode()

    class _S3:
        def get_object(self, Bucket, Key):
            return {"Body": _Body()}

        def head_object(self, Bucket, Key):
            raise RuntimeError("absent")

        def put_object(self, **kw):
            puts.append(kw)

    return _S3()


def _ref_env(monkeypatch):
    monkeypatch.setattr(R, "_SKILLS_BUCKET", "skills-bkt")
    monkeypatch.setattr(R, "_ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(R, "_ARTIFACTS_PREFIX", "artifacts/")


def test_presigned_m2c_url_becomes_a_reference_and_is_written_back(monkeypatch):
    _ref_env(monkeypatch)
    manifest = {
        "tracks": [{"src": f"https://{M2C_BUCKET}.s3.us-west-2.amazonaws.com/o.mp4?X-Amz-Signature=z"}],
        "images": ["https://cdn.example.net/artifacts/already.png"],  # already stable
    }
    puts: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: _fake_s3(manifest, puts))

    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")

    assert [p["Key"] for p in puts] == ["artifacts/s/render/m.json"], \
        "the manifest is the ONLY object written — no byte mirror"
    written = json.loads(puts[0]["Body"])
    assert written["tracks"][0]["src"] == f"artifacts/m2c/{M2C_BUCKET}/o.mp4"
    assert written["images"][0] == "https://cdn.example.net/artifacts/already.png"


def test_unnameable_object_keeps_its_presigned_url(monkeypatch):
    """Per-field fallback: a URL we cannot turn into a bucket + key is left as it was."""
    _ref_env(monkeypatch)
    # An S3 REST host with no object path names no key, so no reference can be built.
    orig = "https://b.s3.us-west-2.amazonaws.com/?X-Amz-Signature=z"
    manifest = {"tracks": [{"src": orig}]}
    puts: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: _fake_s3(manifest, puts))
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")
    assert puts == [], "nothing changed, so no manifest write-back"


# --- Correlation: the buffer-time rid is exactly what dispatch receives ---

def test_buffer_marker_rid_is_threaded_into_dispatch(monkeypatch):
    """The tab-fill contract: the rid announced at buffer time must be the rid handed
    to dispatch_render (which then stamps it on the manifest — same variable)."""
    ch = ProgressChannel()
    set_active_channel(ch)
    captured: dict = {}

    def _fake_dispatch(urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None):
        captured["rid"] = render_request_id
        captured["urls"] = list(urls)
        return "mkey"

    monkeypatch.setattr(R, "dispatch_render", _fake_dispatch)
    A._buffer_render(["https://ex/z.mp4"], [], "Detect Faces")
    marker = [e for e in ch.drain() if e.extra.get("render_request_id")][0]
    A.flush_render_batch()
    assert captured["rid"] == marker.extra["render_request_id"], \
        "flush must pass the buffer-time rid so the manifest fills the right composing tab"


# --- Stable-reference edge cases ---

def test_s3_and_non_aws_urls_are_left_alone(monkeypatch):
    _ref_env(monkeypatch)
    manifest = {"tracks": [
        {"src": "s3://bucket/raw.mp4"},                     # not http -> already stable
        {"src": "https://evil.example.com/x.mp4?sig=1"},    # not an S3 host -> names no object
    ]}
    puts: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: _fake_s3(manifest, puts))
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")
    assert puts == [], "manifest unchanged"


def test_same_object_across_fields_gets_one_reference(monkeypatch):
    _ref_env(monkeypatch)
    same = f"https://{M2C_BUCKET}.s3.us-west-2.amazonaws.com/o.mp4?X-Amz-Signature=A"
    same2 = f"https://{M2C_BUCKET}.s3.us-west-2.amazonaws.com/o.mp4?X-Amz-Signature=B"
    manifest = {"tracks": [{"src": same, "poster": same2}]}
    puts: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: _fake_s3(manifest, puts))
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")
    written = json.loads([p for p in puts if p["Key"].endswith("m.json")][0]["Body"])
    assert written["tracks"][0]["src"] == written["tracks"][0]["poster"] \
        == f"artifacts/m2c/{M2C_BUCKET}/o.mp4", \
        "both fields name the one object, independent of the presign query"


def test_poster_and_image_dicts_are_rewritten(monkeypatch):
    _ref_env(monkeypatch)
    u = f"https://{M2C_BUCKET}.s3.us-west-2.amazonaws.com/frame.png?X-Amz-Signature=z"
    manifest = {"images": [{"src": u}], "poster": u, "src": u}
    puts: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: _fake_s3(manifest, puts))
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")
    written = json.loads([p for p in puts if p["Key"].endswith("m.json")][0]["Body"])
    ref = f"artifacts/m2c/{M2C_BUCKET}/frame.png"
    assert written["images"][0]["src"] == ref
    assert written["poster"] == ref
    assert written["src"] == ref


def test_noop_without_env(monkeypatch):
    monkeypatch.setattr(R, "_SKILLS_BUCKET", "")
    monkeypatch.setattr(R, "_ARTIFACTS_DOMAIN", "")
    called: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: called.append(1))
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")
    assert called == [], "feature is a no-op when the bucket/domain are unset"


def test_own_bucket_source_becomes_its_cloudfront_path(monkeypatch):
    """An object already in our artifacts bucket (e.g. the uploaded source) is rewritten to its
    CloudFront path — unchanged behaviour, and never an M2C reference."""
    _ref_env(monkeypatch)  # _SKILLS_BUCKET=skills-bkt, domain=cdn.example.net
    own = "https://skills-bkt.s3.us-west-2.amazonaws.com/artifacts/123-nba.mp4?X-Amz-Signature=z"
    manifest = {"tracks": [{"src": own}]}
    puts: list = []
    monkeypatch.setattr(R, "_s3_client", lambda: _fake_s3(manifest, puts))
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")
    written = json.loads([p for p in puts if p["Key"].endswith("m.json")][0]["Body"])
    assert written["tracks"][0]["src"] == "https://cdn.example.net/artifacts/123-nba.mp4"


def test_survives_manifest_read_failure(monkeypatch):
    _ref_env(monkeypatch)

    class _S3:
        def get_object(self, Bucket, Key):
            raise RuntimeError("no such key")

        def put_object(self, **kw):
            raise AssertionError("must not write when read failed")

    monkeypatch.setattr(R, "_s3_client", lambda: _S3())
    R._stabilize_manifest_media_urls("artifacts/s/render/m.json", "sess")  # must not raise
