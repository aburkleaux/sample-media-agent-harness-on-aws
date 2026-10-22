"""The `artifact` tool's `adopt` op — register a file code_interpreter already
wrote under artifacts/ by KEY (no base64), returning the permanent CloudFront URL.

This is the escape hatch for large files (video/PDF/HTML): base64-relaying them
through the model corrupts on transcription and blows the output-token cap, and
presigning is pointless because CloudFront serves the artifacts prefix
permanently. Adopt also backfills a wrong/absent content-type so the browser
renders rather than downloads.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


class _FakeS3:
    def __init__(self, head):
        self._head = head
        self.copied = None

    def head_object(self, Bucket, Key):
        if self._head is None:
            raise Exception("NoSuchKey")
        return self._head

    def copy_object(self, **kw):
        self.copied = kw


def _call(**kwargs):
    fn = getattr(main.artifact, "func", main.artifact)  # unwrap @tool if needed
    return json.loads(fn(**kwargs))


def test_adopt_returns_cloudfront_url_and_backfills_content_type(monkeypatch):
    fake = _FakeS3({"ContentType": "application/octet-stream"})
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")
    out = _call(operation="adopt", key="snake-game.html")
    assert out["status"] == "adopted"
    assert out["url"] == "https://cdn.example.net/artifacts/snake-game.html"
    assert out["content_type"] == "text/html"
    # octet-stream was corrected in place so the browser renders the HTML
    assert fake.copied is not None and fake.copied["ContentType"] == "text/html"


def test_adopt_no_presign_for_video_and_no_bytes_needed(monkeypatch):
    fake = _FakeS3({"ContentType": "video/mp4"})
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")
    out = _call(operation="adopt", key="nba_proxy_30s.mp4")
    assert out["url"] == "https://cdn.example.net/artifacts/nba_proxy_30s.mp4"
    assert "AWSAccessKeyId" not in out["url"] and "Signature" not in out["url"]
    # content-type already correct → no rewrite
    assert fake.copied is None


def test_adopt_missing_object_returns_actionable_error(monkeypatch):
    monkeypatch.setattr(main, "s3_client", _FakeS3(None))
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")
    out = _call(operation="adopt", key="missing.pdf")
    assert "error" in out and "artifacts/missing.pdf" in out["error"]


# --- session-scoped output location (artifacts/<session_id>/output/) ---------


class _KeyedS3:
    """Fake S3 tracking which keys exist so we can assert session-scoped keys
    and flat->session relocation."""

    def __init__(self, keys):
        self.keys = dict(keys)  # key -> {"ContentType": ...}
        self.copies = []
        self.put = None

    def head_object(self, Bucket, Key):
        if Key not in self.keys:
            raise Exception("NoSuchKey")
        return self.keys[Key]

    def copy_object(self, **kw):
        self.copies.append(kw)
        self.keys[kw["Key"]] = {"ContentType": kw.get("ContentType")}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.keys[Key] = {"ContentType": ContentType}
        self.put = {"Key": Key, "ContentType": ContentType}


def _with_session(sid):
    return main.jobs_context.set_request_context(actor_id="actor-1", session_id=sid)


def test_adopt_scopes_url_to_session_output_prefix(monkeypatch):
    sid = "sess-abc"
    fk = f"artifacts/{sid}/output/report.pdf"
    fake = _KeyedS3({fk: {"ContentType": "application/pdf"}})
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")
    tokens = _with_session(sid)
    try:
        out = _call(operation="adopt", key="report.pdf")
    finally:
        main.jobs_context.reset_request_context(tokens)
    assert out["status"] == "adopted"
    assert out["url"] == f"https://cdn.example.net/artifacts/{sid}/output/report.pdf"
    assert out["key"] == fk
    # content-type already correct at the session key → no relocation/rewrite
    assert fake.copies == []


def test_adopt_relocates_flat_file_into_session_output(monkeypatch):
    sid = "sess-xyz"
    flat = "artifacts/chart.png"
    fake = _KeyedS3({flat: {"ContentType": "image/png"}})
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")
    tokens = _with_session(sid)
    try:
        out = _call(operation="adopt", key="chart.png")
    finally:
        main.jobs_context.reset_request_context(tokens)
    dest = f"artifacts/{sid}/output/chart.png"
    assert out["url"] == f"https://cdn.example.net/{dest}"
    assert out["key"] == dest
    # relocated from the flat key into the canonical session output key
    assert len(fake.copies) == 1
    assert fake.copies[0]["Key"] == dest
    assert fake.copies[0]["CopySource"] == {"Bucket": "bkt", "Key": flat}


def test_upload_scopes_key_to_session_output_prefix(monkeypatch):
    import base64
    sid = "sess-up"
    fake = _KeyedS3({})
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")
    tokens = _with_session(sid)
    try:
        out = _call(operation="upload", key="note.txt",
                    data_b64=base64.b64encode(b"hi").decode(), content_type="text/plain")
    finally:
        main.jobs_context.reset_request_context(tokens)
    assert out["status"] == "uploaded"
    assert out["key"] == f"artifacts/{sid}/output/note.txt"
    assert out["url"] == f"https://cdn.example.net/artifacts/{sid}/output/note.txt"


def test_no_session_falls_back_to_flat_prefix(monkeypatch):
    fake = _KeyedS3({"artifacts/x.png": {"ContentType": "image/png"}})
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(main, "S3_BUCKET", "bkt")

    # No session bound: current_session_id() RAISES when unset. Stub it so the test is
    # deterministic regardless of a session another test in the process may have left bound.
    def _raise():
        raise RuntimeError("no session bound")
    monkeypatch.setattr(main.jobs_context, "current_session_id", _raise)

    out = _call(operation="adopt", key="x.png")
    assert out["url"] == "https://cdn.example.net/artifacts/x.png"
    assert fake.copies == []


def test_adopt_never_doubles_the_session_output_prefix(monkeypatch):
    # Regression (session 4f1fd39c): a key carrying the session segment WITHOUT the leading
    # "artifacts/" was re-prefixed into artifacts/<sid>/output/<sid>/output/report.pdf, which 404s
    # and made the renderer fall back to the harness SPA. The strip must collapse every leading
    # prefix form to the single correct key.
    sid = "sess-dbl"
    correct = f"artifacts/{sid}/output/report.pdf"
    for passed in (f"{sid}/output/report.pdf",                       # session segment, no artifacts/
                   f"artifacts/{sid}/output/{sid}/output/report.pdf",  # already doubled
                   f"artifacts/{sid}/output/report.pdf",             # full prefix
                   "report.pdf"):                                    # bare filename
        fake = _KeyedS3({correct: {"ContentType": "application/pdf"}})
        monkeypatch.setattr(main, "s3_client", fake)
        monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
        monkeypatch.setattr(main, "S3_BUCKET", "bkt")
        tokens = _with_session(sid)
        try:
            out = _call(operation="adopt", key=passed)
        finally:
            main.jobs_context.reset_request_context(tokens)
        assert out.get("key") == correct, f"{passed!r} -> {out.get('key')!r}"
        assert out.get("url") == f"https://cdn.example.net/{correct}"
