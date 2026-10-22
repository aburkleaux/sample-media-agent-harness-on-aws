"""Citation classification (design Component 4 "Classification", Req 2.5, 2.10).

Pure module, no I/O. The turn-end path (`turn_end.finish_turn`, Component 4) and its property test
are the only callers. Given the Citations parsed from a turn's answer, the session's delivered Hits
under the Active_Project, and the registry resolution of those Hits' assets, this module decides which
Citations are **verified** and attaches the Asset_Reference, kind, and recency the SPA and the
Moments_Rule read.

A Citation is verified exactly when some delivered Hit of the session, returned under the same
Active_Project, is a well-formed Hit (Req 3.1) whose resolved Project_Asset is not ``removed``, whose
Display_Name equals the cited name case-insensitively, and whose whole-second timespan contains the
cited range (Req 2.5). The Hit's own ``title`` is never consulted, so a Hit indexed under an older
title verifies only against the current Display_Name. When several Hits qualify, the most recent one
wins (Req 3.4 tie-break), which is also the Hit the Moments_Rule reuses.

Three facts matter for the reader:

- **No URL ever appears here.** :class:`AssetReference` carries ``project_id``, ``asset_id`` and,
  only for a shot Hit, a ``frame_s3_key`` — never a presigned URL. The SPA mints URLs itself
  (Component 5), so a stored record cannot leak or expire one.
- **One record per distinct citation key.** Classification depends only on the normalized name and
  the start/end, so repeated Citations always classify alike; collapsing them keeps the block small
  and gives the SPA a markdown-independent lookup.
- **The registry being unreadable is distinct from no match.** ``assets is None`` means the whole
  resolution could not be trusted (``RegistryUnavailable``); every record is then unverified with
  reason ``"registry_unavailable"``, which errs toward showing less.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .assets import ResolvedAsset
from projects.document_locator import page_of_span

from .citations import Citation, normalize_name
from .session_hits import SessionHit

# A turn stores at most this many distinct Citations; the rest render as plain, unverified text
# (Component 5), which shows less rather than guessing. `turn_end` sets `truncated` when a turn has
# more distinct keys than this.
MAX_RECORDS = 100

_REMOVED_STATUS = "removed"
_SHOT_SOURCE = "shot"


@dataclass(frozen=True)
class AssetReference:
    """The Asset_Reference a verified Citation points at — never a URL.

    ``frame_s3_key`` is set only for a shot Hit that carries a non-empty key; the SPA mints a fresh
    URL from these identifiers itself, so nothing here can expire or leak.
    """

    project_id: str
    asset_id: str
    frame_s3_key: Optional[str] = None  # set only for a shot Hit that carries one


@dataclass(frozen=True)
class CitationRecord:
    """One distinct Citation of a turn, classified. Verified-only fields are ``None`` otherwise."""

    key: str                                      # citation_key of the Citation
    text: str                                     # first occurrence as written
    display_name: str                             # as cited, trimmed
    start_ms: int
    end_ms: int
    verified: bool
    reason: Optional[str]                         # None when verified; else "no_matching_hit" | "registry_unavailable"
    ref: Optional[AssetReference] = None          # verified only
    kind: Optional[str] = None                    # verified only: the Project_Asset's kind
    asset_display_name: Optional[str] = None      # verified only: the registry Display_Name (tab label, a11y name)
    hit_recency: Optional[tuple[int, int, int]] = None  # verified only: recency of the chosen Hit (Component 6)
    page: Optional[int] = None                    # a page citation's first page (a document); None for a timecode
    page_end: Optional[int] = None                # its last page (== page for one page)


def is_hit(h: Any) -> bool:
    """The Hit shape of Req 3.1, shared with the Moments_Rule.

    ``True`` when ``h`` is a dict with a str ``asset_id``, ``int`` (not ``bool``) ``start_ms`` and
    ``end_ms`` with ``start_ms < end_ms``, and an ``int`` or ``float`` (not ``bool``) ``final_score``.
    ``bool`` is a subclass of ``int``, so it is excluded explicitly.
    """
    if not isinstance(h, dict):
        return False
    asset_id = h.get("asset_id")
    if not isinstance(asset_id, str):
        return False
    start_ms = h.get("start_ms")
    end_ms = h.get("end_ms")
    if not _is_int(start_ms) or not _is_int(end_ms):
        return False
    if not start_ms < end_ms:
        return False
    final_score = h.get("final_score")
    if isinstance(final_score, bool) or not isinstance(final_score, (int, float)):
        return False
    return True


def whole_second_range(h: dict) -> tuple[int, int]:
    """``(floor(start_ms / 1000) * 1000, ceil(end_ms / 1000) * 1000)``.

    Widening the Hit's millisecond span to whole-second bounds matches the resolution the
    Citation_Formatter writes (floor start, ceil end), so a cited ``mm:ss`` range verifies against the
    Hit it was formatted from.
    """
    start_ms = h["start_ms"]
    end_ms = h["end_ms"]
    lo = (start_ms // 1000) * 1000
    hi = math.ceil(end_ms / 1000) * 1000
    return lo, hi


def classify_citations(
    citations: Sequence[Citation],
    *,
    project_id: str,
    session_hits: Sequence[SessionHit],
    assets: Optional[Mapping[str, ResolvedAsset]],
) -> list[CitationRecord]:
    """Classify a turn's Citations into :class:`CitationRecord`s (Req 2.5, 2.10).

    One record per distinct ``Citation.key``, in order of first appearance, at most
    :data:`MAX_RECORDS`. When ``assets is None`` the registry was unreadable, so every record is
    unverified with reason ``"registry_unavailable"``. Otherwise a Citation is verified when some
    :class:`SessionHit` ``sh`` satisfies all of:

    - ``sh.project_id == project_id``;
    - ``is_hit(sh.hit)``;
    - ``a = assets.get(sh.hit["asset_id"])`` exists and ``a.status != "removed"``;
    - ``normalize_name(a.display_name) == normalize_name(c.display_name)``;
    - ``lo, hi = whole_second_range(sh.hit)`` and ``lo <= c.start_ms`` and ``c.end_ms <= hi``;
    - a page citation (``c.page``) matches only a ``document`` asset, by the page its Hit's span encodes
      (instead of the range rule: a page range verifies on a hit on ANY of its pages), and a timecode
      citation only media.

    Among the qualifying Hits the one with the largest ``recency`` is chosen (Req 3.4) and supplies
    ``ref``, ``kind``, ``asset_display_name`` and ``hit_recency``. ``ref.frame_s3_key`` is the Hit's
    ``frame_s3_key`` only when its ``source_index == "shot"`` and the value is a non-empty str, else
    ``None``.
    """
    records: list[CitationRecord] = []
    seen_keys: set[str] = set()

    for citation in citations:
        key = citation.key
        if key in seen_keys:
            continue  # one record per distinct key; later occurrences classify identically
        seen_keys.add(key)

        records.append(_classify_one(citation, project_id=project_id,
                                     session_hits=session_hits, assets=assets))
        if len(records) >= MAX_RECORDS:
            break

    return records


def _classify_one(
    citation: Citation,
    *,
    project_id: str,
    session_hits: Sequence[SessionHit],
    assets: Optional[Mapping[str, ResolvedAsset]],
) -> CitationRecord:
    base = dict(
        key=citation.key,
        text=citation.text,
        display_name=citation.display_name,
        start_ms=citation.start_ms,
        end_ms=citation.end_ms,
        page=citation.page,
        page_end=citation.page_end,
    )

    if assets is None:
        return CitationRecord(**base, verified=False, reason="registry_unavailable")

    cited_name = normalize_name(citation.display_name)
    best: Optional[SessionHit] = None
    best_asset: Optional[ResolvedAsset] = None

    for sh in session_hits:
        if sh.project_id != project_id:
            continue
        if not is_hit(sh.hit):
            continue
        asset = assets.get(sh.hit["asset_id"])
        if asset is None or asset.status == _REMOVED_STATUS:
            continue
        if normalize_name(asset.display_name) != cited_name:
            continue
        # A page citation names a document; a timecode names media. A document hit's span is a page
        # locator, so a timecode "verified" against it would point at nothing a reader could find.
        if (citation.page is not None) != (asset.kind == "document"):
            continue
        if citation.page is not None:
            if not citation.page <= page_of_span(sh.hit["start_ms"]) <= (citation.page_end or citation.page):
                continue
        else:
            lo, hi = whole_second_range(sh.hit)
            if not (lo <= citation.start_ms and citation.end_ms <= hi):
                continue
        if best is None or sh.recency > best.recency:
            best = sh
            best_asset = asset

    if best is None or best_asset is None:
        return CitationRecord(**base, verified=False, reason="no_matching_hit")

    return CitationRecord(
        **base,
        verified=True,
        reason=None,
        ref=AssetReference(
            project_id=project_id,
            asset_id=best.hit["asset_id"],
            frame_s3_key=_frame_key(best.hit),
        ),
        kind=best_asset.kind,
        asset_display_name=best_asset.display_name,
        hit_recency=best.recency,
    )


def _is_int(value: Any) -> bool:
    """``int`` but not ``bool`` (``bool`` is an ``int`` subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _frame_key(hit: dict) -> Optional[str]:
    """``hit["frame_s3_key"]`` only for a shot Hit carrying a non-empty str, else ``None``."""
    if hit.get("source_index") != _SHOT_SOURCE:
        return None
    key = hit.get("frame_s3_key")
    if isinstance(key, str) and key:
        return key
    return None
