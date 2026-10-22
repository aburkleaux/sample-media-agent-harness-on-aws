"""Every field a template plays must hold a STABLE reference, not just `tracks[].src`.

Session 59c46b2c: the face-recognition tab reported "Source video could not be loaded"
while a stable form of the same video sat one field away. The manifest held

    tracks[0].src  https://<cf>/artifacts/durable/d41a1956….mp4      (rewritten)
    media.src      https://m2cv6-…-proxy.s3.amazonaws.com/…?AWSAccessKeyId=…  (expires)
    sourceUrl      same presigned URL

because the pass rewrote tracks, images and the top-level `src`/`poster`, and
`VideoAnnotations` reads `media.src` first. A presigned URL minted from an assumed role dies
with its session token, so the tab worked and then stopped.

The interesting property is not "media.src gets rewritten" but that the rewrite is whole: a
rewrite list is a per-field allowlist, and the failure mode of an allowlist is silence. So the
test walks the manifest and asserts NO credential-bearing URL survives anywhere a player could
read one.

Phase 2 changed WHAT the fields are rewritten to: an M2C_REFERENCE
(`artifacts/m2c/<bucket>/<key>`, presigned by the proxy at view time) instead of a byte copy
under `artifacts/durable/`, whose 60-day lifecycle expired while the M2C original never did.
"""

from __future__ import annotations

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

M2C_BUCKET = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
M2C_KEY = "micro-workflow/media-processing/9ede25bc/proxy/transcoded.mp4"
PRESIGNED = (
    f"https://{M2C_BUCKET}.s3.amazonaws.com/{M2C_KEY}"
    "?AWSAccessKeyId=ASIAEXAMPLEREDACTED&Signature=REDACTED%3D&x-amz-security-token=REDACTED"
)
M2C_REF = f"artifacts/m2c/{M2C_BUCKET}/{M2C_KEY}"
DOMAIN = "dexamplecdn002.cloudfront.net"
BUCKET = "mediaworks-harness-skills-store-100000000002-us-west-2"

_CREDENTIAL_QUERY = re.compile(
    r"[?&](X-Amz-Signature|X-Amz-Credential|AWSAccessKeyId|Signature)=", re.I
)


def _manifest() -> dict:
    """The shape `_build_video_annotations_manifest` emits, all three fields presigned."""
    return {
        "id": "m-1",
        "template": "video-annotations",
        "duration": 60.059,
        "tracks": [
            {"id": "source-video", "type": "video", "src": PRESIGNED, "label": "transcoded"}
        ],
        "media": {"src": PRESIGNED, "duration": 60.059},
        "sourceUrl": PRESIGNED,
        "entities": [],
        "annotations": [],
    }


def _run(manifest: dict):
    """Invoke the stable-reference pass over `manifest`, returning (written manifest, puts)."""
    import render.artifacts as render_artifacts

    written: dict = {}
    puts: list[str] = []
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(manifest).encode())}

    def _put_object(**kwargs):
        puts.append(kwargs["Key"])
        if kwargs["Key"].endswith(".json"):
            written["manifest"] = json.loads(kwargs["Body"].decode())
        return {}

    s3.put_object.side_effect = _put_object

    with (
        patch.object(render_artifacts, "_s3_client", return_value=s3),
        patch.object(render_artifacts, "_SKILLS_BUCKET", BUCKET),
        patch.object(render_artifacts, "_ARTIFACTS_DOMAIN", DOMAIN),
        patch.object(render_artifacts, "_ARTIFACTS_PREFIX", "artifacts/"),
    ):
        render_artifacts._stabilize_manifest_media_urls("artifacts/s/render/m-1.json", "s")

    return written.get("manifest"), puts


def _urls(node, path="") -> list[tuple[str, str]]:
    """Every string that looks like a URL, with the path it was found at."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out += _urls(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out += _urls(value, f"{path}[{index}]")
    elif isinstance(node, str) and node.startswith("http"):
        out.append((path, node))
    return out


def test_media_src_is_referenced():
    """The field `VideoAnnotations` reads FIRST."""
    result, _ = _run(_manifest())
    assert result is not None, "the pass should have written the manifest back"
    assert result["media"]["src"] == M2C_REF


def test_source_url_is_referenced():
    result, _ = _run(_manifest())
    assert result["sourceUrl"] == M2C_REF


def test_no_expiring_url_survives_anywhere():
    # The real invariant. A rewrite list is an allowlist, and an allowlist that misses a
    # field fails silently — which is exactly what happened to `media.src`.
    result, _ = _run(_manifest())
    leftovers = [(p, u) for p, u in _urls(result) if _CREDENTIAL_QUERY.search(u)]
    assert leftovers == [], f"presigned URLs left in the manifest: {leftovers}"


def test_no_bytes_are_copied_any_more():
    """The whole point of Phase 2: the M2C original is referenced, never mirrored.

    The mirror wrote `artifacts/durable/<digest>/…`, which a 60-day lifecycle rule then deleted
    while the M2C original it copied never expires.
    """
    result, puts = _run(_manifest())
    assert puts == ["artifacts/s/render/m-1.json"], \
        f"only the manifest may be written, got {puts}"
    assert result["tracks"][0]["src"] == M2C_REF


def test_three_fields_naming_one_object_share_one_reference():
    """`_stable` caches on the URL sans query, so tracks/media/sourceUrl converge."""
    result, _ = _run(_manifest())
    assert result["tracks"][0]["src"] == result["media"]["src"] == result["sourceUrl"] == M2C_REF


def test_existing_durable_url_still_resolves_and_is_left_alone():
    """Old manifests must keep working: a `durable/` CloudFront URL is already stable."""
    durable = f"https://{DOMAIN}/artifacts/durable/abc.mp4"
    manifest = _manifest()
    manifest["tracks"][0]["src"] = durable
    manifest["media"]["src"] = durable
    manifest["sourceUrl"] = durable

    written, puts = _run(manifest)
    assert written is None, "no write-back when there was nothing to rewrite"
    assert puts == []
