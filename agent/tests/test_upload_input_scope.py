"""The `upload_file` skills action — the active input path (UI: uploadFile ->
skillsAction upload_file). Inputs land in the consistent, session-scoped INPUT
location artifacts/<session_id>/input/<file> in the skills bucket (CloudFront-served),
falling back to artifacts/uploads/input/ when no session is provided.

Also a regression pin: the handler previously referenced an undefined `prefix`
(NameError) and hard-coded application/pdf; it now derives the key + content-type.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


class _FakeS3:
    def __init__(self):
        self.put = None

    def put_object(self, **kw):
        self.put = kw


def _b64(data: bytes = b"hello") -> str:
    return base64.b64encode(data).decode()


def test_upload_file_session_scopes_input_key(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")
    out = main._handle_skills_action("upload_file", {
        "filename": "clip.png", "data": _b64(), "session_id": "sess-1"})
    assert out["status"] == "uploaded"
    assert out["full_key"] == "artifacts/sess-1/input/clip.png"
    assert out["url"] == "https://cdn.example.net/artifacts/sess-1/input/clip.png"
    # content-type auto-detected from the extension (was hard-coded application/pdf before)
    assert out["content_type"] == "image/png"
    assert fake.put["Bucket"] == "skills-bkt"
    assert fake.put["Key"] == "artifacts/sess-1/input/clip.png"
    assert fake.put["ContentType"] == "image/png"


def test_upload_file_no_session_still_keeps_the_input_segment(monkeypatch):
    """The session-less fallback must ALSO carry `/input/`.

    Role is now derived from the path — an `/input/` segment marks source media
    (`hooks.provider._has_input_path`, and the same rule in `ui/server/uploadKey.ts`). A
    fallback of bare `artifacts/uploads/` silently opts every session-less upload out of
    that, so an input would be classified as a deliverable and render its own tab. The
    fallback loses the SESSION, which is unavoidable; it must not also lose the ROLE.
    """
    fake = _FakeS3()
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", "cdn.example.net")

    # Simulate no request-context session bound. current_session_id() RAISES when unset
    # (see jobs/context.py); we stub it so the test is deterministic regardless of any
    # session another test in the same process may have left on the contextvar.
    def _raise():
        raise RuntimeError("no session bound")
    monkeypatch.setattr(main.jobs_context, "current_session_id", _raise)

    out = main._handle_skills_action("upload_file", {"filename": "doc.pdf", "data": _b64()})
    assert out["full_key"] == "artifacts/uploads/input/doc.pdf"
    assert out["content_type"] == "application/pdf"


def test_upload_file_requires_data(monkeypatch):
    monkeypatch.setattr(main, "s3_client", _FakeS3())
    monkeypatch.setattr(main, "S3_BUCKET", "skills-bkt")
    out = main._handle_skills_action("upload_file", {"filename": "x.pdf"})
    assert "error" in out and "data" in out["error"]
