"""Project id validation (media-harness-projects-access Req 1.4, 1.5).

A Project id is ``p`` followed by the 32 lowercase hex digits of a UUID v4. It doubles as the
hybrid-search ``collection_id``, so it also matches M2C's collection-id pattern
``^[a-z0-9][a-z0-9_-]{2,47}$`` and can never be ``default``. The TS twin is
``ui/server/projects/ids.ts``; both read ``infra/lib/projects/fixtures/project-id-cases.json``.
"""
from __future__ import annotations

import hashlib
import re

PROJECT_ID_PATTERN = re.compile(r"^p[0-9a-f]{32}$")


def is_project_id(s: object) -> bool:
    # fullmatch, not match: `$` alone accepts a trailing newline.
    return isinstance(s, str) and PROJECT_ID_PATTERN.fullmatch(s) is not None


def asset_id_for(kind: str, pid: str, key: str, version: str) -> str:
    """A well-formed ``<kind>:<uuid4>`` derived from (Project, key, version) — the dedupe identity.

    Here rather than in ``ingestion/`` (Lambda-only) because the agent runtime derives the same ids
    (``hooks.actions``' Moments dedup of a harvested image); ``ingestion.generated`` re-exports it.
    """
    h = bytearray(hashlib.sha256(f"{pid}\n{key}\n{version}".encode("utf-8")).digest()[:16])
    h[6] = (h[6] & 0x0F) | 0x40  # version 4
    h[8] = (h[8] & 0x3F) | 0x80  # RFC 4122 variant
    x = h.hex()
    return f"{kind}:{x[:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:]}"


def location_asset_id(kind: str, pid: str, bucket: str, key: str) -> str:
    """The asset id of an object registered IN PLACE: derived from (Project, bucket, key), not the version."""
    return asset_id_for(kind, pid, f"{bucket}/{key}", "in-place")
