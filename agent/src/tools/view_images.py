"""Let the model SEE a few images: return them as image blocks in the tool result.

Nothing else hands the orchestrator pixels. A code cell can extract frames, but its result is text (the
file's url), so "describe this shot" meant either a 12-minute enrichment job over every shot of a film or a
description invented from file names. Workshop Lab 1 needed one shot described from its frames, the way the
enrichment tool does it: a few full frames as evidence, the transcript for what is said.

Sources, read with the privileges the runtime already uses for result files (``read_result_files``):

* this session's own outputs — an artifacts-CDN url or ``s3://<skills bucket>/…`` under the session's
  ``output/`` roots, read straight from S3 (the CDN is private). Any other key in that bucket is refused:
  it holds every project's assets, and project access is checked elsewhere;
* any other ``s3://`` object (M2C outputs, e.g. a shot's frame grid), presigned through the gateway's
  download-url tool, falling back to a direct read;
* an ``https`` url, fetched as is (a presigned url from ``get_download_url``).

Only real images get through: the bytes must carry a jpeg/png/gif/webp signature and fit Bedrock's
per-image cap. Models that reject images inside tool results (GPT) get them hoisted by ``model/factory``.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import unquote, urlparse

from strands import tool

from security import url_guard

logger = logging.getLogger(__name__)

MAX_IMAGES = 8
#: Bedrock's Converse limit is 3.75 MB per image.
MAX_BYTES_PER_IMAGE = 3_750_000
MAX_TOTAL_BYTES = 12_000_000
FETCH_TIMEOUT_S = 15.0


def image_format(raw: bytes) -> Optional[str]:
    """The Converse image format named by the file signature, or ``None`` when it is not an image."""
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _skills_bucket() -> str:
    return os.environ.get("SKILLS_S3_BUCKET", "")


def _session_key(ref: str) -> Optional[str]:
    """The skills-bucket key ``ref`` names (artifacts url or ``s3://`` uri), or ``None`` when it names none."""
    bucket, domain = _skills_bucket(), os.environ.get("ARTIFACTS_DOMAIN", "")
    if bucket and ref.startswith(f"s3://{bucket}/"):
        return ref[len(f"s3://{bucket}/"):]
    u = urlparse(ref)
    if domain and u.scheme == "https" and u.netloc == domain:
        return unquote(u.path.lstrip("/"))
    return None


def _session_roots() -> list[str]:
    """This turn's session ``output/`` prefixes. Read on the CALLING thread: the session is a contextvar,
    and the fetch pool's threads do not inherit it."""
    from sandbox_outputs import output_roots  # noqa: PLC0415
    return output_roots()


def _read_s3(bucket: str, key: str) -> bytes:
    import boto3  # noqa: PLC0415
    obj = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2")).get_object(Bucket=bucket, Key=key)
    return obj["Body"].read(MAX_BYTES_PER_IMAGE + 1) or b""


def _read_http(url: str) -> bytes:
    """Model-supplied url -> through the SSRF guard (https, public addresses only, pinned)."""
    # One byte past the cap so the caller can tell an oversized image from one that fits.
    return url_guard.fetch(url, max_bytes=MAX_BYTES_PER_IMAGE + 1, timeout=FETCH_TIMEOUT_S,
                           truncate=True).body


def _fetch(ref: str, roots: list[str]) -> tuple[Optional[bytes], str]:
    """``(bytes, "")`` or ``(None, why)``. Never raises."""
    try:
        key = _session_key(ref)
        if key is not None:
            if not any(key.startswith(root) for root in roots):
                return None, "only this session's own output files can be viewed from the skills bucket"
            return _read_s3(_skills_bucket(), key), ""
        if ref.startswith("s3://"):
            from tools.result_files import _presign_many  # noqa: PLC0415
            url = _presign_many([ref]).get(ref)
            if url:
                return _read_http(url), ""
            bucket, _, k = ref[len("s3://"):].partition("/")
            if not bucket or not k:
                return None, "not an s3://bucket/key uri"
            return _read_s3(bucket, k), ""
        if ref.startswith("https://"):
            return _read_http(ref), ""
        return None, "not an https url or s3:// uri"
    except Exception as exc:  # noqa: BLE001 — one unreadable image is reported, the rest still load
        return None, f"fetch failed: {exc}"


@tool
def view_images(refs: list[str], note: str = "") -> dict:
    """LOOK at up to 8 images yourself — they come back as pictures you can see, not as text.

    Use it when an answer depends on what is IN an image: describing a video shot from a few of its
    frames, reading on-screen text, checking a generated image or a chart before you present it. For a
    video, extract the frames first in the code sandbox (e.g. the first, middle and last frame of a shot,
    as jpeg, at most ~1280 px wide) and save them in this session's output folder — the cell result lists
    their urls — then pass those urls here. Prefer 1–4 images; each one costs context.

    Args:
        refs: Image urls or ``s3://`` uris: files the code sandbox saved in this session's output folder,
            presigned urls (e.g. from get_download_url), or ``s3://`` uris of job outputs (a shot's frame
            grid). Other projects' and sessions' files are refused.
        note: Optional — what you are looking for; repeated back above the images as a reminder.

    Returns:
        The images, each labelled with its ref, plus one line per ref that could not be loaded.
    """
    refs = [r.strip() for r in (refs or []) if isinstance(r, str) and r.strip()]
    refs = list(dict.fromkeys(refs))
    if not refs:
        return {"status": "error", "content": [{"text": "view_images needs at least one image url or s3:// uri."}]}
    skipped = refs[MAX_IMAGES:]
    refs = refs[:MAX_IMAGES]
    roots = _session_roots()
    with ThreadPoolExecutor(max_workers=len(refs)) as pool:
        fetched = list(pool.map(lambda r: _fetch(r, roots), refs))

    content: list = []
    problems: list[str] = []
    total = 0
    for i, (ref, (raw, why)) in enumerate(zip(refs, fetched), start=1):
        if raw is None:
            problems.append(f"{ref}: {why}")
            continue
        if len(raw) > MAX_BYTES_PER_IMAGE:
            problems.append(f"{ref}: larger than {MAX_BYTES_PER_IMAGE // 1_000_000} MB — save a smaller copy")
            continue
        fmt = image_format(raw)
        if fmt is None:
            problems.append(f"{ref}: not a jpeg, png, gif or webp image")
            continue
        if total + len(raw) > MAX_TOTAL_BYTES:
            problems.append(f"{ref}: skipped — the images together are over {MAX_TOTAL_BYTES // 1_000_000} MB")
            continue
        total += len(raw)
        content.append({"text": f"Image {i}: {ref.split('?', 1)[0].rsplit('/', 1)[-1]}"})
        content.append({"image": {"format": fmt, "source": {"bytes": raw}}})
    problems += [f"{r}: skipped — at most {MAX_IMAGES} images per call" for r in skipped]

    shown = sum(1 for c in content if "image" in c)
    head = f"{shown} image(s) below." + (f" Looking for: {note.strip()}" if note.strip() else "")
    tail = [{"text": "Not shown:\n" + "\n".join(problems)}] if problems else []
    return {"status": "success" if shown else "error", "content": [{"text": head}, *content, *tail]}
