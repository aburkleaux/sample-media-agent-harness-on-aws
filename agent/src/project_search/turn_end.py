"""Turn-end orchestration for citation verification (design Component 4, Req 2.5-2.7, 2.10).

This module is the one place a turn's answer text, its Search_Ledger, the Session_Hit_Store and the
Project_Registry are brought together to classify the Citations the Main_Agent wrote. It owns no
policy of its own: it buffers the streamed answer, then at turn end gathers the delivered Hits,
resolves their assets once, and hands everything to :func:`verify.classify_citations`.

Three public shapes:

- :func:`note_answer_text` accumulates the answer as ``data`` chunks stream (called from ``main.py``).
- :func:`finish_turn` runs the seven-step orchestration on the turn-end worker thread. It is
  **synchronous and never raises** — it is driven from ``asyncio.to_thread`` in
  ``HarnessHookProvider._on_session_end`` before the ``session_end`` rules fire, so the Moments_Rule
  (a ``session_end`` rule) always sees the :class:`TurnOutcome`. A boto3/Lambda call on the event
  loop would stall ``_progress_poller`` and the keepalive, hence the thread offload.
- :func:`outcome_for` / :func:`release` read and drop the stored outcome; ``release`` also clears the
  per-turn Search_Ledger.

Both the answer buffer and the outcomes are bounded to 64 turns, matching ``ledger._MAX_TURNS``, so a
long-lived container never grows without bound.

**No URL ever reaches the wire.** :meth:`TurnOutcome.citations_wire` builds the CitationBlock from the
typed :class:`verify.CitationRecord` fields only — never from Hit text or a presigned URL — so the SPA
mints its own URLs (Component 5) and a stored block cannot leak or expire one.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from . import ledger
from .assets import AssetResolver, RegistryUnavailable, ResolvedAsset
from .citations import parse_citations
from .context import current_turn_context
from .session_hits import SessionHit, SessionHitStore, SessionHitStoreError
from .verify import MAX_RECORDS, CitationRecord, classify_citations

logger = logging.getLogger(__name__)

# Bound the answer buffers and the outcomes like the Search_Ledger (`ledger._MAX_TURNS`): a long-lived
# Main_Agent container handles many turns, and nothing else evicts these maps between turns.
_MAX_TURNS = 64

_lock = threading.Lock()
_answer_buffers: dict[str, list[str]] = {}
_outcomes: dict[str, "TurnOutcome"] = {}


# ---------------------------------------------------------------------------
# Answer buffering (producer side, main.py)
# ---------------------------------------------------------------------------
def note_answer_text(turn_id: str, chunk: str) -> None:
    """Append one streamed answer ``chunk`` to ``turn_id``'s buffer (every ``data`` string of a turn).

    No-op when ``turn_id`` is empty or ``chunk`` is not a non-empty str. Bounded to :data:`_MAX_TURNS`
    turns, evicting the oldest buffer first.
    """
    if not turn_id or not isinstance(chunk, str) or not chunk:
        return
    with _lock:
        if turn_id not in _answer_buffers and len(_answer_buffers) >= _MAX_TURNS:
            _answer_buffers.pop(next(iter(_answer_buffers)))  # evict oldest
        _answer_buffers.setdefault(turn_id, []).append(chunk)


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TurnOutcome:
    """The classified result of one turn, read by the wire builder and the Moments_Rule (Component 6)."""

    turn_id: str
    project_id: str
    citations: list[CitationRecord]
    session_hits: list[SessionHit]              # candidate Hits used above (Component 6 reads them)
    turn_hits: list[SessionHit]                 # this turn's Hits, in call and result order (Req 3.5)
    assets: Optional[dict[str, ResolvedAsset]]  # None when the registry was unreadable (Req 3.3)
    truncated: bool
    # The Moments tab's candidates (Req 3.5 as amended 2026-10-10): this turn's delivered Hits in call order,
    # then the overflow each call returned past the model's `k`. Never used to verify a Citation. None →
    # `turn_hits` (an outcome built before the field existed).
    tab_hits: Optional[list[SessionHit]] = None
    # This turn's distinct search terms, in call order (the Moments tab's search box starts from the first).
    queries: Optional[list[str]] = None
    # The Moments tab's typed query (its search bar): {"type": "text", "q"} or {"type": "image", "images": [{
    # assetId | frameKey (+ sourceAssetId, atMs), label}], "match": "any"}. None when the turn searched nothing.
    query_ref: Optional[dict] = None

    def citations_wire(self) -> dict:
        """The CitationBlock (Req 2.6, 2.12), built from typed fields only — never a URL."""
        return {
            "v": 1,
            "project_id": self.project_id,
            "truncated": self.truncated,
            "items": [_record_wire(record) for record in self.citations],
        }


def _record_wire(record: CitationRecord) -> dict:
    """One CitationBlock item. Verified-only fields appear only on a verified record; a URL never does."""
    item: dict = {
        "key": record.key,
        "text": record.text,
        "display_name": record.display_name,
        "start_ms": record.start_ms,
        "end_ms": record.end_ms,
        "verified": record.verified,
    }
    if record.page is not None:
        item["page"] = record.page
        if record.page_end is not None and record.page_end != record.page:
            item["page_end"] = record.page_end
    if record.verified:
        ref = record.ref
        item["ref"] = (
            {
                "project_id": ref.project_id,
                "asset_id": ref.asset_id,
                "frame_s3_key": ref.frame_s3_key,
            }
            if ref is not None
            else None
        )
        item["kind"] = record.kind
        item["asset_display_name"] = record.asset_display_name
    else:
        item["reason"] = record.reason
    return item


# ---------------------------------------------------------------------------
# Turn-end orchestration
# ---------------------------------------------------------------------------
def finish_turn(*, store: SessionHitStore, resolver: AssetResolver) -> Optional[TurnOutcome]:
    """Classify this turn's Citations against its delivered Hits (Req 2.5). Synchronous; never raises.

    Returns ``None`` when the turn had no Projects context (nothing to verify); otherwise the
    :class:`TurnOutcome`, also stored under ``turn_id`` for :func:`outcome_for`.
    """
    try:
        # 1. Projects context. No context (or an empty turn id) means no citation block is emitted and
        #    the answer renders as before. prepare_turn sets this contextvar before the producer task,
        #    so the to_thread worker inherits it.
        tc = current_turn_context.get()
        if tc is None or not tc.turn_id:
            return None
        turn_id = tc.turn_id
        pid = tc.active_project.id

        # 2. This turn's ok ledger entries under the Active_Project.
        entries = [
            e
            for e in ledger.results_for(turn_id)
            if getattr(e, "ok", False) and e.project_id == pid
        ]

        session_id = _current_session_id_safe()

        # 3. Persist this turn's Hits. A store failure only costs later turns; this turn still uses them
        #    from memory, so log and continue.
        if session_id is not None:
            try:
                store.append(session_id, turn_id, entries)
            except SessionHitStoreError as exc:
                logger.warning(
                    "Session_Hit_Store append failed session=%s turn=%s: %s",
                    session_id, turn_id, type(exc).__name__,
                )

        # 4. Candidate Hits: this turn's Hits unioned with the session's stored Hits, deduped on
        #    (turn_id, tool_use_id, index). A read failure leaves only this turn's Hits, so earlier-turn
        #    Citations classify as unverified (no_matching_hit).
        turn_hits = _build_turn_hits(entries)
        stored_hits: list[SessionHit] = []
        if session_id is not None:
            try:
                stored_hits = store.hits(session_id, pid)
            except SessionHitStoreError as exc:
                logger.warning(
                    "Session_Hit_Store read failed session=%s turn=%s: %s",
                    session_id, turn_id, type(exc).__name__,
                )
                stored_hits = []
        session_hits = _union_hits(stored_hits, turn_hits)
        tab_hits = turn_hits + _build_overflow_hits(entries, turn_hits)

        # 5. Parse the buffered answer. With no Citations and no Hits there is nothing to resolve.
        citations = parse_citations(_answer_text(turn_id))

        # 6. Resolve the distinct assets once (reused by the Moments_Rule). RegistryUnavailable → None,
        #    which makes every record unverified with reason "registry_unavailable".
        assets: Optional[dict[str, ResolvedAsset]] = {}
        if citations or session_hits or tab_hits:
            try:
                assets = dict(resolver.resolve(pid, list(dict.fromkeys(
                    [*_distinct_asset_ids([*session_hits, *tab_hits]), *_query_asset_ids(entries, pid)]))))
            except RegistryUnavailable:
                assets = None

        # 7. Classify, then store the outcome bounded to 64 turns.
        records = classify_citations(
            citations, project_id=pid, session_hits=session_hits, assets=assets
        )
        truncated = len({c.key for c in citations}) > MAX_RECORDS

        outcome = TurnOutcome(
            turn_id=turn_id,
            project_id=pid,
            citations=records,
            session_hits=session_hits,
            turn_hits=turn_hits,
            assets=assets,
            truncated=truncated,
            tab_hits=tab_hits,
            queries=list(dict.fromkeys(
                t for t in (getattr(e, "search_term", "") for e in entries) if isinstance(t, str) and t.strip())),
            query_ref=query_ref_for(entries, pid, session_hits, assets),
        )
        _store_outcome(turn_id, outcome)
        return outcome
    except Exception:  # noqa: BLE001 — finish_turn must never raise into the hook path
        logger.exception("turn_end.finish_turn failed")
        return None


def outcome_for(turn_id: str) -> Optional[TurnOutcome]:
    """The stored :class:`TurnOutcome` for ``turn_id``, or ``None``."""
    with _lock:
        return _outcomes.get(turn_id)


def release(turn_id: str) -> None:
    """Drop the answer buffer and the stored outcome, and clear the Search_Ledger for ``turn_id``."""
    with _lock:
        _answer_buffers.pop(turn_id, None)
        _outcomes.pop(turn_id, None)
    ledger.clear_turn(turn_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _build_turn_hits(entries: Sequence) -> list[SessionHit]:
    """This turn's delivered Hits as :class:`SessionHit`s, in call and result order (Req 3.5).

    ``turn_seq`` is epoch ms now, so if the store append failed these in-memory Hits still sort as the
    most recent (earlier turns were appended at smaller epoch ms). ``entries`` is already filtered to
    ok entries under the Active_Project.
    """
    turn_seq = time.time_ns() // 1_000_000
    out: list[SessionHit] = []
    for call_index, entry in enumerate(entries):
        if not getattr(entry, "ok", False):
            continue
        for index, hit in enumerate(entry.hits):
            out.append(
                SessionHit(
                    hit=hit,
                    project_id=entry.project_id,
                    turn_id=entry.turn_id,
                    tool_use_id=entry.tool_use_id,
                    index=index,
                    recency=(turn_seq, call_index, -index),
                )
            )
    return out


def _build_overflow_hits(entries: Sequence, turn_hits: Sequence[SessionHit]) -> list[SessionHit]:
    """Each call's ``extra_hits`` (ranked after the model's ``k``) as :class:`SessionHit`s, in call order.

    Indices continue after the call's delivered Hits, so an overflow Hit never shares an identity with a
    delivered one. Recency sorts them below every delivered Hit of the turn; it is never used to verify.
    """
    turn_seq = turn_hits[0].recency[0] if turn_hits else time.time_ns() // 1_000_000
    out: list[SessionHit] = []
    for call_index, entry in enumerate(entries):
        extra = getattr(entry, "extra_hits", None) or []
        base = len(entry.hits)
        for offset, hit in enumerate(extra):
            index = base + offset
            out.append(
                SessionHit(
                    hit=hit,
                    project_id=entry.project_id,
                    turn_id=entry.turn_id,
                    tool_use_id=entry.tool_use_id,
                    index=index,
                    recency=(turn_seq, call_index, -index),
                )
            )
    return out


def _union_hits(stored: Sequence[SessionHit], turn_hits: Sequence[SessionHit]) -> list[SessionHit]:
    """``stored`` ∪ ``turn_hits`` deduped on ``(turn_id, tool_use_id, index)``, most recent first.

    Stored Hits win a duplicate (they carry the authoritative append-time recency); the in-memory
    copies only fill in when the store write or read failed.
    """
    out: list[SessionHit] = list(stored)
    seen = {(sh.turn_id, sh.tool_use_id, sh.index) for sh in stored}
    for sh in turn_hits:
        dedup = (sh.turn_id, sh.tool_use_id, sh.index)
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append(sh)
    out.sort(key=lambda sh: sh.recency, reverse=True)
    return out


def _distinct_asset_ids(session_hits: Sequence[SessionHit]) -> list[str]:
    """The distinct, non-empty ``asset_id``s of ``session_hits``, in first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for sh in session_hits:
        asset_id = sh.hit.get("asset_id") if isinstance(sh.hit, dict) else None
        if isinstance(asset_id, str) and asset_id and asset_id not in seen:
            seen.add(asset_id)
            ordered.append(asset_id)
    return ordered


