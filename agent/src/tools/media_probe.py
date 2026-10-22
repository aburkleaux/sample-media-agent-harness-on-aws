"""Media Probe tool — validates and probes media files by URL.

Performs format detection via file extension and MIME type matching,
file size validation (2GB limit), and reachability checks with a
3-second connection timeout.

Returns JSON with media metadata on success, or structured error
objects with codes: UNSUPPORTED_FORMAT, FILE_TOO_LARGE, UNREACHABLE.
"""

import json
import logging
from urllib.parse import urlparse

import requests
from strands import tool

from media.artifact_fetch import head_artifact, own_artifact_key
from security import url_guard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported formats: extension → MIME type mapping
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: dict[str, str] = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
}

# Reverse lookup: MIME type → extension
MIME_TO_EXT: dict[str, str] = {v: k for k, v in SUPPORTED_FORMATS.items()}

# Maximum supported file size: 2 GB
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Connection timeout for HEAD request
CONNECTION_TIMEOUT_SECONDS = 3


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _extract_extension(url: str) -> str | None:
    """Extract file extension from URL path, ignoring query params."""
    parsed = urlparse(url)
    path = parsed.path
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        return ext if ext else None
    return None


def _build_error(code: str, message: str, **extra) -> str:
    """Build a JSON error response string."""
    error = {"error": {"code": code, "message": message, **extra}}
    return json.dumps(error)


def _build_success(
    url: str,
    extension: str,
    mime_type: str,
    file_size: int | None,
) -> str:
    """Build a JSON success response string with media metadata."""
    result = {
        "url": url,
        "format": extension,
        "mime_type": mime_type,
        "extension": extension,
        "file_size": file_size,
        # Duration, codec, width, height require actual media inspection
        # (e.g., ffprobe via code_interpreter). Set to null in probe response.
        "duration": None,
        "codec": None,
        "width": None,
        "height": None,
    }
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Media Probe Tool
# ---------------------------------------------------------------------------

@tool
def media_probe(url: str) -> str:
    """Probe media file for metadata: duration, codec, resolution, format.

    Validates that the URL is reachable, the file size is within limits
    (max 2GB), and the format is supported (mp4, webm, mov, mp3, wav, flac).

    Args:
        url: CloudFront or S3 URL of the media artifact

    Returns:
        JSON string with media metadata on success, or an error object
        with code UNSUPPORTED_FORMAT, FILE_TOO_LARGE, or UNREACHABLE.
    """
    # Step 1: Extract file extension from URL. For our own proxy form
    # (`/api/artifact?key=artifacts/x.mp4`) the extension lives in the key, not the path.
    own_key = own_artifact_key(url)
    extension = _extract_extension(url)
    if own_key and not extension:
        extension = _extract_extension("/" + own_key)

    # Step 2a: one of OUR artifact URLs -> head_object by key. The CloudFront/proxy URL may be
    # private (403 to an unsigned GET), and S3 by key is the same object without the CDN hop.
    try:
        head = head_artifact(url)
    except Exception as exc:  # noqa: BLE001 - missing/denied object reads as unreachable
        status = _s3_error_status(exc)
        logger.warning("Media probe (S3) failed for %s: %s", url, exc)
        extra = {"http_status": status} if status else {}
        return _build_error(
            code="UNREACHABLE",
            message=(f"URL returned HTTP {status}" if status
                     else f"Unable to read artifact: {str(exc)[:200]}"),
            **extra,
        )
    if head is not None:
        file_size = head.get("ContentLength")
        try:
            file_size = int(file_size) if file_size is not None else None
        except (TypeError, ValueError):
            file_size = None
        return _classify(url, extension, file_size, head.get("ContentType") or "")

    # Step 2b: Probe with a RANGED GET (not HEAD). Presigned S3 urls are signed for a specific
    # method: a url signed for GET returns 403 to a HEAD, which made every presigned media probe
    # fail as UNREACHABLE. `Range: bytes=0-0` fetches a single byte, so it works with GET presigns
    # and does not download the file; the total size comes from the Content-Range header.
    # Model-supplied url: through the shared SSRF guard (https, public addresses only, pinned,
    # every redirect re-validated).
    try:
        response = url_guard.get(
            url,
            timeout=CONNECTION_TIMEOUT_SECONDS,
            allow_redirects=True,
            stream=True,
            headers={"Range": "bytes=0-0"},
        )
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass
    except url_guard.BlockedURLError as exc:
        logger.warning("Media probe refused %s: %s", url, exc)
        return _build_error(code="UNREACHABLE", message=f"URL refused: {exc}")
    except (requests.ConnectionError, requests.Timeout, requests.RequestException) as exc:
        logger.warning("Media probe failed for %s: %s", url, exc)
        return _build_error(
            code="UNREACHABLE",
            message=f"Unable to reach URL: {str(exc)[:200]}",
        )

    # Step 3: Check for non-2xx HTTP status (206 Partial Content is the expected ranged reply).
    if not response.ok:
        return _build_error(
            code="UNREACHABLE",
            message=f"URL returned HTTP {response.status_code}",
            http_status=response.status_code,
        )

    # Step 4: Total file size — from Content-Range ("bytes 0-0/12345") when the server honored the
    # range, else Content-Length (a server that ignored Range and returned the whole file).
    file_size: int | None = None
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        try:
            file_size = int(content_range.rsplit("/", 1)[-1])
        except (ValueError, TypeError):
            file_size = None
    if file_size is None:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                file_size = int(content_length)
            except (ValueError, TypeError):
                file_size = None

    return _classify(url, extension, file_size, response.headers.get("Content-Type", ""))


