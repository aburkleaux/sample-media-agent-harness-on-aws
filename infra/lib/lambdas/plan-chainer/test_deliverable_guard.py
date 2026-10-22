"""False-success guard: a node that reports a deliverable URI in OUR bucket which was never
written must be marked FAILED, not laundered completed (run 627fb96e — oil-paint, 6/8 images
missing while the run reported SUCCEEDED).

Pins the conservative contract: fail ONLY when a claimed our-bucket deliverable is a confirmed
404 and NONE of the claimed our-bucket objects exist; never fail on an existing object, a foreign
bucket/CDN, or an unverifiable (non-404) head error.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_dg", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_dg"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

_BUCKET = "mediaworks-harness-skills-store-x"
_DOMAIN = "dexamplecdn002.cloudfront.net"


class _NotFound(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "404"}}


class _Denied(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "AccessDenied"}}


class _FakeS3:
    """head_object returns for keys in ``present``; 404s for keys in ``absent``; raises
    AccessDenied for anything else (to exercise the 'unknown' path)."""
    def __init__(self, present=(), absent=()):
        self.present = set(present)
        self.absent = set(absent)
        self.heads = []

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if Key in self.present:
            return {"ContentLength": 10}
        if Key in self.absent:
            raise _NotFound()
        raise _Denied()


def _wire(monkeypatch, s3):
    monkeypatch.setattr(pc, "_s3", s3)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", _BUCKET)
    monkeypatch.setattr(pc, "ARTIFACTS_DOMAIN", _DOMAIN, raising=False)


def test_no_claimed_uri_is_not_a_failure(monkeypatch):
    _wire(monkeypatch, _FakeS3())
    assert pc._missing_declared_deliverable({"status": "done", "note": "all good"}) is None


def test_claimed_s3_deliverable_missing_is_flagged(monkeypatch):
    key = "artifacts/sess/output/meridian_shot06_oil.png"
    _wire(monkeypatch, _FakeS3(absent=[key]))
    reason = pc._missing_declared_deliverable(
        {"uri": f"s3://{_BUCKET}/{key}", "cloudfront_url": f"https://{_DOMAIN}/{key}"})
    assert reason and "not found" in reason
    assert "meridian_shot06_oil.png" in reason


def test_existing_deliverable_passes(monkeypatch):
    key = "artifacts/sess/output/meridian_shot02_oil.png"
    _wire(monkeypatch, _FakeS3(present=[key]))
    assert pc._missing_declared_deliverable({"uri": f"s3://{_BUCKET}/{key}"}) is None


def test_one_existing_among_claims_is_not_a_false_success(monkeypatch):
    good = "artifacts/sess/output/a.png"
    bad = "artifacts/sess/output/b.png"
    _wire(monkeypatch, _FakeS3(present=[good], absent=[bad]))
    assert pc._missing_declared_deliverable(
        {"images": [f"https://{_DOMAIN}/{good}", f"https://{_DOMAIN}/{bad}"]}) is None


def test_unverifiable_head_error_never_fails(monkeypatch):
    # AccessDenied / transient -> unknown -> do NOT fail a possibly-real success.
    key = "artifacts/sess/output/c.png"
    _wire(monkeypatch, _FakeS3())  # neither present nor absent -> raises AccessDenied
    assert pc._missing_declared_deliverable({"uri": f"s3://{_BUCKET}/{key}"}) is None


def test_foreign_bucket_or_cdn_is_skipped(monkeypatch):
    s3 = _FakeS3()
    _wire(monkeypatch, s3)
    # fal.media / a foreign bucket: we can't head it, so it's neither claimed nor verified here.
    assert pc._missing_declared_deliverable(
        {"video": {"url": "https://v3b.fal.media/files/b/abc/out.mp4"},
         "src": "s3://some-other-bucket/x.png"}) is None
    assert s3.heads == []  # nothing in our bucket was even probed


def test_https_our_domain_missing_is_flagged(monkeypatch):
    key = "artifacts/sess/output/only.png"
    _wire(monkeypatch, _FakeS3(absent=[key]))
    reason = pc._missing_declared_deliverable({"cloudfront_url": f"https://{_DOMAIN}/{key}?v=1"})
    assert reason and "only.png" in reason