QUERY_IMAGES_MAX = 3  # the Moments tab searches at most this many images at once ("match any")
QUERY_LABEL_MAX = 120


def _query_image_ref(image: object, pid: str) -> Optional[dict]:
    """``{"assetId"}`` or ``{"frameKey"}`` for one ledger ``query_image``: a rendition key names its image asset."""
    if not isinstance(image, dict):
        return None
    asset_id = image.get("assetId")
    if isinstance(asset_id, str) and asset_id:
        return {"assetId": asset_id}
    uri = image.get("s3uri")
    if not isinstance(uri, str) or not uri.startswith("s3://") or "/" not in uri[5:]:
        return None
    key = uri[5:].split("/", 1)[1]
    parts = key.split("/")
    if len(parts) == 4 and parts[0] == "projects" and parts[1] == pid and parts[3] == "rendition.jpg":
        return {"assetId": parts[2]}
    return {"frameKey": key}


def _query_asset_ids(entries: Sequence, pid: str) -> list[str]:
    """The image asset ids this turn searched with, so their names resolve with the hits'."""
    out: list[str] = []
    for e in entries:
        ref = _query_image_ref(getattr(e, "query_image", None), pid)
        if ref and ref.get("assetId") and ref["assetId"] not in out:
            out.append(ref["assetId"])
    return out


