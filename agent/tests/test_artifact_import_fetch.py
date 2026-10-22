"""The shared https->bytes fetch helper behind artifact(operation='import', source_uri='https://…').

One module on purpose: the artifact tool is duplicated in main.py and task_agent.py, so the fetch
logic must NOT be copied into each. These pin the guards that keep a runaway URL from exhausting
runtime memory.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import artifact_import  # noqa: E402


class _FakeResp:
    """What ``security.url_guard.get`` returns (a streamed requests.Response, minimally)."""

    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers
        self.status_code = 200
        self.url = "https://cdn/x"

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        return None


def _patch_urlopen(monkeypatch, body: bytes, headers: dict):
    # The fetch goes through the shared SSRF guard; fake its transport, keep its cap logic.
    from security import url_guard
    monkeypatch.setattr(url_guard, "get", lambda url, **kw: _FakeResp(body, headers))


def test_rejects_non_https():
    with pytest.raises(ValueError, match="only https"):
        artifact_import.fetch_https_bytes("http://example.com/x.png")
    with pytest.raises(ValueError, match="only https"):
        artifact_import.fetch_https_bytes("s3://b/k")


def test_fetches_body_and_content_type(monkeypatch):
    _patch_urlopen(monkeypatch, b"\x89PNGdata", {"Content-Type": "image/png; charset=binary"})
    body, ct = artifact_import.fetch_https_bytes("https://cdn/x.png")
    assert body == b"\x89PNGdata"
    assert ct == "image/png"  # charset stripped


def test_missing_content_type_returns_empty(monkeypatch):
    _patch_urlopen(monkeypatch, b"abc", {})
    body, ct = artifact_import.fetch_https_bytes("https://cdn/x.bin")
    assert body == b"abc"
    assert ct == ""


def test_rejects_when_content_length_over_cap(monkeypatch):
    _patch_urlopen(monkeypatch, b"x", {"Content-Length": str(artifact_import.IMPORT_HTTPS_MAX_BYTES + 1)})
    with pytest.raises(ValueError, match="import cap"):
        artifact_import.fetch_https_bytes("https://cdn/huge.mp4")


def test_rejects_when_streamed_body_over_cap(monkeypatch):
    # No/understated Content-Length: the read(max+1) overflow guard must still fire.
    _patch_urlopen(monkeypatch, b"y" * 20, {})  # no Content-Length header
    with pytest.raises(ValueError, match="import cap"):
        artifact_import.fetch_https_bytes("https://cdn/huge.mp4", max_bytes=10)
