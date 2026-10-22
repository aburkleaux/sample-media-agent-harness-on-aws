"""A video's hero image: one captured frame copied next to the asset as its ``rendition`` (Projects surface tile).

The frame is the representative frame of the video's longest "content" shot — inside the middle 80% of the
timeline and without on-screen text (credits and title cards), falling back to the longest shot — taken from
the shot documents the build just produced, so it is a frame the index already points at. It is copied from M2C's proxy
bucket to ``projects/<pid>/<assetId>/rendition.jpg`` in the Ingest bucket, the same key an image's
Search_Rendition uses, so the Asset_URL_Route serves both the same way and the asset's own prefix owns it.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from projects import schema


def pick_frame(documents: Sequence[dict]) -> Optional[str]:
    """``s3://…`` of the hero frame, or ``None`` when there is no shot document.

    The longest shot, preferring one that (a) lies inside the middle 80% of the timeline and (b) whose scene
    shows no on-screen text: the longest shot of a film is often its end credits or a title card (measured on
    Netflix_Open_Content_Meridian: the longest shot is the cast list). Falls back to the longest shot overall.
    """
    shots = [d for d in documents if d.get("doc_type") == "shot"]
    if not shots:
        return None
    end = max(d["end_ms"] for d in documents)
    texty = {f"scene:{d['start_ms']}:{d['end_ms']}" for d in documents if d.get("doc_type") == "scene"
             and any("\nOn-screen text: " in f"\n{c.get('value', '')}" for c in d.get("content") or []
                     if c.get("field") == "text")}

    def ok(d: dict) -> bool:
        mid = (d["start_ms"] + d["end_ms"]) / 2
        return 0.1 * end <= mid <= 0.9 * end and (d.get("fields") or {}).get("scene_parent_id") not in texty

    pool = [d for d in shots if ok(d)] or shots
    best = max(pool, key=lambda d: (d["end_ms"] - d["start_ms"], -d["start_ms"]))
    uri = next((c.get("value") for c in best.get("content") or [] if c.get("kind") == "image"), None)
    return uri if isinstance(uri, str) and uri.startswith("s3://") else None


def poster_key(pid: str, asset_id: str) -> str:
    return f"{schema.project_prefix(pid)}{asset_id}/rendition.jpg"


def copy_poster(s3: Any, frame_uri: str, ingest_bucket: str, pid: str, asset_id: str,
                width: int, height: int) -> dict:
    """Copy the frame; return the row's ``rendition`` attribute."""
    src_bucket, src_key = frame_uri[5:].split("/", 1)
    key = poster_key(pid, asset_id)
    s3.copy_object(Bucket=ingest_bucket, Key=key, CopySource={"Bucket": src_bucket, "Key": src_key},
                   ContentType="image/jpeg", MetadataDirective="REPLACE")
    size = int(s3.head_object(Bucket=ingest_bucket, Key=key).get("ContentLength") or 0)
    return {"key": key, "width": width, "height": height, "bytes": size, "fromFrame": src_key}