def _mmss(ms: object) -> str:
    s = max(0, int(ms)) // 1000 if isinstance(ms, (int, float)) else 0
    return f"{s // 60:02d}:{s % 60:02d}"


def query_ref_for(entries: Sequence, pid: str, session_hits: Sequence[SessionHit],
                  assets: Optional[dict]) -> Optional[dict]:
    """The Moments tab's typed query: the kind of the turn's FIRST query-bearing search decides it.

    Text → ``{"type": "text", "q"}``. Image → up to :data:`QUERY_IMAGES_MAX` distinct query images in call
    order, each ``{assetId, label}`` or ``{frameKey, sourceAssetId?, atMs?, label}`` (a frame is labelled from
    the session hit it came from), with ``"match": "any"``. ``None`` when the turn searched nothing.
    """
    names = {aid: getattr(a, "display_name", "") for aid, a in (assets or {}).items()}
    by_frame: dict[str, dict] = {}
    for sh in session_hits:
        hit = sh.hit if isinstance(sh.hit, dict) else {}
        key = hit.get("frame_s3_key")
        if isinstance(key, str) and key and key not in by_frame:
            by_frame[key] = hit
    images: list[dict] = []
    for e in entries:
        term = getattr(e, "search_term", "")
        ref = _query_image_ref(getattr(e, "query_image", None), pid)
        if not images and isinstance(term, str) and term.strip():
            return {"type": "text", "q": term.strip()[:500]}
        if ref is None or any(i.get("assetId", i.get("frameKey")) == ref.get("assetId", ref.get("frameKey"))
                              for i in images):
            continue
        if "assetId" in ref:
            ref["label"] = (names.get(ref["assetId"]) or "an image")[:QUERY_LABEL_MAX]
        else:
            hit = by_frame.get(ref["frameKey"]) or {}
            source = hit.get("asset_id") if isinstance(hit.get("asset_id"), str) else None
            if source:
                ref["sourceAssetId"] = source
            if isinstance(hit.get("start_ms"), int):
                ref["atMs"] = hit["start_ms"]
            name = names.get(source or "") or ""
            ref["label"] = (f"Frame {_mmss(hit.get('start_ms'))}" + (f" · {name}" if name else ""))[:QUERY_LABEL_MAX]
        images.append(ref)
        if len(images) >= QUERY_IMAGES_MAX:
            break
    return {"type": "image", "images": images, "match": "any"} if images else None


def _answer_text(turn_id: str) -> str:
    """The buffered answer for ``turn_id`` as one string."""
    with _lock:
        return "".join(_answer_buffers.get(turn_id, ()))


def _store_outcome(turn_id: str, outcome: "TurnOutcome") -> None:
    """Store ``outcome`` under ``turn_id``, bounded to :data:`_MAX_TURNS`, evicting the oldest first."""
    with _lock:
        if turn_id not in _outcomes and len(_outcomes) >= _MAX_TURNS:
            _outcomes.pop(next(iter(_outcomes)))
        _outcomes[turn_id] = outcome


def _current_session_id_safe() -> Optional[str]:
    """The bound session id, or ``None`` when unbound (``finish_turn`` must not raise)."""
    try:
        from jobs.context import current_session_id

        return current_session_id()
    except Exception:  # noqa: BLE001 — unbound session id is non-fatal; this turn still verifies
        return None