def _s3_error_status(exc: BaseException) -> int | None:
    """HTTP status carried by a botocore ClientError (404 for a missing key), else None."""
    try:
        status = exc.response["ResponseMetadata"]["HTTPStatusCode"]  # type: ignore[attr-defined]
        return int(status) if status else None
    except Exception:  # noqa: BLE001
        return None


def _classify(url: str, extension: str | None, file_size: int | None, content_type: str) -> str:
    """Size + format validation shared by the HTTP and S3 probe paths."""
    if file_size is not None and file_size > MAX_FILE_SIZE_BYTES:
        return _build_error(
            code="FILE_TOO_LARGE",
            message=f"File size {file_size} bytes exceeds maximum of {MAX_FILE_SIZE_BYTES} bytes (2GB)",
            file_size=file_size,
            max_size=MAX_FILE_SIZE_BYTES,
        )

    # Step 5: Determine MIME type from response headers
    # Strip charset or other params: "video/mp4; charset=utf-8" → "video/mp4"
    detected_mime = content_type.split(";")[0].strip().lower() if content_type else ""

    # Step 6: Validate format — check extension and MIME type
    # Extension must be in supported set
    if extension and extension in SUPPORTED_FORMATS:
        expected_mime = SUPPORTED_FORMATS[extension]
        # If server returned a MIME type, check it matches (or accept if generic)
        if detected_mime and detected_mime not in ("application/octet-stream", "binary/octet-stream", ""):
            if detected_mime != expected_mime:
                # MIME mismatch — still allow if the detected MIME is itself supported
                if detected_mime not in MIME_TO_EXT:
                    return _build_error(
                        code="UNSUPPORTED_FORMAT",
                        message=f"MIME type '{detected_mime}' does not match extension '.{extension}' and is not a supported format",
                        supported_formats=list(SUPPORTED_FORMATS.keys()),
                    )
                # Use the detected MIME's extension as the canonical format
                extension = MIME_TO_EXT[detected_mime]
                expected_mime = detected_mime
        mime_type = expected_mime
    elif detected_mime and detected_mime in MIME_TO_EXT:
        # Extension not recognized but MIME type is supported
        extension = MIME_TO_EXT[detected_mime]
        mime_type = detected_mime
    else:
        # Neither extension nor MIME type is in supported set
        return _build_error(
            code="UNSUPPORTED_FORMAT",
            message=f"Format not supported. Extension: '{extension or 'none'}', MIME: '{detected_mime or 'none'}'",
            supported_formats=list(SUPPORTED_FORMATS.keys()),
        )

    # Step 7: Return success response with metadata
    return _build_success(
        url=url,
        extension=extension,
        mime_type=mime_type,
        file_size=file_size,
    )
