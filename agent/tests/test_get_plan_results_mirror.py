"""Plan-path fal.media durability: ``get_plan_results`` must mirror external media.

Regression context (prod session be3e9c00): 8 restyled frames were produced by a
plan (dispatch_tool -> fal nodes -> get_plan_results). The direct tool-result mirror
(``_ResilientMCPMixin._mirror_result_media``) does NOT fire on the plan-execution
path, and ``get_plan_results`` returned the node outputs verbatim — so the ephemeral
``fal.media`` URLs were written to chat/render history and 404'd once fal purged them.
``get_plan_results`` now mirrors external media at the results boundary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobs import context as jobs_context  # noqa: E402
from jobs import get_plan_results_tool as mod  # noqa: E402
from media import persist_media  # noqa: E402

FAL = "https://v3b.fal.media/files/b/0aa8b869/4ItF0JDzSIfdjYtZsnsBt_vMesSz9z.png"
CF_DOMAIN = "cdn.example.net"
CF = f"https://{CF_DOMAIN}/artifacts/external/abc123.png"


class _Rec:
    def __init__(self, session_id="sess-1", status="SUCCEEDED"):
        self.session_id = session_id
        self.status = status


class _Registry:
    def __init__(self, rec):
        self._rec = rec

    def get(self, job_id, *, actor_id):
        return self._rec


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    jobs_context.set_request_context(actor_id="actor-1", session_id="sess-caller")
    mod.set_registry(None)
    mod.set_manifest_location(bucket="", region="us-west-2")
    monkeypatch.setattr(mod, "_MIRROR_S3", object())  # non-None: never build a boto3 client
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bkt")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CF_DOMAIN)
    monkeypatch.setenv("ARTIFACTS_PREFIX", "artifacts/")
    yield
    mod.set_registry(None)
    mod.set_manifest_location(bucket="", region="us-west-2")


def _fake_swap_fal_to_cf(result, *, s3_client, bucket, prefix, artifacts_domain, limit):
    """Simulate persist_media: rewrite the fal URL to a CloudFront copy, no network."""
    blob = json.dumps(result)
    if "fal.media" not in blob:
        return result, []
    def rw(o):
        if isinstance(o, str):
            return o.replace(FAL, CF)
        if isinstance(o, dict):
            return {k: rw(v) for k, v in o.items()}
        if isinstance(o, list):
            return [rw(v) for v in o]
        return o
    return rw(result), [{"source_url": FAL, "url": CF}]


# --- helper unit tests ------------------------------------------------------

def test_helper_swaps_fal_to_cloudfront(monkeypatch):
    monkeypatch.setattr(persist_media, "swap_result_media", _fake_swap_fal_to_cf)
    out = mod._mirror_external_media_in({"nodes": [{"artifacts": [{"url": FAL}]}]})
    assert out["nodes"][0]["artifacts"][0]["url"] == CF
    assert "fal.media" not in json.dumps(out)


def test_helper_no_bucket_is_passthrough(monkeypatch):
    monkeypatch.delenv("SKILLS_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    called = {"n": 0}
    def _spy(*a, **k):
        called["n"] += 1
        return object(), []
    monkeypatch.setattr(persist_media, "swap_result_media", _spy)
    payload = {"nodes": [{"artifacts": [{"url": FAL}]}]}
    out = mod._mirror_external_media_in(payload)
    assert out is payload          # unchanged
    assert called["n"] == 0         # never attempted without a bucket


def test_helper_exception_is_passthrough(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(persist_media, "swap_result_media", _boom)
    payload = {"nodes": [{"artifacts": [{"url": FAL}]}]}
    out = mod._mirror_external_media_in(payload)
    assert out is payload          # best-effort: never fatal, returns original


def test_helper_already_ours_makes_no_copy(monkeypatch):
    # Real persist_media (no mock): an already-CloudFront URL is is_already_ours,
    # so nothing is copied and no network is touched.
    payload = {"nodes": [{"artifacts": [{"url": CF}]}]}
    out = mod._mirror_external_media_in(payload)
    assert out == payload


# --- integration through get_plan_results (resolved aggregate) --------------

def test_get_plan_results_mirrors_fal_in_resolved_aggregate(monkeypatch):
    monkeypatch.setattr(persist_media, "swap_result_media", _fake_swap_fal_to_cf)
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="skills-bkt", region="us-west-2")
    aggregate = {
        "schema": "v1",
        "plan_id": "plan-1",
        "status": "completed",
        "nodes": [
            {"id": "restyle_1", "kind": "media",
             "artifacts": [{"url": FAL, "media_kind": "image", "role": "output"}]},
        ],
    }
    monkeypatch.setattr(mod, "_read_plan_result", lambda session_id, plan_id: dict(aggregate))
    out = json.loads(mod.get_plan_results("plan-1"))
    # The surfaced artifact AND the flat downloads must be the durable CloudFront URL.
    assert out["nodes"][0]["artifacts"][0]["url"] == CF
    assert out["downloads"] == [{"url": CF, "media_kind": "image", "role": "output"}]
    assert "fal.media" not in json.dumps(out)
