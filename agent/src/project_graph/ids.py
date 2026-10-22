"""Stable string node / edge ids. A re-sync recomputes the same id for the same thing, so upserts converge.

    project:<pid>   user:<sub>   tag:<nfc-lowercase>   asset:<assetId>   session:<sid>   job:<jobId>
    sample:<Label>:<n>
    edge: <TYPE>|<src>|<dst>
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .schema import NODE_LABELS, RELATIONSHIPS

_PREFIX_LABEL = {"project": "Project", "user": "User", "tag": "Tag", "asset": "Asset",
                 "session": "Session", "job": "Job"}
MAX_NODE_ID_LEN = 200
_NODE_ID_RE = re.compile(r"^(project|user|tag|asset|session|job|sample):[^\x00-\x1f\x7f]{1,190}$")


def project_node(pid: str) -> str:
    return f"project:{pid}"


def user_node(sub: str) -> str:
    return f"user:{sub}"


def tag_fold(tag: str) -> str:
    return unicodedata.normalize("NFC", tag).strip().lower()


def tag_node(tag: str) -> str:
    return f"tag:{tag_fold(tag)}"


def asset_node(asset_id: str) -> str:
    return f"asset:{asset_id}"


def session_node(session_id: str) -> str:
    return f"session:{session_id}"


def job_node(job_id: str) -> str:
    return f"job:{job_id}"


def sample_node(label: str, n: int) -> str:
    if label not in NODE_LABELS:
        raise ValueError(f"unknown label {label!r}")
    return f"sample:{label}:{n}"


def edge_id(rel: str, src: str, dst: str) -> str:
    if rel not in RELATIONSHIPS:
        raise ValueError(f"unknown relationship {rel!r}")
    return f"{rel}|{src}|{dst}"


def label_of(node_id: object) -> Optional[str]:
    """The node label an id belongs to, or ``None`` for a malformed id."""
    if not (isinstance(node_id, str) and len(node_id) <= MAX_NODE_ID_LEN and _NODE_ID_RE.match(node_id)):
        return None
    prefix, rest = node_id.split(":", 1)
    if prefix == "sample":
        label = rest.split(":", 1)[0]
        return label if label in NODE_LABELS else None
    return _PREFIX_LABEL.get(prefix)


def valid_node_id(node_id: object) -> bool:
    """Well-formed AND names a known label — what a neighbourhood route accepts from a URL."""
    return label_of(node_id) is not None
