"""Richer-wins for render manifests (asset-lineage R7.3).

A manifest key is a stable identity (job/node/content id), so a later render of the same job
OVERWRITES the earlier one. That is the point -- one tab per job -- but it was last-writer-wins:
a later, poorer render (an LLM fallback that wrote one marker, an error manifest, a re-render that
lost its sidecar) replaced a richer one, and the shots tab of staging session 6ac33e9e went
through three S3 versions to end on the worst. Before a manifest is written, read what is
already at its key; if that one carries more populated analysis items, keep it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ANALYSIS_KEYS = ("markers", "segments", "annotations")

# Keys already reported as kept, so a loop of poorer re-renders logs once per key, not per write.
_logged: set[str] = set()


def populated_items(manifest: Any) -> int:
    """How many analysis items a manifest carries: ``len(markers) + len(segments) + len(annotations)``."""
    if not isinstance(manifest, dict):
        return 0
    return sum(len(v) for v in (manifest.get(k) for k in _ANALYSIS_KEYS) if isinstance(v, list))


def _existing_manifest(s3: Any, bucket: str, key: str) -> Optional[dict]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            data = json.loads(body.read())
        finally:
            try:
                body.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - absent (the normal first write) or unreadable: write
        return None
    return data if isinstance(data, dict) else None


_SOURCE_FIELDS = ("sourceAssetId", "sourceLocation", "sourceKey")


def same_source(a: Any, b: Any) -> bool:
    """False when both manifests name their source media and the names differ.

    Compared on the first identity field BOTH carry (``sourceAssetId``, then ``sourceLocation``, then
    ``sourceKey``, the order tabs fold by). Richness only ranks two renders OF THE SAME media: staging
    session d6a560a8 kept NBA's 97-sentence diarization over Meridian's 86 at one shared key, so
    Meridian's tab lost its speakers. Unknown on either side counts as the same (the old behaviour).
    """
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return True
    for f in _SOURCE_FIELDS:
        x, y = a.get(f), b.get(f)
        if isinstance(x, str) and x and isinstance(y, str) and y:
            return x == y
    return True


def keep_existing(s3: Any, bucket: str, key: str, new_manifest: Any) -> bool:
    """True when the manifest already at ``key`` is RICHER than ``new_manifest`` (skip the write).

    Never raises; any read failure means "write". ``new_manifest`` may be a dict or JSON text.
    """
    keep, _ = prepare_write(s3, bucket, key, new_manifest)
    return keep


def prepare_write(s3: Any, bucket: str, key: str, new_manifest: Any) -> tuple[bool, Any]:
    """``(keep_existing, manifest_to_write)`` from ONE read of what is already at ``key``.

    ``keep_existing`` is the richer-wins verdict. Otherwise the returned manifest is ``new_manifest``
    with the existing manifest's user ``viewState`` (saved range / frame marker / crop, design §6)
    carried over when the new one has none, so a re-render never wipes what the user set. The
    returned value has the same type as the input (dict stays dict, JSON text stays text). Never raises.
    """
    text_in = isinstance(new_manifest, (str, bytes))
    parsed: Any = new_manifest
    if text_in:
        try:
            parsed = json.loads(new_manifest)
        except (ValueError, TypeError):
            parsed = None
    existing = _existing_manifest(s3, bucket, key)
    if existing is None or not same_source(existing, parsed):
        return False, new_manifest
    have, new = populated_items(existing), populated_items(parsed)
    if have > new:
        if key not in _logged:
            if len(_logged) > 512:
                _logged.clear()
            _logged.add(key)
            logger.info("render: keeping the richer manifest at %s (%d analysis items) over a new "
                        "write with %d", key, have, new)
        return True, None
    view_state = existing.get("viewState")
    if isinstance(parsed, dict) and isinstance(view_state, dict) and view_state and "viewState" not in parsed:
        merged = {**parsed, "viewState": view_state}
        return False, (json.dumps(merged) if text_in else merged)
    return False, new_manifest
