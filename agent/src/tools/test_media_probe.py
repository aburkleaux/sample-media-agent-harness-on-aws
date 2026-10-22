"""Unit tests for media_probe tool.

Tests format detection, file size validation, unreachable URLs,
and supported/unsupported format handling.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from tools.media_probe import (
    media_probe,
    _extract_extension,
    _build_error,
    _build_success,
    SUPPORTED_FORMATS,
    MAX_FILE_SIZE_BYTES,
    CONNECTION_TIMEOUT_SECONDS,
)


# ---------------------------------------------------------------------------
# Helper extraction tests
# ---------------------------------------------------------------------------

class TestExtractExtension:
    def test_simple_url(self):
        assert _extract_extension("https://cdn.example.com/media/video.mp4") == "mp4"

    def test_url_with_query_params(self):
        assert _extract_extension("https://cdn.example.com/file.webm?token=abc") == "webm"

    def test_url_with_path_segments(self):
        assert _extract_extension("https://cdn.example.com/a/b/c/audio.flac") == "flac"

    def test_uppercase_extension(self):
        assert _extract_extension("https://cdn.example.com/VIDEO.MP4") == "mp4"

    def test_no_extension(self):
        assert _extract_extension("https://cdn.example.com/noext") is None

    def test_url_with_fragment(self):
        assert _extract_extension("https://cdn.example.com/clip.mov#t=10") == "mov"

    def test_empty_extension(self):
        assert _extract_extension("https://cdn.example.com/file.") is None


# ---------------------------------------------------------------------------
# Supported format detection
# ---------------------------------------------------------------------------

class TestSupportedFormats:
    """Verify all expected formats are in SUPPORTED_FORMATS."""

    def test_video_formats(self):
        assert "mp4" in SUPPORTED_FORMATS
        assert "webm" in SUPPORTED_FORMATS
        assert "mov" in SUPPORTED_FORMATS

    def test_audio_formats(self):
        assert "mp3" in SUPPORTED_FORMATS
        assert "wav" in SUPPORTED_FORMATS
        assert "flac" in SUPPORTED_FORMATS

    def test_mime_types(self):
        assert SUPPORTED_FORMATS["mp4"] == "video/mp4"
        assert SUPPORTED_FORMATS["webm"] == "video/webm"
        assert SUPPORTED_FORMATS["mov"] == "video/quicktime"
        assert SUPPORTED_FORMATS["mp3"] == "audio/mpeg"
        assert SUPPORTED_FORMATS["wav"] == "audio/wav"
        assert SUPPORTED_FORMATS["flac"] == "audio/flac"


# ---------------------------------------------------------------------------
# Media probe tool tests — using mocked requests
# ---------------------------------------------------------------------------

class TestMediaProbeSuccess:
    """Test successful probes for each supported format."""

    @pytest.mark.parametrize("ext,mime", [
        ("mp4", "video/mp4"),
        ("webm", "video/webm"),
        ("mov", "video/quicktime"),
        ("mp3", "audio/mpeg"),
        ("wav", "audio/wav"),
        ("flac", "audio/flac"),
    ])
    @patch("tools.media_probe.url_guard.get")
    def test_supported_format_returns_metadata(self, mock_get, ext, mime):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": mime,
            "Content-Length": "1048576",  # 1 MB
        }
        mock_get.return_value = mock_response

        url = f"https://cdn.example.com/media/test.{ext}"
        result = json.loads(media_probe(url=url))

        assert "error" not in result
        assert result["format"] == ext
        assert result["mime_type"] == mime
        assert result["file_size"] == 1048576
        assert result["url"] == url
        assert result["extension"] == ext
        # Duration, codec, width, height are null (not probed without download)
        assert result["duration"] is None
        assert result["codec"] is None
        assert result["width"] is None
        assert result["height"] is None


class TestMediaProbeUnreachable:
    """Test UNREACHABLE error handling."""

    @patch("tools.media_probe.url_guard.get")
    def test_connection_timeout(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout("Connection timed out")

        result = json.loads(media_probe(url="https://unreachable.example.com/video.mp4"))

        assert "error" in result
        assert result["error"]["code"] == "UNREACHABLE"
        assert "timed out" in result["error"]["message"].lower()

    @patch("tools.media_probe.url_guard.get")
    def test_connection_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("DNS resolution failed")

        result = json.loads(media_probe(url="https://badhost.example.com/video.mp4"))

        assert "error" in result
        assert result["error"]["code"] == "UNREACHABLE"

    @patch("tools.media_probe.url_guard.get")
    def test_non_2xx_status(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/missing.mp4"))

        assert "error" in result
        assert result["error"]["code"] == "UNREACHABLE"
        assert "404" in result["error"]["message"]


class TestMediaProbeFileTooLarge:
    """Test FILE_TOO_LARGE error handling."""

    @patch("tools.media_probe.url_guard.get")
    def test_exceeds_2gb(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(3 * 1024 * 1024 * 1024),  # 3 GB
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/huge.mp4"))

        assert "error" in result
        assert result["error"]["code"] == "FILE_TOO_LARGE"

    @patch("tools.media_probe.url_guard.get")
    def test_exactly_2gb_is_allowed(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(MAX_FILE_SIZE_BYTES),  # Exactly 2 GB
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/big.mp4"))

        # Exactly 2GB should NOT trigger FILE_TOO_LARGE (only > 2GB)
        assert "error" not in result
        assert result["format"] == "mp4"


class TestMediaProbeUnsupportedFormat:
    """Test UNSUPPORTED_FORMAT error handling."""

    @patch("tools.media_probe.url_guard.get")
    def test_unsupported_extension(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": "1024",
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/doc.pdf"))

        assert "error" in result
        assert result["error"]["code"] == "UNSUPPORTED_FORMAT"
        assert "supported_formats" in result["error"]
        assert set(result["error"]["supported_formats"]) == set(SUPPORTED_FORMATS.keys())

    @patch("tools.media_probe.url_guard.get")
    def test_mime_mismatch_unsupported(self, mock_get):
        """Extension is supported but MIME type doesn't match and isn't in supported set."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "text/html",  # Not a supported media MIME
            "Content-Length": "1024",
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/fake.mp4"))

        assert "error" in result
        assert result["error"]["code"] == "UNSUPPORTED_FORMAT"

    @patch("tools.media_probe.url_guard.get")
    def test_no_extension_unsupported_mime(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": "1024",
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/noext"))

        assert "error" in result
        assert result["error"]["code"] == "UNSUPPORTED_FORMAT"


class TestMediaProbeEdgeCases:
    """Edge case handling."""

    @patch("tools.media_probe.url_guard.get")
    def test_no_content_length_header(self, mock_get):
        """Missing Content-Length is fine — file_size is null."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "audio/mpeg",
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/song.mp3"))

        assert "error" not in result
        assert result["file_size"] is None
        assert result["format"] == "mp3"

    @patch("tools.media_probe.url_guard.get")
    def test_octet_stream_mime_with_valid_extension(self, mock_get):
        """Generic MIME type is accepted when extension is valid."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": "5000",
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/clip.wav"))

        assert "error" not in result
        assert result["format"] == "wav"
        assert result["mime_type"] == "audio/wav"

    @patch("tools.media_probe.url_guard.get")
    def test_mime_type_with_charset(self, mock_get):
        """MIME type with parameters (charset) is handled correctly."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "video/webm; codecs=vp8",
            "Content-Length": "2000",
        }
        mock_get.return_value = mock_response

        result = json.loads(media_probe(url="https://cdn.example.com/video.webm"))

        assert "error" not in result
        assert result["format"] == "webm"
        assert result["mime_type"] == "video/webm"

    @patch("tools.media_probe.url_guard.get")
    def test_timeout_value_is_3_seconds(self, mock_get):
        """Verify the ranged GET probe uses a 3-second timeout."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "100",
        }
        mock_get.return_value = mock_response

        media_probe(url="https://cdn.example.com/test.mp4")

        mock_get.assert_called_once_with(
            "https://cdn.example.com/test.mp4",
            timeout=3,
            allow_redirects=True,
            stream=True,
            headers={"Range": "bytes=0-0"},
        )


