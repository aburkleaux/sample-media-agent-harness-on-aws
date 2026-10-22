"""is_already_ours must scope "ours" to OUR domain/bucket only — not every *.amazonaws.com.

The M2C proxy bucket is also on *.amazonaws.com and its presigned URLs EXPIRE; the old
catch-all made the render mirror treat them as "ours" and skip the copy, so the inline
"play video" broke after ~1 hour. These pin the corrected classification.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from media import persist_media  # noqa: E402

CF = "dexamplecdn002.cloudfront.net"
SKILLS = "mediaworks-harness-skills-store-100000000002-us-west-2"


def test_m2c_proxy_presigned_is_external_and_gets_mirrored(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    u = ("https://m2cv6-0e0000000002-100000000002-us-west-2-proxy.s3.us-west-2.amazonaws.com/"
         "micro-workflow/media-processing/EXEC/proxy/transcoded.mp4?X-Amz-Signature=zzz")
    assert persist_media.media_type_for(u) == "video/mp4"        # classified as media
    assert persist_media.is_already_ours(u, CF) is False         # NOT ours -> will be mirrored


def test_our_cloudfront_is_ours(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    assert persist_media.is_already_ours(f"https://{CF}/artifacts/imports/x.mp4", CF) is True


def test_our_bucket_virtualhosted_is_ours(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    assert persist_media.is_already_ours(
        f"https://{SKILLS}.s3.us-west-2.amazonaws.com/artifacts/x.mp4", CF) is True


def test_our_bucket_pathstyle_is_ours(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    assert persist_media.is_already_ours(
        f"https://s3.us-west-2.amazonaws.com/{SKILLS}/artifacts/x.mp4", CF) is True


def test_fal_media_is_external(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", SKILLS)
    assert persist_media.is_already_ours("https://v3b.fal.media/files/b/x/y.mp4", CF) is False


def test_no_bucket_env_only_domain_is_ours(monkeypatch):
    monkeypatch.delenv("SKILLS_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    assert persist_media.is_already_ours(f"https://{CF}/a.mp4", CF) is True
    # without a known bucket, a foreign S3 host is NOT assumed ours (so it gets mirrored)
    assert persist_media.is_already_ours("https://proxy.s3.amazonaws.com/x.mp4", CF) is False
