"""Session 764a71c0: one trimmed clip opened TWO render tabs.

The trim output had already been adopted into ``artifacts/<sid>/output/<execUUID>/<basename>``
when the render pass mirrored the M2C proxy copy of the SAME bytes into the flat
``artifacts/durable/<sha32>.mp4``. That name carries neither the exec UUID nor the basename, so
`_canonical_source_identity` keyed it to the bare url, the clip's analyses got a different
`sourceKey` than the clip, and `composeRenderTabs` (exact-sourceKey grouping) opened a new tab.

Fix A: reuse the adopted copy (HeadObject, no fetch, no PutObject).
Fix B: the stored reference keeps the identity.

Phase 2 retired the mirror: when there is no adopted copy the manifest now holds an
M2C_REFERENCE (``artifacts/m2c/<bucket>/<key>``), which keeps the WHOLE original key — exec UUID
and basename included — so Fix B holds by construction rather than by a key convention. Existing
``artifacts/durable/`` keys are still read (identity unchanged); nothing writes new ones.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render_agent as ra  # noqa: E402
from media.source_media import malformed_media_s3uris  # noqa: E402
from render.review import _norm_url  # noqa: E402

DOMAIN = "dexamplecdn002.cloudfront.net"
BUCKET = "mediaworks-harness-skills-store-100000000002-us-west-2"
M2C_BUCKET = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
SID = "764a71c0-2b7e-4a49-9d0e-1f0c2a7b9e11"
EXEC = "5b3a3e4a-5744-5c57-8147-6fa3daaf9ab3"
BASENAME = "meridian_trim_0_84040.mp4"
CLIP_SOURCE_KEY = f"{EXEC}/{BASENAME}"

M2C_KEY = f"micro-workflow/media-processing/{EXEC}/trim_clip/{BASENAME}"
M2C_PROXY = (
    f"https://{M2C_BUCKET}.s3.amazonaws.com/{M2C_KEY}"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=REDACTED&X-Amz-Signature=REDACTED"
)
M2C_REF = f"artifacts/m2c/{M2C_BUCKET}/{M2C_KEY}"
ADOPTED_KEY = f"artifacts/{SID}/output/{EXEC}/{BASENAME}"
ADOPTED_URL = f"https://{DOMAIN}/{ADOPTED_KEY}"


class _NotFound(Exception):
    """Stand-in for botocore's ClientError(404) from HeadObject."""


class FakeS3:
    """Only the calls the stable-reference pass makes; HeadObject is strict."""

    def __init__(self, manifest: dict, existing: set[str] | None = None):
        self.manifest = manifest
        self.existing = set(existing or ())
        self.heads: list[str] = []
        self.puts: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 casing
        return {"Body": MagicMock(read=lambda: json.dumps(self.manifest).encode())}

    def head_object(self, Bucket, Key):  # noqa: N803
        self.heads.append(Key)
        if Key not in self.existing:
            raise _NotFound(Key)
        return {"ETag": '"abc"', "ContentLength": 4}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.puts[Key] = Body
        return {}


def _manifest() -> dict:
    return {
        "id": "m-1",
        "template": "video-annotations",
        "tracks": [{"id": "source-video", "type": "video", "src": M2C_PROXY}],
        "media": {"src": M2C_PROXY},
        "sourceUrl": M2C_PROXY,
    }


def _run(s3: FakeS3, session_id: str | None = SID):
    import render.artifacts as render_artifacts

    with (
        patch.object(render_artifacts, "_s3_client", return_value=s3),
        patch.object(render_artifacts, "_SKILLS_BUCKET", BUCKET),
        patch.object(render_artifacts, "_ARTIFACTS_DOMAIN", DOMAIN),
        patch.object(render_artifacts, "_ARTIFACTS_PREFIX", "artifacts/"),
    ):
        render_artifacts._stabilize_manifest_media_urls(
            f"artifacts/{session_id or 's'}/render/m-1.json", session_id)
    written = s3.puts.get(f"artifacts/{session_id or 's'}/render/m-1.json")
    return json.loads(written) if written else None


