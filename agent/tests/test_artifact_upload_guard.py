"""The artifact tool must not let a large file be base64-relayed through the model.

Relaying a sizeable file as `data_b64` makes the model emit every byte as tool-call output, which
exceeds the output-token budget and never completes — the "stopped at the output token limit" loop
seen building a Pac-Man game (session c8cf3d89). The tools now reject an oversized inline payload
with an actionable redirect to the code_interpreter -> S3 -> adopt path, and the task agent gained
the `adopt` operation it was missing.
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
import task_agent  # noqa: E402


def _fn(mod):
    a = mod.artifact
    return getattr(a, "_tool_func", a)


def _big(mod):
    return "A" * (mod._MAX_INLINE_UPLOAD_B64_CHARS + 100)


def test_main_upload_rejects_oversized_inline_with_adopt_redirect():
    out = json.loads(_fn(main)(operation="upload", key="pacman.html", data_b64=_big(main)))
    assert "INLINE_UPLOAD_TOO_LARGE" in out.get("error", "")
    assert "adopt" in out["error"].lower()
    # The redirect must name the concrete key so the agent can write it from code_interpreter.
    assert "pacman.html" in out["error"]


def test_task_agent_upload_rejects_oversized_inline():
    # The guard fires before the S3-config check, so it holds even with S3 unconfigured.
    out = json.loads(_fn(task_agent)(operation="upload", key="pacman.html", data_b64=_big(task_agent)))
    assert "INLINE_UPLOAD_TOO_LARGE" in out.get("error", "")
    assert "adopt" in out["error"].lower()


def test_task_agent_small_upload_still_works(monkeypatch):
    puts = {}

    class _S3:
        def put_object(self, **k):
            puts.update(k)

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    out = json.loads(_fn(task_agent)(operation="upload", key="a.txt",
                                     data_b64=base64.b64encode(b"hello").decode()))
    assert out["status"] == "uploaded"
    assert puts.get("Bucket") == "b"


def test_task_agent_adopt_registers_by_key(monkeypatch):
    class _S3:
        def head_object(self, Bucket, Key):
            return {"ContentType": "text/html"}

        def copy_object(self, **k):
            pass

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    out = json.loads(_fn(task_agent)(operation="adopt", key="pacman.html"))
    assert out["status"] == "adopted"
    assert out["url"].endswith("pacman.html")


def test_task_agent_adopt_missing_object_is_actionable(monkeypatch):
    class _S3:
        def head_object(self, Bucket, Key):
            raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    out = json.loads(_fn(task_agent)(operation="adopt", key="missing.html"))
    assert "adopt" in out.get("error", "").lower()
    assert "code_interpreter" in out["error"]


def test_main_import_copies_from_proxy_to_our_bucket(monkeypatch):
    # M2C result files live in the proxy bucket; import server-side copies them into our bucket so
    # they're downloadable via CloudFront (no bytes through the model).
    copied = {}

    class _S3:
        def copy_object(self, **k):
            copied.update(k)

    monkeypatch.setattr(main, "s3_client", _S3())
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    out = json.loads(_fn(main)(operation="import",
                               source_uri="s3://m2cv6-proxy/micro-workflow/x/facemetadata.json"))
    assert out["status"] == "imported"
    assert out["url"].endswith("facemetadata.json")
    assert copied["CopySource"] == {"Bucket": "m2cv6-proxy", "Key": "micro-workflow/x/facemetadata.json"}


def test_main_import_rejects_a_non_s3_non_https_uri():
    # Plain http (not https), ftp, gs, or a bare name are still rejected; only s3:// and https:// work.
    out = json.loads(_fn(main)(operation="import", source_uri="http://example.com/x.json"))
    assert "import requires" in out.get("error", "")
    assert "https://" in out["error"]


def test_main_import_fetches_https_and_stores(monkeypatch):
    # A fal generation output lives on an ephemeral https CDN; import fetches and stores it durably.
    puts = {}

    class _S3:
        def put_object(self, **k):
            puts.update(k)

    monkeypatch.setattr(main, "s3_client", _S3())
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    import artifact_import
    monkeypatch.setattr(artifact_import, "fetch_https_bytes",
                        lambda url, *a, **k: (b"\x89PNG...", "image/png"))
    out = json.loads(_fn(main)(operation="import", key="meridian_shot06_oil.png",
                               source_uri="https://v3b.fal.media/files/b/abc/shot06.png"))
    assert out["status"] == "imported"
    assert out["url"].endswith("meridian_shot06_oil.png")
    assert puts["Body"] == b"\x89PNG..."
    assert puts["ContentType"] == "image/png"


def test_task_agent_import_fetches_https_and_stores(monkeypatch):
    puts = {}

    class _S3:
        def put_object(self, **k):
            puts.update(k)

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    import artifact_import
    monkeypatch.setattr(artifact_import, "fetch_https_bytes",
                        lambda url, *a, **k: (b"vid-bytes", "video/mp4"))
    out = json.loads(_fn(task_agent)(operation="import", key="seg01.mp4",
                                     source_uri="https://v3b.fal.media/files/b/abc/out.mp4"))
    assert out["status"] == "imported"
    assert out["url"].endswith("seg01.mp4")
    assert puts["Body"] == b"vid-bytes"
    assert puts["ContentType"] == "video/mp4"


def test_task_agent_import_https_fetch_error_is_actionable(monkeypatch):
    monkeypatch.setattr(task_agent, "_s3", type("S", (), {"put_object": lambda self, **k: None})())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    import artifact_import

    def _boom(url, *a, **k):
        raise ValueError("remote file exceeds the 104857600-byte import cap")

    monkeypatch.setattr(artifact_import, "fetch_https_bytes", _boom)
    out = json.loads(_fn(task_agent)(operation="import", key="huge.mp4",
                                     source_uri="https://cdn/huge.mp4"))
    assert "import failed fetching" in out.get("error", "")
    assert "import cap" in out["error"]


def test_task_agent_import_copies_by_key(monkeypatch):
    copied = {}

    class _S3:
        def copy_object(self, **k):
            copied.update(k)

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    out = json.loads(_fn(task_agent)(operation="import",
                                     source_uri="s3://m2cv6-proxy/w/6614_0.wav"))
    assert out["status"] == "imported"
    assert out["url"].endswith("6614_0.wav")
    assert copied["CopySource"] == {"Bucket": "m2cv6-proxy", "Key": "w/6614_0.wav"}


# ---------------------------------------------------------------------------
# download: verify the key exists before returning a URL, and route an s3:// key
# through import (server-side copy). Guards prod session 1792bb4d, where the
# download branch returned a dead CloudFront URL for stems that lived in the M2C
# proxy bucket, and those 403 URLs got baked into render manifests.
# ---------------------------------------------------------------------------

_SESSION_PREFIX = "artifacts/sess123/output/"


def test_main_download_missing_key_returns_import_hint(monkeypatch):
    class _S3:
        def head_object(self, Bucket, Key):
            raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(main, "s3_client", _S3())
    monkeypatch.setattr(main, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    monkeypatch.setattr(main, "_artifact_output_prefix", lambda: _SESSION_PREFIX)
    out = json.loads(_fn(main)(operation="download", key="stems/audio_speech.wav"))
    assert "url" not in out
    assert "import" in out.get("error", "").lower()


def test_task_agent_download_missing_key_returns_import_hint(monkeypatch):
    class _S3:
        def head_object(self, Bucket, Key):
            raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    monkeypatch.setattr(task_agent, "_artifact_output_prefix", lambda: _SESSION_PREFIX)
    out = json.loads(_fn(task_agent)(operation="download", key="stems/audio_speech.wav"))
    assert "url" not in out
    assert "import" in out.get("error", "").lower()


def test_main_download_s3_uri_routes_to_import(monkeypatch):
    copied = {}

    class _S3:
        def copy_object(self, **k):
            copied.update(k)

        def head_object(self, Bucket, Key):  # must never be reached on an s3:// key
            raise AssertionError("download of an s3:// URI must not HEAD the artifacts bucket")

    monkeypatch.setattr(main, "s3_client", _S3())
    monkeypatch.setattr(main, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    monkeypatch.setattr(main, "_artifact_output_prefix", lambda: _SESSION_PREFIX)
    out = json.loads(_fn(main)(operation="download", key="s3://proxy/stems/x.wav"))
    assert out["status"] == "imported"
    assert copied["CopySource"] == {"Bucket": "proxy", "Key": "stems/x.wav"}
    assert out["url"] == "https://d.cloudfront.net/artifacts/sess123/output/x.wav"


def test_task_agent_download_s3_uri_routes_to_import(monkeypatch):
    copied = {}

    class _S3:
        def copy_object(self, **k):
            copied.update(k)

        def head_object(self, Bucket, Key):  # must never be reached on an s3:// key
            raise AssertionError("download of an s3:// URI must not HEAD the artifacts bucket")

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    monkeypatch.setattr(task_agent, "_artifact_output_prefix", lambda: _SESSION_PREFIX)
    out = json.loads(_fn(task_agent)(operation="download", key="s3://proxy/stems/x.wav"))
    assert out["status"] == "imported"
    assert copied["CopySource"] == {"Bucket": "proxy", "Key": "stems/x.wav"}
    assert out["url"] == "https://d.cloudfront.net/artifacts/sess123/output/x.wav"


def test_main_download_existing_key_returns_url(monkeypatch):
    class _S3:
        def head_object(self, Bucket, Key):
            return {"ContentType": "audio/wav"}

    monkeypatch.setattr(main, "s3_client", _S3())
    monkeypatch.setattr(main, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    monkeypatch.setattr(main, "_artifact_output_prefix", lambda: _SESSION_PREFIX)
    out = json.loads(_fn(main)(operation="download", key="report.pdf"))
    assert "error" not in out
    assert out["url"] == "https://d.cloudfront.net/artifacts/sess123/output/report.pdf"


def test_task_agent_download_existing_key_returns_url(monkeypatch):
    class _S3:
        def head_object(self, Bucket, Key):
            return {"ContentType": "audio/wav"}

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "d.cloudfront.net", raising=False)
    monkeypatch.setattr(task_agent, "_artifact_output_prefix", lambda: _SESSION_PREFIX)
    out = json.loads(_fn(task_agent)(operation="download", key="report.pdf"))
    assert "error" not in out
    assert out["url"] == "https://d.cloudfront.net/artifacts/sess123/output/report.pdf"
