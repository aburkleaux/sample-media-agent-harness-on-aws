"""Rendered artifacts must use the permanent CloudFront URL, not an expiring presigned one.

Root cause of the broken game render (prod session 46446dba): a document-viewer
`document.previewUrl` was a self-presigned S3 URL signed with the render role's TEMPORARY
STS creds. The session token expired (S3 `ExpiredToken`) long before the 24h `ExpiresIn`,
so the iframe showed the S3 error XML and the refresh button (which re-fetches the same
stored manifest) could not recover it. `_public_or_presigned_url` fixes this by returning
the ArtifactsDomain CloudFront URL for skills-bucket `artifacts/` objects, presigning only
as the no-CloudFront / other-bucket fallback.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render_agent as ra  # noqa: E402


class _FakeS3:
    def __init__(self):
        self.presigned = []

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.presigned.append((Params["Bucket"], Params["Key"], ExpiresIn))
        return (f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}"
                f"?AWSAccessKeyId=ASIA_TEST&Signature=sig&x-amz-security-token=tok&Expires=1")


def _cfg(monkeypatch, *, domain="cdn.example.net", bucket="skills-bkt", prefix="artifacts/"):
    monkeypatch.setattr(ra, "ARTIFACTS_DOMAIN", domain)
    monkeypatch.setattr(ra, "S3_BUCKET", bucket)
    monkeypatch.setattr(ra, "S3_PREFIX", prefix)


def test_skills_bucket_artifact_uses_cloudfront_not_presigned(monkeypatch):
    _cfg(monkeypatch)
    cli = _FakeS3()
    key = "artifacts/sess/output/space_invaders.html"
    url = ra._public_or_presigned_url(cli, "skills-bkt", key)
    assert url == "https://cdn.example.net/artifacts/sess/output/space_invaders.html"
    # Crucially, it must NOT presign (no STS token to expire).
    assert cli.presigned == []
    assert "X-Amz" not in url and "AWSAccessKeyId" not in url and "security-token" not in url


def test_non_artifacts_key_falls_back_to_presign(monkeypatch):
    _cfg(monkeypatch)
    cli = _FakeS3()
    url = ra._public_or_presigned_url(cli, "skills-bkt", "uploads/raw/x.bin")
    assert "cdn.example.net" not in url
    assert cli.presigned == [("skills-bkt", "uploads/raw/x.bin", 86400)]


def test_other_bucket_falls_back_to_presign(monkeypatch):
    # An M2C proxy-bucket object is NOT fronted by ArtifactsDomain -> must presign.
    _cfg(monkeypatch)
    cli = _FakeS3()
    url = ra._public_or_presigned_url(cli, "m2c-proxy-bkt", "artifacts/sess/out.json")
    assert "cdn.example.net" not in url
    assert cli.presigned == [("m2c-proxy-bkt", "artifacts/sess/out.json", 86400)]


def test_no_cloudfront_domain_falls_back_to_presign(monkeypatch):
    # Local dev: no CloudFront -> presign is the only option.
    _cfg(monkeypatch, domain="")
    cli = _FakeS3()
    url = ra._public_or_presigned_url(cli, "skills-bkt", "artifacts/sess/output/g.html")
    assert cli.presigned == [("skills-bkt", "artifacts/sess/output/g.html", 86400)]