# --- Fix A ---------------------------------------------------------------------------------------

def test_adopted_copy_is_reused():
    s3 = FakeS3(_manifest(), existing={ADOPTED_KEY})
    manifest = _run(s3)
    media_puts = [k for k in s3.puts if not k.endswith(".json")]
    assert media_puts == [], f"nothing may be copied, got {media_puts}"
    assert s3.heads == [ADOPTED_KEY], "one HeadObject, cached across the three fields"
    assert manifest["tracks"][0]["src"] == ADOPTED_URL
    assert manifest["media"]["src"] == ADOPTED_URL
    assert manifest["sourceUrl"] == ADOPTED_URL


def test_absent_adopted_copy_references_the_m2c_original_in_place():
    s3 = FakeS3(_manifest(), existing=set())
    manifest = _run(s3)
    media_puts = [k for k in s3.puts if not k.endswith(".json")]
    assert media_puts == [], "the M2C original is referenced, never copied (asset-lineage D4)"
    assert manifest["tracks"][0]["src"] == M2C_REF


def test_no_session_id_skips_the_head_and_references():
    s3 = FakeS3(_manifest(), existing={ADOPTED_KEY})
    manifest = _run(s3, session_id=None)
    assert s3.heads == []
    assert manifest["tracks"][0]["src"] == M2C_REF


# --- Fix B: identity equality --------------------------------------------------------------------

def _identity(url: str) -> str:
    return ra._canonical_source_identity(_norm_url(url))


def test_adopted_proxy_and_m2c_reference_share_one_identity():
    assert _identity(ADOPTED_URL) == CLIP_SOURCE_KEY
    assert _identity(M2C_PROXY) == CLIP_SOURCE_KEY
    assert _identity(M2C_REF) == CLIP_SOURCE_KEY


def test_derive_source_key_matches_across_all_three_forms():
    # the clip's own render (plain media) keys to its video
    clip = ra.derive_source_key([], [ADOPTED_URL], [ADOPTED_URL], has_analysis=False)
    # analyses of the clip key to their source_video, in whichever form they carried it
    via_proxy = ra.derive_source_key([M2C_PROXY], [], [], has_analysis=True)
    via_ref = ra.derive_source_key([M2C_REF], [], [], has_analysis=True)
    assert clip == via_proxy == via_ref == CLIP_SOURCE_KEY


def test_legacy_durable_forms_still_read_the_way_they_always_did():
    # new form with no exec uuid: the 32-hex digest must not become an identity
    url = f"https://{DOMAIN}/artifacts/durable/{'f' * 32}/clip.mp4"
    assert _identity(url) == _norm_url(url)
    # the old flat form carried no identity at all
    old = f"https://{DOMAIN}/artifacts/durable/d41a19560123456789abcdef01234567.mp4"
    assert _identity(old) == _norm_url(old)
    # and the exec-uuid-bearing form still collapses onto the clip
    keyed = f"https://{DOMAIN}/artifacts/durable/{'a' * 32}/{EXEC}/{BASENAME}"
    assert _identity(keyed) == CLIP_SOURCE_KEY


# --- false-positive malformed-id warning ---------------------------------------------------------

def test_durable_namespace_is_not_a_malformed_id():
    payload = {"source_video": f"s3://{BUCKET}/artifacts/durable/{'a' * 32}/{EXEC}/{BASENAME}"}
    assert malformed_media_s3uris(payload) == []
    old = {"source_video": f"s3://{BUCKET}/artifacts/durable/{'a' * 32}.mp4"}
    assert malformed_media_s3uris(old) == []


def test_mistyped_session_segment_still_flagged():
    payload = {"source_video": f"s3://{BUCKET}/artifacts/bcddb89ee4ce8/input/clip.mp4"}
    assert malformed_media_s3uris(payload) != []