# ---------------------------------------------------------------------------
# Own artifact URLs are probed with head_object by key (the CDN may be private)
# ---------------------------------------------------------------------------

class TestMediaProbeOwnArtifact:
    DOMAIN = "d123.cloudfront.net"

    @pytest.fixture
    def s3(self, monkeypatch):
        from media import artifact_fetch as af

        calls = []

        class _S3:
            objects = {}

            def head_object(self, Bucket, Key):
                calls.append((Bucket, Key))
                if Key not in self.objects:
                    from botocore.exceptions import ClientError
                    raise ClientError({"Error": {"Code": "404", "Message": "Not Found"},
                                       "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
                return self.objects[Key]

        fake = _S3()
        fake.calls = calls
        monkeypatch.setenv("ARTIFACTS_DOMAIN", self.DOMAIN)
        monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bucket")
        monkeypatch.setattr(af, "_client", lambda: fake)
        return fake

    @patch("tools.media_probe.url_guard.get")
    def test_cloudfront_url_uses_head_object(self, mock_get, s3):
        s3.objects["artifacts/s1/clip.mp4"] = {"ContentLength": 2048, "ContentType": "video/mp4"}
        url = f"https://{self.DOMAIN}/artifacts/s1/clip.mp4"
        result = json.loads(media_probe(url=url))
        mock_get.assert_not_called()
        assert s3.calls == [("skills-bucket", "artifacts/s1/clip.mp4")]
        assert result == {
            "url": url, "format": "mp4", "mime_type": "video/mp4", "extension": "mp4",
            "file_size": 2048, "duration": None, "codec": None, "width": None, "height": None,
        }

    @patch("tools.media_probe.url_guard.get")
    def test_proxy_url_takes_extension_from_key(self, mock_get, s3):
        s3.objects["artifacts/s1/a.wav"] = {"ContentLength": 10, "ContentType": "binary/octet-stream"}
        result = json.loads(media_probe(url="/api/artifact?key=artifacts/s1/a.wav"))
        mock_get.assert_not_called()
        assert result["format"] == "wav" and result["mime_type"] == "audio/wav"

    @patch("tools.media_probe.url_guard.get")
    def test_missing_own_object_is_unreachable_404(self, mock_get, s3):
        result = json.loads(media_probe(url=f"https://{self.DOMAIN}/artifacts/s1/none.mp4"))
        mock_get.assert_not_called()
        assert result["error"]["code"] == "UNREACHABLE"
        assert result["error"]["http_status"] == 404
        assert "404" in result["error"]["message"]

    @patch("tools.media_probe.url_guard.get")
    def test_own_object_over_2gb_is_too_large(self, mock_get, s3):
        s3.objects["artifacts/s1/big.mp4"] = {"ContentLength": MAX_FILE_SIZE_BYTES + 1,
                                              "ContentType": "video/mp4"}
        result = json.loads(media_probe(url=f"https://{self.DOMAIN}/artifacts/s1/big.mp4"))
        assert result["error"]["code"] == "FILE_TOO_LARGE"

    @patch("tools.media_probe.url_guard.get")
    def test_foreign_url_still_uses_http(self, mock_get, s3):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 206
        mock_response.headers = {"Content-Type": "video/mp4", "Content-Range": "bytes 0-0/99"}
        mock_get.return_value = mock_response
        result = json.loads(media_probe(url="https://v3b.fal.media/files/out.mp4"))
        mock_get.assert_called_once()
        assert s3.calls == []
        assert result["file_size"] == 99
