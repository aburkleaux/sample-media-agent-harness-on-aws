"""Index submission chunks and refresh flags (Requirement 6, design Component 11).

Pure. ``History`` is the asset's record for the CURRENT collection incarnation: whether any ``index_content``
call for it was accepted (returned a job id), and the digest of its last completed Document_Record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

CHUNK_SIZE = 100


@dataclass(frozen=True)
class History:
    accepted: bool = False
    last_completed_digest: Optional[str] = None
    # Union of the scopes of accepted submissions in this incarnation (Req 6.9).
    scopes: frozenset = frozenset()


def refresh_mode(history: History, record_digest: str) -> bool:
    """False on a first submission to the incarnation (Req 6.4) or a byte-identical resubmission (Req 6.5)."""
    if not history.accepted:
        return False
    return history.last_completed_digest != record_digest


def chunk_count(n_docs: int) -> int:
    return (n_docs + CHUNK_SIZE - 1) // CHUNK_SIZE


def chunks(docs: Sequence[dict], refresh: bool) -> list[list[dict]]:
    """100 per chunk except the last (Req 6.3). With ``refresh``, ``true`` on every document of a scope in the
    FIRST chunk containing that scope and ``false`` after (Req 6.6); otherwise ``false`` everywhere."""
    out: list[list[dict]] = []
    seen: set[str] = set()
    for i in range(0, len(docs), CHUNK_SIZE):
        part = docs[i:i + CHUNK_SIZE]
        here = {d["doc_type"] for d in part}
        out.append([{**d, "refresh": bool(refresh and d["doc_type"] not in seen)} for d in part])
        seen |= here
    return out


def stale_scopes(history: History, record_scopes: frozenset) -> frozenset:
    """Scopes that held rows for the asset in this incarnation but get no document now → rebuild (Req 6.9)."""
    return frozenset(history.scopes) - frozenset(record_scopes)
