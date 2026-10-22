"""Gallery manifests the harness builds itself (no Render Agent turn), with the labels the caller gives.

A Render Agent asked for "one audio clip-gallery, labelled with voice names" built an nle-timeline whose tracks
were labelled by file id (staging session a5b5acaf: four tries before agent_olive_voice_v4 could be found and
played). The layout and the labels are known up front, so they are written deterministically here.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

from render import types as render_types

_URL = re.compile(r"https://[^\s\"'<>\\]+")
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}


_ARTIFACT_KEY = re.compile(r"^artifacts/[^\s\"'<>\\]+$")


def _clean(url: Any) -> str:
    """The first https url in ``url`` (a value that glued a second name on with ``\\n`` keeps only the first),
    or a stable artifact key (``artifacts/...``, e.g. an ``artifacts/m2c/<bucket>/<key>`` reference)."""
    if isinstance(url, str) and _ARTIFACT_KEY.match(url.strip()):
        return url.strip()
    m = _URL.search(url) if isinstance(url, str) else None
    return m.group(0) if m else ""


def _ext(url: str) -> str:
    name = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def item_kind(item: dict, url: str) -> str:
    kind = str(item.get("kind") or "").lower()
    if kind in ("audio", "video", "image"):
        return kind
    ext = _ext(url)
    if ext in _IMAGE_EXTS:
        return "image"
    return "video" if ext in render_types.VIDEO_EXTS else "audio"


def build_gallery_manifest(items: list, title: str = "", subtitle: str = "", manifest_id: str = "") -> dict:
    """``clip-gallery`` (audio / video: playable cards with mark-in/out) or, when every item is an image, an image
    gallery. Each item is ``{url, label, text?, kind?, durationHint?}``; items without an https url or artifact key are dropped."""
    entries: list[dict] = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        url = _clean(it.get("url") or it.get("src"))
        if not url:
            continue
        e: dict = {"label": str(it.get("label") or f"Item {i + 1}")[:200], "src": url, "kind": item_kind(it, url)}
        if it.get("text"):
            e["text"] = str(it["text"])[:2000]
        dh = it.get("durationHint")
        if isinstance(dh, (int, float)) and not isinstance(dh, bool) and dh > 0:
            e["durationHint"] = float(dh)
        entries.append(e)
    mid = manifest_id or f"gallery-{uuid.uuid4().hex[:24]}"
    base = {"id": mid, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    if title:
        base["title"] = title
    if subtitle:
        base["subtitle"] = subtitle
    if entries and all(e["kind"] == "image" for e in entries):
        return {**base, "template": "image-annotated", "src": entries[0]["src"], "annotations": [],
                "images": [{"src": e["src"], "label": e["label"], "annotations": []} for e in entries]}
    clips = [{"id": f"clip{n + 1}", "label": e["label"], "src": e["src"],
              "kind": "video" if e["kind"] == "video" else "audio",
              **({"text": e["text"]} if "text" in e else {}),
              **({"durationHint": e["durationHint"]} if "durationHint" in e else {})}
             for n, e in enumerate(entries) if e["kind"] in ("audio", "video")]
    return {**base, "template": "clip-gallery", "galleryClips": clips}
