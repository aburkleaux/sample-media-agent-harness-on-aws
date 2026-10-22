"""Shared helper: fetch an ``https://`` artifact into memory for durable re-store.

Used by the ``artifact(operation='import', source_uri='https://...')`` path in BOTH the
main agent (``main.py``) and the task / plan-node agent (``task_agent.py``). Kept in ONE
module on purpose: the artifact tool is duplicated across those two files, and a fetch
helper copied into each would be exactly the kind of pair that silently drifts.

Why this exists — the image-to-image / generation persist gap (run 627fb96e): a generation
plan node has NO ``code_interpreter``, and a fal output lives on an ephemeral CDN
(``fal.media`` / our own CloudFront mirror). Before this, ``import`` only accepted an
``s3://`` URI, so a node had to *guess* the auto-mirrored ``artifacts/external/<hash>``
key to copy the image into its output prefix. 6 of 8 oil-paint nodes guessed wrong and
persisted nothing while still reporting success. Fetching the ``https://`` URL directly
removes the guess: the node passes the URL it already has.
"""
from __future__ import annotations


# Cap the fetch so a runaway or hostile URL cannot exhaust runtime memory. Comfortably
# covers generated stills (a few MB) and short lip-sync / clip videos (tens of MB).
IMPORT_HTTPS_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_HTTP_TIMEOUT_S = 60
#: Wall-clock bound on one whole import download.
_HTTP_DEADLINE_S = 600


def own_artifact_s3_uri(url: str, bucket: str, domain: str | None = None) -> str | None:
    """``s3://<bucket>/<key>`` when ``url`` is one of OUR artifact URLs, else None.

    The ``import`` tool uses this to turn an own-artifact https source into the same
    server-side ``copy_object`` its ``s3://`` branch already does — no bytes through this
    process, and it keeps working when the artifacts CDN is private.
    """
    if not bucket:
        return None
    from media.artifact_fetch import own_artifact_key

    key = own_artifact_key(url, domain if domain else None)
    return f"s3://{bucket}/{key}" if key else None


def fetch_https_bytes(url: str, max_bytes: int = IMPORT_HTTPS_MAX_BYTES) -> tuple[bytes, str]:
    """Download ``url`` into memory, bounded. Returns ``(body, content_type)``.

    ``content_type`` is the server's ``Content-Type`` (charset stripped), or ``""`` when the
    server sent none — the caller decides the final MIME (explicit arg > extension > this).

    Raises:
        ValueError: the URL is not ``https://``, its advertised ``Content-Length`` exceeds
            ``max_bytes``, or the streamed body runs past ``max_bytes`` (no/observed wrong
            Content-Length). ``security.url_guard.BlockedURLError`` (a ValueError) when the URL may not be
            fetched (non-public address). Network / HTTP errors from requests propagate.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("only https:// URLs can be fetched")
    from media.artifact_fetch import head_artifact, is_own_artifact, read_artifact_bytes

    if is_own_artifact(url):
        # One of OUR artifact URLs: read the object by key. The CDN may be private (403 to an
        # unsigned GET), and S3 is the same bytes without the round trip. The `import` tool
        # itself prefers own_artifact_s3_uri() + a server-side copy; this keeps any other
        # caller correct too.
        head = head_artifact(url) or {}
        size = head.get("ContentLength")
        if isinstance(size, int) and size > max_bytes:
            raise ValueError(f"remote file is {size} bytes, over the {max_bytes}-byte import cap")
        body = read_artifact_bytes(url, max_bytes=max_bytes)
        if len(body) > max_bytes:
            raise ValueError(f"remote file exceeds the {max_bytes}-byte import cap")
        ct = (head.get("ContentType") or "").split(";", 1)[0].strip()
        return body, ct
    # Not ours: the URL is model-supplied -> the shared SSRF guard (public addresses only,
    # pinned connection, every redirect re-validated, streamed byte cap).
    from security import url_guard  # noqa: PLC0415

    try:
        fetched = url_guard.fetch(
            url, max_bytes=max_bytes, timeout=_HTTP_TIMEOUT_S, deadline_s=_HTTP_DEADLINE_S,
            headers={"User-Agent": "mediaworks-harness/artifact-import"})
    except url_guard.ResponseTooLarge as exc:
        raise ValueError(f"remote file exceeds the {max_bytes}-byte import cap ({exc})") from None
    body = fetched.body
    ct = (fetched.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    return body, ct
