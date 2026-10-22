"""End-to-end tests for auto-importing plan deliverables into our bucket.

Covers the chainer's REAL `_import_artifact` (server-side copy + CloudFront url), the already-ours
short-circuit, failure/no-domain fallbacks, and the full `build_aggregate` path wired with the real
callable — not just the resolver with a fake. This is the "does it actually work" check: the
resolver rewrite is only useful if the chainer copy it calls behaves correctly.

Importing `handler` requires its env + builds boto3 clients at import, so required env is stubbed
before import; the bucket/domain module globals are monkeypatched per-test so the suite is robust
to whatever another test module set at import time.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402
import result_resolution as rr  # noqa: E402


@pytest.fixture(autouse=True)
def _bucket_and_domain(monkeypatch):
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "harness-bucket")
    monkeypatch.setattr(handler, "ARTIFACTS_DOMAIN", "d.cloudfront.net")


class _RecordingS3:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def copy_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("AccessDenied")


def test_import_artifact_copies_from_proxy(monkeypatch):
    s3 = _RecordingS3()
    monkeypatch.setattr(handler, "_s3", s3)
    url = handler._make_import_artifact("sess-1")(
        "m2cv6-proxy", "micro-workflow/x/facemetadata.json", "p1/n1/facemetadata.json")
    assert url == "https://d.cloudfront.net/artifacts/sess-1/output/p1/n1/facemetadata.json"
    assert len(s3.calls) == 1
    c = s3.calls[0]
    assert c["Bucket"] == "harness-bucket"
    assert c["Key"] == "artifacts/sess-1/output/p1/n1/facemetadata.json"
    assert c["CopySource"] == {"Bucket": "m2cv6-proxy", "Key": "micro-workflow/x/facemetadata.json"}
    assert c["ContentType"] == "application/json"  # content-type backfilled so CloudFront serves it right


def test_import_artifact_no_session_falls_back_to_imports_prefix(monkeypatch):
    s3 = _RecordingS3()
    monkeypatch.setattr(handler, "_s3", s3)
    url = handler._make_import_artifact("")(
        "m2cv6-proxy", "w/x.json", "p1/n1/x.json")
    # No session bound → legacy imports prefix (backward compatible).
    assert url == "https://d.cloudfront.net/artifacts/imports/p1/n1/x.json"
    assert s3.calls[0]["Key"] == "artifacts/imports/p1/n1/x.json"


def test_import_artifact_already_ours_does_not_copy(monkeypatch):
    s3 = _RecordingS3()
    monkeypatch.setattr(handler, "_s3", s3)
    url = handler._make_import_artifact("sess-1")("harness-bucket", "artifacts/x/clip.wav", "p1/n1/clip.wav")
    # No copy — just a browser-fetchable url for the existing key.
    assert url == "https://d.cloudfront.net/artifacts/x/clip.wav"
    assert s3.calls == []


def test_import_artifact_copy_failure_returns_none(monkeypatch):
    monkeypatch.setattr(handler, "_s3", _RecordingS3(fail=True))
    assert handler._make_import_artifact("s")("proxy", "k/x.json", "p/n/x.json") is None


def test_import_artifact_without_domain_returns_none(monkeypatch):
    monkeypatch.setattr(handler, "ARTIFACTS_DOMAIN", "")
    monkeypatch.setattr(handler, "_s3", _RecordingS3())
    assert handler._make_import_artifact("s")("proxy", "k/x.json", "p/n/x.json") is None


def test_end_to_end_aggregate_rewrites_deliverables_via_real_import(monkeypatch):
    s3 = _RecordingS3()
    monkeypatch.setattr(handler, "_s3", s3)
    nodes = {"t1": {"status": "completed", "artifact_urls": [
        "s3://m2cv6-proxy/w/facemetadata.json",
        "s3://m2cv6-proxy/w/transcoded.mp4",
    ]}}
    agg = rr.build_aggregate(plan_id="plan9", status="SUCCEEDED", nodes=nodes,
                             import_artifact=handler._make_import_artifact("sessX"))
    arts = agg["nodes"][0]["artifacts"]
    assert len(arts) == 2
    for a in arts:
        assert a["url"].startswith("https://d.cloudfront.net/artifacts/sessX/output/plan9/t1/")
        assert a["source_ref"].startswith("s3://m2cv6-proxy/")
    # Both deliverables (the analysis json + the media) were actually copied.
    assert len(s3.calls) == 2
    dests = sorted(c["Key"] for c in s3.calls)
    assert dests == [
        "artifacts/sessX/output/plan9/t1/facemetadata.json",
        "artifacts/sessX/output/plan9/t1/transcoded.mp4",
    ]
