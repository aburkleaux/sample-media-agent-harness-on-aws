"""Render source resolution (#4): the deterministic render resolves gateway OUTPUTS via the
download-url tool (proxy bucket only), and must SELF-PRESIGN a skills-bucket SOURCE the tool
can't own — otherwise the source stays s3://, the deterministic build fails, and the render
needlessly escalates to the slow LLM path and times out (trim/people-detection, 96198138/443eb166).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import render_agent as ra  # noqa: E402


class _StubS3:
    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://presigned.example/{Params['Bucket']}/{Params['Key']}?sig=1"


def _run(urls):
    return asyncio.run(ra._resolve_s3_urls_via_gateway(urls, budget_s=1.0))


def test_skills_bucket_source_is_self_presigned_when_gateway_cannot(monkeypatch):
    ra._resolved_url_cache.clear()
    monkeypatch.setattr(ra, "_s3", _StubS3())

    async def _noop_gw():
        return None
    monkeypatch.setattr(ra, "_ensure_gateways_loaded", _noop_gw)
    # Gateway tool exists but cannot resolve the skills-bucket ref (returns None), like the real
    # "bucket must be m2cv6-...-proxy" rejection.
    monkeypatch.setattr(ra, "_download_url_tools", lambda: [("gw", object(), "media-outputs___get_download_url")])
    monkeypatch.setattr(ra, "_tool_arg_name", lambda tool: "s3uri")
    monkeypatch.setattr(ra, "_resolve_one", lambda gw, tool, arg, x: None)

    src = "s3://mediaworks-harness-skills-store-100000000002-us-west-2/artifacts/1787-Meridian.mp4"
    out = _run([src])
    assert out[0].startswith("https://presigned.example/"), out   # resolved, not left as s3://
    assert "s3://" not in out[0]


def test_non_s3_urls_pass_through(monkeypatch):
    ra._resolved_url_cache.clear()
    out = _run(["https://cdn/x.mp4"])
    assert out == ["https://cdn/x.mp4"]
