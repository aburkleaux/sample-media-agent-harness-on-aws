"""Long presigned URLs never reach the model; short identifiers are presigned at call time (session e2fb2692).

The planner was handed a ~1.5 KB presigned S3 URL for an attached project image, abbreviated it to
`https://…amazonaws.com/.../xi.webp?...` in every node prompt, and all four fal calls failed. Three layers now:
the incoming prompt carries the short identifier, the outbound tool input is presigned freshly (project assets
only for the turn's TRUSTED project), and an elided URL is refused before anything is sent.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs.strands_tool_wrap import _elided_url_refusal  # noqa: E402
from media import artifact_fetch as af  # noqa: E402

BUCKET = "mediaworks-harness-skills-store-100000000002-us-west-2"
DOMAIN = "dexamplecdn002.cloudfront.net"
PID = "paa97084ce1224386a60336bc3f52dddb"
OTHER = "p" + "b" * 32
ASSET_KEY = f"projects/{PID}/image:2f1c9a7e-1111-4111-8111-111111111111/xi.webp"
SIG = ("X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIA%2F20261011%2Fus-west-2%2Fs3%2Faws4_request"
       "&X-Amz-Date=20261011T003800Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
       "&X-Amz-Security-Token=" + "IQoJb3JpZ2luX2VjE" * 60 + "&X-Amz-Signature=" + "a" * 64)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "1")


class _FakeS3:
    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://{Params['Bucket']}.s3.us-west-2.amazonaws.com/{Params['Key']}?{op}&exp={ExpiresIn}&X-Amz-Signature=z"


@pytest.fixture
def fake_s3(monkeypatch):
    monkeypatch.setattr(af, "_client", lambda: _FakeS3())


# ------------------------------------------------------------------------------------------- shortener
def test_project_asset_presigned_url_becomes_the_short_identifier():
    long = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/{ASSET_KEY.replace(':', '%3A')}?{SIG}"
    prompt = f"[File attached: xi.webp\nPublic HTTPS URL (use this as the image_url...): {long}]\n\nHere is Xi"
    out = af.shorten_own_presigned_urls(prompt)
    assert f"https://{DOMAIN}/{ASSET_KEY}" in out
    assert "X-Amz-" not in out and len(out) < len(prompt) - 1000
    assert out.endswith("Here is Xi")


def test_artifact_presigned_url_is_shortened_too():
    key = "artifacts/e2fb2692-9cfd-4076-a272-3daa309a7e55/input/trump.webp"
    out = af.shorten_own_presigned_urls(f"see https://{BUCKET}.s3.amazonaws.com/{key}?{SIG} now")
    assert out == f"see https://{DOMAIN}/{key} now"


def test_other_buckets_and_unsigned_urls_are_left_alone():
    foreign = f"https://someone-else.s3.amazonaws.com/{ASSET_KEY}?{SIG}"
    plain = f"https://{DOMAIN}/{ASSET_KEY}"
    for text in (foreign, plain, "no urls here", f"https://fal.media/files/x.png?{SIG}"):
        assert af.shorten_own_presigned_urls(text) == text


# -------------------------------------------------------------------------------- trusted-project assets
def test_trusted_project_asset_key_requires_the_same_project():
    url = f"https://{DOMAIN}/{ASSET_KEY}"
    assert af.trusted_project_asset_key(url, PID) == ASSET_KEY
    assert af.trusted_project_asset_key(url, OTHER) is None
    assert af.trusted_project_asset_key(url, None) is None
    assert af.trusted_project_asset_key(f"https://{DOMAIN}/projects/{PID}/../x/y.webp", PID) is None
    # still NOT an artifact key for every other caller
    assert af.own_artifact_key(url) is None


def test_tool_input_presigns_a_trusted_project_asset_and_only_that(fake_s3):
    url = f"https://{DOMAIN}/{ASSET_KEY}"
    tool_in = {"endpoint_id": "fal-ai/flux-pro/kontext", "input": {"image_url": url, "prompt": "anime"}}
    signed, changed = af.presign_artifact_urls_in(tool_in, 21600, PID)
    assert changed and signed["input"]["image_url"].startswith(f"https://{BUCKET}.s3.")
    assert ASSET_KEY in signed["input"]["image_url"] and "exp=21600" in signed["input"]["image_url"]
    same, changed = af.presign_artifact_urls_in(tool_in, 21600, OTHER)
    assert not changed and same is tool_in
    same, changed = af.presign_artifact_urls_in(tool_in, 21600, None)
    assert not changed


def test_artifact_urls_still_presign_without_a_trusted_project(fake_s3):
    url = f"https://{DOMAIN}/artifacts/e2fb2692-9cfd-4076-a272-3daa309a7e55/input/trump.webp"
    signed, changed = af.presign_artifact_urls_in({"image_urls": [url]}, 21600)
    assert changed and "X-Amz-Signature" in signed["image_urls"][0]


def test_http_request_presign_covers_trusted_assets(fake_s3, monkeypatch):
    import tool_input_normalize as tin
    from jobs import context as jc

    monkeypatch.setattr(jc, "trusted_project_id", lambda: PID)
    tu = {"toolUseId": "t", "name": "http_request", "input": {"method": "HEAD", "url": f"https://{DOMAIN}/{ASSET_KEY}"}}
    out = tin.presign_artifact_url_in_http_request(tu)
    assert "head_object" in out["input"]["url"]
    monkeypatch.setattr(jc, "trusted_project_id", lambda: OTHER)
    assert tin.presign_artifact_url_in_http_request(tu) is tu


# ---------------------------------------------------------------------------------------- elided guard
@pytest.mark.parametrize("bad", [
    "https://mediaworks-harness-skills-store-100000000002-us-west-2.s3.us-west-2.amazonaws.com/.../xi.webp?...",
    "https://dexamplecdn002.cloudfront.net/projects/…/xi.webp",
])
def test_elided_url_is_refused_before_the_call(bad):
    res = _elided_url_refusal({"toolUseId": "t1", "input": {"input": {"image_urls": [bad]}}})
    assert res is not None and res["status"] == "error" and res["toolUseId"] == "t1"
    assert "abbreviated URL" in res["content"][0]["text"]


def test_complete_urls_and_prose_ellipses_pass():
    ok = {"input": {"image_url": f"https://{DOMAIN}/{ASSET_KEY}",
                    "prompt": "A tense pause... then laughter. Keep the composition."}}
    assert _elided_url_refusal({"toolUseId": "t", **ok}) is None


def test_code_interpreter_text_presigns_a_trusted_asset_only(fake_s3):
    code = f'import requests\nr = requests.get("https://{DOMAIN}/{ASSET_KEY}")\n'
    out, changed = af.presign_artifact_urls_in_text(code, 3600, trusted_project_id=PID)
    assert changed and f"https://{BUCKET}.s3." in out
    same, changed = af.presign_artifact_urls_in_text(code, 3600, trusted_project_id=OTHER)
    assert not changed and same == code


def test_legacy_sigv2_presigned_url_is_shortened_too():
    url = f"https://{BUCKET}.s3.amazonaws.com/{ASSET_KEY}?AWSAccessKeyId=AKIA&Signature=abc%3D&Expires=1791680000"
    assert af.shorten_own_presigned_urls(url) == f"https://{DOMAIN}/{ASSET_KEY}"
