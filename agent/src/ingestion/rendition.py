"""Search_Rendition of an image (Req 8.5, 8.6, 8.13; design Component 12). Pure over bytes; needs Pillow.

decode (over 100 MP counts as undecodable) → EXIF orientation → transparency composited onto white → RGB →
scale without upscaling so the longer side is ≤ 1,568 px → LANCZOS → baseline JPEG at quality 90, 85, 80, 75, 70,
60, 50 until it is ≤ 1,500,000 bytes. An animated image contributes its first frame.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

MAX_SIDE = 1568
MAX_BYTES = 1_500_000
MAX_PIXELS = 100_000_000
QUALITIES = (90, 85, 80, 75, 70, 60, 50)


class RenditionError(Exception):
    """The image cannot become a Search_Rendition; the message is the reason's text."""


@dataclass(frozen=True)
class Rendition:
    jpeg: bytes
    width: int
    height: int


def target_size(w: int, h: int) -> tuple[int, int]:
    scale = min(1.0, MAX_SIDE / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def render(src: bytes) -> Rendition:
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(src)) as probe:
            w, h = probe.size
    except Exception as exc:  # noqa: BLE001 — every decode failure is the same outcome
        raise RenditionError(f"the image cannot be decoded ({type(exc).__name__})") from exc
    if w <= 0 or h <= 0:
        raise RenditionError("the image has a zero width or height")
    if w * h > MAX_PIXELS:
        raise RenditionError(f"the image is {w}x{h}, over the {MAX_PIXELS:,}-pixel decode limit")
    try:
        im = Image.open(io.BytesIO(src))
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise RenditionError(f"the image cannot be decoded ({type(exc).__name__})") from exc
    size = target_size(*im.size)
    if size != im.size:
        im = im.resize(size, Image.Resampling.LANCZOS)
    for q in QUALITIES:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, progressive=False, optimize=True)
        if buf.tell() <= MAX_BYTES:
            return Rendition(buf.getvalue(), size[0], size[1])
    raise RenditionError(f"no JPEG quality fits the {MAX_BYTES:,}-byte limit")


MAX_SOURCE_BYTES = 50 * 1024 * 1024  # the upload limit for images (limits.DEFAULT_UPLOAD_LIMITS)


def store_search_rendition(s3, *, source_bucket: str, source_key: str, ingest_bucket: str, out_key: str) -> dict:
    """Read an image, render its Search_Rendition, and write it to ``ingest_bucket/out_key``.

    The one place a rendition is made, for ingestion (IngestTick) and for a search query image that ingestion
    will never render (Asset_Service ``search_images``: a "this chat only" upload, a stored asset). Returns the
    row's ``rendition`` record ``{key, width, height, bytes, sha256}``; :class:`RenditionError` when the image
    cannot become one. An unreadable source raises the S3 client's own error.
    """
    import hashlib

    obj = s3.get_object(Bucket=source_bucket, Key=source_key)
    if int(obj.get("ContentLength") or 0) > MAX_SOURCE_BYTES:
        raise RenditionError("the image is too large to decode")
    out = render(obj["Body"].read(MAX_SOURCE_BYTES + 1))
    s3.put_object(Bucket=ingest_bucket, Key=out_key, Body=out.jpeg, ContentType="image/jpeg")
    return {"key": out_key, "width": out.width, "height": out.height, "bytes": len(out.jpeg),
            "sha256": hashlib.sha256(out.jpeg).hexdigest()}

