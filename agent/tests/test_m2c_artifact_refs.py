"""M2C_REFERENCES: `artifacts/m2c/<bucket>/<key>` (asset-lineage D4/R4, Phase 2).

An M2C output is referenced WHERE M2C WROTE IT and never copied. The reference is encoded as an
artifact key so every existing shape check accepts it unchanged (the proxy's
`key must be under artifacts/` gate, `isArtifactKey`, the SPA resolver, `own_artifact_key`) — but
it resolves to the bucket IT names, not to the skills bucket. These tests pin the round trip the
rest of the system depends on:

    presigned M2C URL  --stamp-->  artifacts/m2c/<bucket>/<key>  --resolve-->  s3://<bucket>/<key>

Deliberately NOT the CloudFront form: no object exists at that CDN path, and a URL that looks
fetchable and 403s is the broken-link failure this reference removes.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from media import artifact_fetch as af  # noqa: E402

M2C_BUCKET = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
M2C_KEY = "micro-workflow/media-processing/9ede25bc/proxy/transcoded.mp4"
REF = f"artifacts/m2c/{M2C_BUCKET}/{M2C_KEY}"
SKILLS = "mediaworks-harness-skills-store-100000000002-us-west-2"
DOMAIN = "dexamplecdn002.cloudfront.net"


# --- encoding -------------------------------------------------------------------------------

def test_reference_is_built_from_a_bucket_and_key():
    assert af.m2c_artifact_ref(M2C_BUCKET, M2C_KEY) == REF
    # a leading slash and surrounding space are normalised away
    assert af.m2c_artifact_ref(M2C_BUCKET.upper(), f"  /{M2C_KEY}  ") == REF


def test_reference_refuses_anything_that_is_not_one_object():
    assert af.m2c_artifact_ref("b", M2C_KEY) is None                 # too short for a bucket
    assert af.m2c_artifact_ref("Not_A_Bucket", M2C_KEY) is None      # illegal bucket characters
    assert af.m2c_artifact_ref(M2C_BUCKET, "") is None
    assert af.m2c_artifact_ref(M2C_BUCKET, "a/prefix/") is None      # a prefix, not an object
    assert af.m2c_artifact_ref(M2C_BUCKET, "../etc/passwd") is None
    assert af.m2c_artifact_ref(M2C_BUCKET, "a\\b") is None
    assert af.m2c_artifact_ref(None, M2C_KEY) is None


def test_reference_is_parsed_back_in_every_identifier_form(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    for form in (
        REF,                                         # bare key
        f"/api/artifact?key={REF}",                  # proxy read-through
        f"https://{DOMAIN}/{REF}",                   # CDN form (accepted on the way in)
    ):
        assert af.parse_m2c_artifact_ref(form) == (M2C_BUCKET, M2C_KEY), form
        assert af.is_m2c_artifact_ref(form) is True


def test_an_ordinary_artifact_key_is_not_a_reference():
    assert af.parse_m2c_artifact_ref("artifacts/sess/output/clip.mp4") is None
    assert af.parse_m2c_artifact_ref("artifacts/m2c/") is None
    assert af.parse_m2c_artifact_ref(f"artifacts/m2c/{M2C_BUCKET}") is None  # no key
    assert af.is_m2c_artifact_ref("s3://bucket/key") is False


# --- resolution -----------------------------------------------------------------------------

def test_reference_is_an_own_artifact_key(monkeypatch):
    """It travels as an artifact key, so every existing shape check keeps working."""
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    assert af.own_artifact_key(REF) == REF
    assert af.own_artifact_key(f"/api/artifact?key={REF}") == REF
    assert af.is_own_artifact(REF) is True


def test_canonical_location_resolves_to_the_m2c_bucket_not_the_skills_bucket(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    expected = f"s3://{M2C_BUCKET}/{M2C_KEY}"
    assert af.canonical_location(REF) == expected
    assert af.canonical_location(f"/api/artifact?key={REF}") == expected
    assert af.canonical_location(f"https://{DOMAIN}/{REF}") == expected
    # the presigned original and the reference are ONE location (R1.2 / R7.1 folding)
    presigned = f"https://{M2C_BUCKET}.s3.amazonaws.com/{M2C_KEY}?X-Amz-Signature=z"
    assert af.canonical_location(presigned) == expected
    # an ordinary artifact key still resolves to the skills bucket
    assert af.canonical_location("artifacts/sess/output/clip.mp4") == \
        f"s3://{SKILLS}/artifacts/sess/output/clip.mp4"


def test_reads_and_presigns_target_the_m2c_bucket(monkeypatch):
    """`artifact_fetch` treats a reference as a first-class own-artifact identifier."""
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://signed"
    client.get_object.return_value = {"Body": MagicMock(read=lambda *a: b"BYTES", close=lambda: None)}
    client.head_object.return_value = {"ContentLength": 5}
    with patch.object(af, "_client", return_value=client):
        assert af.presign_artifact_url(REF) == "https://signed"
        assert af.read_artifact_bytes(REF) == b"BYTES"
        assert af.head_artifact(REF) == {"ContentLength": 5}
    for call in (client.generate_presigned_url, client.get_object, client.head_object):
        params = call.call_args.kwargs.get("Params") or call.call_args.kwargs
        assert params["Bucket"] == M2C_BUCKET, call
        assert params["Key"] == M2C_KEY, call


def test_an_artifacts_bucket_override_does_not_redirect_a_reference(monkeypatch):
    """The bytes are in the bucket the reference names; the artifacts-bucket override is for
    ordinary keys only."""
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://signed"
    with patch.object(af, "_client", return_value=client):
        af.presign_artifact_url(REF, bucket="some-other-bucket")
    assert client.generate_presigned_url.call_args.kwargs["Params"]["Bucket"] == M2C_BUCKET


# --- the stamp side: the render pass writes the reference ------------------------------------

def test_render_pass_stamps_the_reference_and_copies_nothing():
    """Round trip: a presigned M2C deliverable in a stored manifest becomes the reference, and
    the reference resolves back to the location it came from."""
    import render.artifacts as render_artifacts

    presigned = f"https://{M2C_BUCKET}.s3.amazonaws.com/{M2C_KEY}?X-Amz-Signature=z"
    manifest = {"id": "m-1", "tracks": [{"src": presigned}]}
    puts: list[str] = []
    written: dict = {}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(manifest).encode())}

    def _put(**kw):
        puts.append(kw["Key"])
        written["body"] = json.loads(kw["Body"].decode())
        return {}

    s3.put_object.side_effect = _put

    with (
        patch.object(render_artifacts, "_s3_client", return_value=s3),
        patch.object(render_artifacts, "_SKILLS_BUCKET", SKILLS),
        patch.object(render_artifacts, "_ARTIFACTS_DOMAIN", DOMAIN),
        patch.object(render_artifacts, "_ARTIFACTS_PREFIX", "artifacts/"),
    ):
        render_artifacts._stabilize_manifest_media_urls("artifacts/s/render/m-1.json", "s")

    assert puts == ["artifacts/s/render/m-1.json"], "no byte mirror may be written"
    stamped = written["body"]["tracks"][0]["src"]
    assert stamped == REF
    assert af.canonical_location(stamped) == f"s3://{M2C_BUCKET}/{M2C_KEY}"


def test_the_mirror_key_builder_is_gone():
    """Nothing may write a new `artifacts/durable/` copy: the 60-day lifecycle on those copies
    expired renders whose M2C originals never do. Existing keys are still READ."""
    import render.artifacts as render_artifacts

    assert not hasattr(render_artifacts, "_durable_key")
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "render", "artifacts.py")).read()
    body = src[src.index("def _stabilize_manifest_media_urls"):src.index("def _manifest_id_for")]
    code = body.split('"""')[2]  # drop the signature and the docstring that explains the history
    assert "urlopen" not in code, "the pass must not fetch deliverable bytes any more"
    assert "durable/" not in code, "the pass must not build a mirror key any more"
