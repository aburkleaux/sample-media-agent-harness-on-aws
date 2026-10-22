"""Property test for Moment eligibility and field sourcing (design Property 14).

Drives `render.moments.select_moments` with hand-built, `TurnOutcome`-shaped inputs whose
`session_hits`/`turn_hits` mix well-formed Hits with the non-Hit shapes `verify.is_hit` must
reject (``start_ms == end_ms``, a bool ``final_score``, a str ``start_ms``, a missing
``asset_id``), carry `project_id`s from several Projects, and whose resolved `assets` map covers
every Ingestion_Status, omits some ids, or is ``None``.

The expected matched text, source index and frame key are recomputed independently in this file
(not by calling the module under test) so the assertions pin the design wording rather than the
implementation.

**Validates: Requirements 3.1, 3.2, 3.3, 3.9, 3.19**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.session_hits import SessionHit  # noqa: E402
from project_search.turn_end import TurnOutcome  # noqa: E402
from project_search.verify import AssetReference, CitationRecord, is_hit  # noqa: E402
from render.moments import (  # noqa: E402
    MOMENT_TEXT_MAX,
    MomentSelection,
    eligible,
    select_moments,
)

# The active Project of the turn, and two other Projects a Hit may have been returned under.
ACTIVE_PID = "pX"
OTHER_PIDS = ["pY", "pZ"]
ALL_PIDS = [ACTIVE_PID, *OTHER_PIDS]

# A small id pool so some ids resolve in `assets` and some are deliberately absent.
ASSET_IDS = ["a1", "a2", "a3", "a4", "a5"]
# Every Ingestion_Status; only "removed" is excluded by eligibility (Req 3.2).
STATUSES = ["indexed", "removed", "failed", "queued", "analyzing", "indexing"]

_ELLIPSIS = "\u2026"
_SHOT = "shot"


# ---------------------------------------------------------------------------
# Independent re-derivations of the design wording (never the module under test).
# ---------------------------------------------------------------------------
def expected_matched_text(hit: dict) -> tuple[str, bool]:
    """Design Req 3.9: "" for a shot Hit, else text, else serialized named_entities, cut to 500."""
    if hit.get("source_index") == _SHOT:
        raw = ""
    else:
        text = hit.get("text")
        if isinstance(text, str) and text:
            raw = text
        elif hit.get("named_entities") is not None:
            ne = hit["named_entities"]
            raw = ne if isinstance(ne, str) else json.dumps(ne, ensure_ascii=False)
        else:
            raw = ""
    if len(raw) > MOMENT_TEXT_MAX:
        return raw[:MOMENT_TEXT_MAX] + _ELLIPSIS, True
    return raw, False


def expected_source_index(hit: dict):
    value = hit.get("source_index")
    return value if isinstance(value, str) else None


def expected_frame_key(hit: dict):
    if hit.get("source_index") != _SHOT:
        return None
    key = hit.get("frame_s3_key")
    return key if isinstance(key, str) and key else None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_names = st.text(min_size=0, max_size=12)
_source_index_vals = st.sampled_from([_SHOT, "transcript", "caption", "ocr", None, "", 7])
_frame_vals = st.one_of(st.none(), st.just(""), st.text(min_size=1, max_size=8))
# A text value that is sometimes longer than MOMENT_TEXT_MAX to exercise truncation.
_text_vals = st.one_of(
    st.none(),
    st.just(""),
    st.text(min_size=1, max_size=12),
    st.text(min_size=MOMENT_TEXT_MAX + 1, max_size=MOMENT_TEXT_MAX + 40),
)
_named_entities_vals = st.one_of(
    st.none(),
    st.text(max_size=12),
    st.lists(st.text(max_size=6), max_size=4),
    st.dictionaries(st.text(max_size=4), st.text(max_size=6), max_size=3),
)


@st.composite
def well_formed_hit(draw) -> dict:
    start = draw(st.integers(min_value=0, max_value=100_000))
    end = start + draw(st.integers(min_value=1, max_value=5_000))
    score = draw(
        st.one_of(
            st.integers(min_value=0, max_value=1000),
            st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False),
        )
    )
    hit = {
        "asset_id": draw(st.sampled_from(ASSET_IDS)),
        "start_ms": start,
        "end_ms": end,
        "final_score": score,
        # The Hit's own title must never reach a Moment — kept disjoint from asset names.
        "title": "title-" + draw(_names),
    }
    si = draw(_source_index_vals)
    if si is not None:
        hit["source_index"] = si
    frame = draw(_frame_vals)
    if frame is not None:
        hit["frame_s3_key"] = frame
    text = draw(_text_vals)
    if text is not None:
        hit["text"] = text
    ne = draw(_named_entities_vals)
    if ne is not None:
        hit["named_entities"] = ne
    return hit


@st.composite
def non_hit(draw) -> dict:
    """One of the non-Hit shapes `is_hit` must reject (design Property 14)."""
    kind = draw(st.integers(min_value=0, max_value=3))
    base = {"asset_id": draw(st.sampled_from(ASSET_IDS)), "final_score": 1.0,
            "start_ms": 10, "end_ms": 20, "title": "title-x"}
    if kind == 0:  # start_ms == end_ms (zero-length span)
        base["end_ms"] = base["start_ms"]
    elif kind == 1:  # bool final_score (bool is an int subclass, excluded)
        base["final_score"] = draw(st.booleans())
    elif kind == 2:  # str start_ms
        base["start_ms"] = "10"
    else:  # missing asset_id
        base.pop("asset_id")
    return base


hit_dict = st.one_of(well_formed_hit(), non_hit())


@st.composite
def session_hits(draw, *, prefix: str):
    """A list of SessionHits with unique recencies and Project ids from several Projects."""
    dicts = draw(st.lists(hit_dict, min_size=0, max_size=10))
    out = []
    for i, hit in enumerate(dicts):
        out.append(
            SessionHit(
                hit=hit,
                project_id=draw(st.sampled_from(ALL_PIDS)),
                turn_id=f"{prefix}-t{i}",
                tool_use_id=f"{prefix}-tu{i}",
                index=i,
                recency=(1, 0, -(i + 1)) if prefix == "s" else (2, 0, -(i + 1)),
            )
        )
    return out


@st.composite
def assets_map(draw):
    """None (registry unreadable), or a map over a subset of the id pool, every status."""
    if draw(st.booleans()):
        return None
    chosen = draw(st.lists(st.sampled_from(ASSET_IDS), unique=True, max_size=len(ASSET_IDS)))
    return {
        aid: ResolvedAsset(
            asset_id=aid,
            kind=draw(st.sampled_from(["video", "image", "audio"])),
            display_name="name-" + aid,
            status=draw(st.sampled_from(STATUSES)),
        )
        for aid in chosen
    }


@st.composite
def outcomes(draw) -> TurnOutcome:
    s_hits = draw(session_hits(prefix="s"))
    t_hits = draw(session_hits(prefix="t"))
    assets = draw(assets_map())

    # Optionally drive the cited path: verified records pointing at some session_hits
    # (matched by recency + asset_id, exactly as classify_citations would have left them).
    citations: list[CitationRecord] = []
    if s_hits and draw(st.booleans()):
        picks = draw(st.lists(st.sampled_from(s_hits), max_size=4))
        for n, sh in enumerate(picks):
            aid = sh.hit.get("asset_id") if isinstance(sh.hit, dict) else None
            citations.append(
                CitationRecord(
                    key=f"c{n}",
                    text=f"cited-{n}",
                    display_name="name-" + (aid or "a1"),
                    start_ms=0,
                    end_ms=1000,
                    verified=True,
                    reason=None,
                    ref=AssetReference(project_id=ACTIVE_PID, asset_id=aid or "a1"),
                    kind=None,
                    asset_display_name=None,
                    hit_recency=sh.recency,
                )
            )

    return TurnOutcome(
        turn_id="turn",
        project_id=ACTIVE_PID,
        citations=citations,
        session_hits=s_hits,
        turn_hits=t_hits,
        assets=assets,
        truncated=False,
    )


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------
@settings(max_examples=400)
@given(outcomes())
def test_property14_moment_eligibility_and_field_sourcing(outcome: TurnOutcome):
    selection = select_moments(outcome)

    # assets is None -> no selection (Req 3.3).
    if outcome.assets is None:
        assert selection is None
        return

    if selection is None:
        return  # no eligible Moments this turn; nothing to source-check (Req 3.6)
    assert isinstance(selection, MomentSelection)
    assert selection.items, "a returned selection is always non-empty"

    all_hits = list(outcome.session_hits) + list(outcome.turn_hits)

    for moment in selection.items:
        # ref is (project_id, asset_id, frame_s3_key); project is always the Active_Project.
        assert isinstance(moment.ref, AssetReference)
        assert moment.ref.project_id == outcome.project_id
        assert moment.key == (moment.ref.asset_id, moment.start_ms, moment.end_ms)

        # The Moment must equal, field for field, some eligible well-formed Hit of the right
        # Project — never a non-Hit, never a Hit of another Project, never a removed/absent asset.
        matched = False
        for sh in all_hits:
            hit = sh.hit
            if not is_hit(hit):
                continue
            if sh.project_id != moment.ref.project_id:
                continue
            if hit["asset_id"] != moment.ref.asset_id:
                continue
            if hit["start_ms"] != moment.start_ms or hit["end_ms"] != moment.end_ms:
                continue
            asset = eligible(sh, outcome.project_id, outcome.assets)
            if asset is None:
                continue
            # kind and display_name come from the registry, never the Hit's title.
            if moment.kind != asset.kind or moment.display_name != asset.display_name:
                continue
            if moment.final_score != float(hit["final_score"]):
                continue
            if moment.source_index != expected_source_index(hit):
                continue
            if moment.ref.frame_s3_key != expected_frame_key(hit):
                continue
            if (moment.text, moment.text_truncated) != expected_matched_text(hit):
                continue
            matched = True
            break

        assert matched, f"Moment not sourced from any eligible Hit: {moment!r}"

        # Field sourcing never leaks the Hit's title into the Display_Name.
        assert not moment.display_name.startswith("title-")
        # The frame key is present only for a shot Hit.
        if moment.ref.frame_s3_key is not None:
            # whichever eligible Hit it came from, the key implies a shot source.
            assert any(
                is_hit(sh.hit)
                and sh.hit.get("source_index") == _SHOT
                and sh.hit.get("frame_s3_key") == moment.ref.frame_s3_key
                for sh in all_hits
            )


# ---------------------------------------------------------------------------
# Concrete examples pinning the specific clauses of Property 14.
# ---------------------------------------------------------------------------
def _sh(hit, *, project_id=ACTIVE_PID, i=0):
    return SessionHit(hit=hit, project_id=project_id, turn_id=f"t{i}",
                      tool_use_id=f"tu{i}", index=i, recency=(1, 0, -(i + 1)))


def _outcome(*, session=(), turn=(), assets, citations=()):
    return TurnOutcome(
        turn_id="turn",
        project_id=ACTIVE_PID,
        citations=list(citations),
        session_hits=list(session),
        turn_hits=list(turn),
        assets=assets,
        truncated=False,
    )


def test_assets_none_returns_none():
    hit = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.9}
    assert select_moments(_outcome(turn=[_sh(hit)], assets=None)) is None


def test_shot_hit_carries_frame_key_and_empty_text():
    hit = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5,
           "source_index": "shot", "frame_s3_key": "frames/a1/0.jpg",
           "text": "ignored for a shot", "title": "title-old"}
    assets = {"a1": ResolvedAsset("a1", "image", "name-a1", "indexed")}
    sel = select_moments(_outcome(turn=[_sh(hit)], assets=assets))
    assert sel is not None and len(sel.items) == 1
    m = sel.items[0]
    assert m.ref.frame_s3_key == "frames/a1/0.jpg"
    assert (m.text, m.text_truncated) == ("", False)
    assert m.kind == "image" and m.display_name == "name-a1"
    assert m.source_index == "shot"


def test_non_shot_hit_has_no_frame_key_and_uses_text():
    hit = {"asset_id": "a2", "start_ms": 500, "end_ms": 2500, "final_score": 7,
           "source_index": "transcript", "frame_s3_key": "frames/a2/0.jpg",
           "text": "a line of dialogue", "title": "title-x"}
    assets = {"a2": ResolvedAsset("a2", "video", "name-a2", "failed")}  # failed stays eligible
    sel = select_moments(_outcome(turn=[_sh(hit)], assets=assets))
    assert sel is not None and len(sel.items) == 1
    m = sel.items[0]
    assert m.ref.frame_s3_key is None
    assert (m.text, m.text_truncated) == ("a line of dialogue", False)
    assert m.final_score == 7.0 and isinstance(m.final_score, float)
    assert m.source_index == "transcript"


def test_removed_asset_is_excluded():
    hit = {"asset_id": "a3", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    assets = {"a3": ResolvedAsset("a3", "video", "name-a3", "removed")}
    assert select_moments(_outcome(turn=[_sh(hit)], assets=assets)) is None


def test_non_hit_entries_never_become_moments():
    non_hits = [
        {"asset_id": "a1", "start_ms": 5, "end_ms": 5, "final_score": 0.5},   # zero span
        {"asset_id": "a1", "start_ms": 0, "end_ms": 10, "final_score": True},  # bool score
        {"asset_id": "a1", "start_ms": "0", "end_ms": 10, "final_score": 0.5},  # str start
        {"start_ms": 0, "end_ms": 10, "final_score": 0.5},                     # missing id
    ]
    assets = {"a1": ResolvedAsset("a1", "video", "name-a1", "indexed")}
    turn = [_sh(h, i=i) for i, h in enumerate(non_hits)]
    assert select_moments(_outcome(turn=turn, assets=assets)) is None


def test_hit_of_another_project_is_excluded():
    hit = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    assets = {"a1": ResolvedAsset("a1", "video", "name-a1", "indexed")}
    # Returned under pY, but the Active_Project is pX.
    out = _outcome(turn=[_sh(hit, project_id="pY")], assets=assets)
    assert select_moments(out) is None


def test_long_text_is_truncated_with_ellipsis():
    long_text = "x" * (MOMENT_TEXT_MAX + 10)
    hit = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5,
           "text": long_text}
    assets = {"a1": ResolvedAsset("a1", "video", "name-a1", "indexed")}
    sel = select_moments(_outcome(turn=[_sh(hit)], assets=assets))
    assert sel is not None
    m = sel.items[0]
    assert m.text_truncated is True
    assert m.text == long_text[:MOMENT_TEXT_MAX] + _ELLIPSIS


# ===========================================================================
# Property 15 (task 7.3): Moments ordering, dedup key, cap and basis.
#
# Property 14 above pins *which* Hits become Moments and *how each field* is
# sourced. Property 15 pins the orthogonal concerns: the BASIS (`cited` vs
# `searched`), the ORDER of the cards, the DEDUP key `(asset_id, start_ms,
# end_ms)`, the MOMENTS_MAX CAP with its `truncated` flag, and the per-card
# `citation` sourcing. As amended 2026-10-10 (Req 3.4/3.5): cited cards come
# first, then every other eligible Hit of the turn's searches fills the rest.
#
# The expected selection is re-derived independently here (never by calling
# `select_moments`), so the assertions pin the design wording of Property 15.
# Eligibility itself is Property 14's concern, so the module's `eligible()`
# stands in for it here rather than being re-derived a second time.
#
# **Validates: Requirements 3.4, 3.5, 3.6**
# ===========================================================================
from render.moments import MOMENTS_MAX  # noqa: E402


def _find_session_hit(session_hits, recency, asset_id):
    """Independent twin of `moments._hit_for_record`: first SessionHit whose
    `recency` and `asset_id` match the chosen Hit (Req 3.4)."""
    for sh in session_hits:
        if (
            sh.recency == recency
            and isinstance(sh.hit, dict)
            and sh.hit.get("asset_id") == asset_id
        ):
            return sh
    return None


def expected_cited(outcome):
    """Re-derive the cited selection (design Property 15, Req 3.4): verified
    records in order, mapped to their SessionHit, kept when eligible, first
    occurrence per key, cut to 50. Returns ([(key, citation_text)], truncated)."""
    assets = outcome.assets
    items: list = []
    seen: set = set()
    truncated = False
    for record in outcome.citations:
        if not getattr(record, "verified", False):
            continue
        ref = getattr(record, "ref", None)
        recency = getattr(record, "hit_recency", None)
        if ref is None or recency is None:
            continue
        sh = _find_session_hit(outcome.session_hits, recency, ref.asset_id)
        if sh is None:
            continue
        if eligible(sh, outcome.project_id, assets) is None:
            continue
        key = (sh.hit["asset_id"], sh.hit["start_ms"], sh.hit["end_ms"])
        if key in seen:
            continue
        seen.add(key)
        if len(items) >= MOMENTS_MAX:
            truncated = True
            continue
        items.append((key, getattr(record, "text", None)))
    return items, truncated


def expected_searched(outcome, taken=frozenset(), limit=MOMENTS_MAX):
    """Re-derive the searched fill (design Property 15, Req 3.5 as amended): the
    tab hits (turn_hits when tab_hits is None) in order, kept when eligible, first
    occurrence per key, skipping keys already cited, cut to `limit`. Returns
    ([key], truncated)."""
    assets = outcome.assets
    keys: list = []
    seen: set = set(taken)
    truncated = False
    hits = outcome.tab_hits if outcome.tab_hits is not None else outcome.turn_hits
    for sh in hits:
        if eligible(sh, outcome.project_id, assets) is None:
            continue
        key = (sh.hit["asset_id"], sh.hit["start_ms"], sh.hit["end_ms"])
        if key in seen:
            continue
        seen.add(key)
        if len(keys) >= limit:
            truncated = True
            continue
        keys.append(key)
    return keys, truncated


@settings(max_examples=400)
@given(outcomes())
def test_property15_moments_ordering_dedup_cap_and_basis(outcome: TurnOutcome):
    selection = select_moments(outcome)

    # assets is None -> no selection (Req 3.3), already pinned by Property 14; here it
    # only guards the re-derivation below.
    if outcome.assets is None:
        assert selection is None
        return

    has_verified = any(getattr(r, "verified", False) for r in outcome.citations)
    cited_items, cited_truncated = expected_cited(outcome) if has_verified else ([], False)
    cited_keys = [key for key, _ in cited_items]
    rest_keys, rest_truncated = expected_searched(
        outcome, taken=frozenset(cited_keys), limit=MOMENTS_MAX - len(cited_keys))
    exp_keys = cited_keys + rest_keys
    exp_truncated = cited_truncated or rest_truncated
    exp_basis = "cited" if cited_keys else "searched"

    # None exactly when no card qualifies on either side (Req 3.6).
    if not exp_keys:
        assert selection is None
        return

    assert selection is not None
    assert isinstance(selection, MomentSelection)
    assert selection.project_id == outcome.project_id
    assert selection.basis == exp_basis

    # Ordering + dedup: cited cards first (first-citation order), then the searched
    # fill; no repeated (asset_id, start_ms, end_ms) key.
    got_keys = [m.key for m in selection.items]
    assert got_keys == exp_keys
    assert len(got_keys) == len(set(got_keys))

    # Cap: never more than MOMENTS_MAX cards; `truncated` exactly when more qualified.
    assert len(selection.items) <= MOMENTS_MAX
    assert selection.truncated == exp_truncated

    # Each cited card carries the text of the first verified record mapping to it;
    # every searched card is a Hit of THIS turn's searches and carries no citation.
    exp_citation = dict(cited_items)
    turn_keys = {
        (sh.hit["asset_id"], sh.hit["start_ms"], sh.hit["end_ms"])
        for sh in outcome.turn_hits
        if is_hit(sh.hit)
    }
    for moment in selection.items[: len(cited_keys)]:
        assert moment.citation == exp_citation[moment.key]
    for moment in selection.items[len(cited_keys):]:
        assert moment.key in turn_keys
        assert moment.citation is None


# ---------------------------------------------------------------------------
# Concrete examples pinning the specific clauses of Property 15.
# ---------------------------------------------------------------------------
def _cite(text, recency, asset_id, *, verified=True, n=0):
    return CitationRecord(
        key=f"c{n}",
        text=text,
        display_name="name-" + asset_id,
        start_ms=0,
        end_ms=1000,
        verified=verified,
        reason=None,
        ref=AssetReference(project_id=ACTIVE_PID, asset_id=asset_id),
        kind=None,
        asset_display_name=None,
        hit_recency=recency,
    )


def test_cited_basis_orders_by_first_citation_and_dedups():
    h1 = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    h2 = {"asset_id": "a2", "start_ms": 0, "end_ms": 2000, "final_score": 0.6}
    session = [_sh(h1, i=0), _sh(h2, i=1)]
    assets = {
        "a1": ResolvedAsset("a1", "video", "name-a1", "indexed"),
        "a2": ResolvedAsset("a2", "video", "name-a2", "indexed"),
    }
    # Cite h2 first, then h1, then h2 again: order is [h2, h1]; the duplicate h2
    # record is dropped and h2's citation stays the first one.
    cites = [
        _cite("cite-h2-first", session[1].recency, "a2", n=0),
        _cite("cite-h1", session[0].recency, "a1", n=1),
        _cite("cite-h2-second", session[1].recency, "a2", n=2),
    ]
    sel = select_moments(_outcome(session=session, assets=assets, citations=cites))
    assert sel is not None and sel.basis == "cited"
    assert [m.key for m in sel.items] == [("a2", 0, 2000), ("a1", 0, 1000)]
    assert sel.items[0].citation == "cite-h2-first"
    assert sel.items[1].citation == "cite-h1"


def test_cited_moment_may_come_from_an_earlier_turn_session_hit():
    # The verified record maps to a Hit present only in session_hits (an earlier
    # turn), not in turn_hits — the cited path still finds it (Req 3.4).
    earlier = {"asset_id": "a9", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    session = [_sh(earlier, i=0)]
    assets = {"a9": ResolvedAsset("a9", "video", "name-a9", "indexed")}
    cites = [_cite("from an earlier turn", session[0].recency, "a9")]
    sel = select_moments(_outcome(session=session, turn=[], assets=assets, citations=cites))
    assert sel is not None and sel.basis == "cited"
    assert [m.key for m in sel.items] == [("a9", 0, 1000)]
    assert sel.items[0].citation == "from an earlier turn"


def test_verified_record_with_no_eligible_hit_falls_through_to_searched():
    # A verified citation points at a removed (ineligible) asset, while turn_hits
    # DO yield an eligible Moment. As amended 2026-10-10 the tab still shows the
    # turn's searched Moments (it used to show nothing).
    cited_hit = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    session = [_sh(cited_hit, i=0)]
    turn = [_sh({"asset_id": "a2", "start_ms": 0, "end_ms": 2000, "final_score": 0.7}, i=0)]
    assets = {
        "a1": ResolvedAsset("a1", "video", "name-a1", "removed"),
        "a2": ResolvedAsset("a2", "video", "name-a2", "indexed"),
    }
    cites = [_cite("c", session[0].recency, "a1")]
    sel = select_moments(_outcome(session=session, turn=turn, assets=assets, citations=cites))
    assert sel is not None and sel.basis == "searched"
    assert [m.key for m in sel.items] == [("a2", 0, 2000)]


def test_cited_first_then_every_other_searched_hit():
    # The amended rule: the cited card leads (with its citation), the turn's other
    # Hits follow in call order, and the cited Hit is not repeated.
    h1 = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    h2 = {"asset_id": "a2", "start_ms": 0, "end_ms": 2000, "final_score": 0.6}
    h3 = {"asset_id": "a3", "start_ms": 0, "end_ms": 3000, "final_score": 0.7}
    turn = [_sh(h1, i=0), _sh(h2, i=1), _sh(h3, i=2)]
    assets = {a: ResolvedAsset(a, "video", "name-" + a, "indexed") for a in ("a1", "a2", "a3")}
    cites = [_cite("the second one", turn[1].recency, "a2")]
    sel = select_moments(_outcome(session=turn, turn=turn, assets=assets, citations=cites))
    assert sel is not None and sel.basis == "cited"
    assert [m.key for m in sel.items] == [("a2", 0, 2000), ("a1", 0, 1000), ("a3", 0, 3000)]
    assert [m.citation for m in sel.items] == ["the second one", None, None]


def test_overflow_tab_hits_follow_delivered_hits():
    # tab_hits = delivered Hits then overflow: overflow cards render after every
    # delivered one and are never cited.
    import dataclasses
    d = [_sh({"asset_id": "d1", "start_ms": 0, "end_ms": 1, "final_score": 0.9}, i=0)]
    o = [_sh({"asset_id": "o1", "start_ms": 0, "end_ms": 1, "final_score": 0.1}, i=1)]
    assets = {a: ResolvedAsset(a, "video", "name-" + a, "indexed") for a in ("d1", "o1")}
    out = dataclasses.replace(_outcome(turn=d, assets=assets), tab_hits=d + o)
    sel = select_moments(out)
    assert [m.key[0] for m in sel.items] == ["d1", "o1"]


def test_searched_basis_keeps_call_order_dedups_and_has_no_citation():
    h1 = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    h2 = {"asset_id": "a2", "start_ms": 0, "end_ms": 2000, "final_score": 0.6}
    h1_dup = {"asset_id": "a1", "start_ms": 0, "end_ms": 1000, "final_score": 0.9}  # same key
    turn = [_sh(h1, i=0), _sh(h2, i=1), _sh(h1_dup, i=2)]
    assets = {
        "a1": ResolvedAsset("a1", "video", "name-a1", "indexed"),
        "a2": ResolvedAsset("a2", "video", "name-a2", "indexed"),
    }
    sel = select_moments(_outcome(turn=turn, assets=assets))
    assert sel is not None and sel.basis == "searched"
    assert [m.key for m in sel.items] == [("a1", 0, 1000), ("a2", 0, 2000)]
    # First occurrence wins: the kept a1 card is h1 (0.5), not the later h1_dup (0.9).
    assert sel.items[0].final_score == 0.5
    assert all(m.citation is None for m in sel.items)


def test_cap_keeps_first_moments_max_and_sets_truncated():
    assets = {}
    turn = []
    for i in range(MOMENTS_MAX + 5):
        aid = f"k{i}"
        assets[aid] = ResolvedAsset(aid, "video", "name-" + aid, "indexed")
        turn.append(_sh({"asset_id": aid, "start_ms": 0, "end_ms": 1000, "final_score": 1.0}, i=i))
    sel = select_moments(_outcome(turn=turn, assets=assets))
    assert sel is not None and sel.basis == "searched"
    assert len(sel.items) == MOMENTS_MAX
    assert sel.truncated is True
    # The kept cards are the first MOMENTS_MAX distinct keys, in order.
    assert [m.key for m in sel.items] == [(f"k{i}", 0, 1000) for i in range(MOMENTS_MAX)]


def test_exactly_moments_max_distinct_keys_is_not_truncated():
    assets = {}
    turn = []
    for i in range(MOMENTS_MAX):
        aid = f"k{i}"
        assets[aid] = ResolvedAsset(aid, "video", "name-" + aid, "indexed")
        turn.append(_sh({"asset_id": aid, "start_ms": 0, "end_ms": 1000, "final_score": 1.0}, i=i))
    sel = select_moments(_outcome(turn=turn, assets=assets))
    assert sel is not None
    assert len(sel.items) == MOMENTS_MAX
    assert sel.truncated is False


def test_manifest_subtitle_and_cited_count_follow_the_cards():
    from render.moments import build_moments_manifest
    h = [{"asset_id": f"a{i}", "start_ms": 0, "end_ms": 1000, "final_score": 0.5} for i in range(3)]
    turn = [_sh(x, i=i) for i, x in enumerate(h)]
    assets = {f"a{i}": ResolvedAsset(f"a{i}", "video", f"name-a{i}", "indexed") for i in range(3)}
    mk = lambda sel: build_moments_manifest(sel, session_id="s", turn_id="t", created_at="now")
    mixed = mk(select_moments(_outcome(session=turn, turn=turn, assets=assets,
                                       citations=[_cite("c", turn[0].recency, "a0")])))
    assert mixed["subtitle"] == "1 cited \u00b7 2 more from this turn's searches"
    assert mixed["galleryMoments"]["cited_count"] == 1 and mixed["galleryMoments"]["basis"] == "cited"
    searched = mk(select_moments(_outcome(turn=turn, assets=assets)))
    assert searched["subtitle"] == "Matched by this turn's searches"
    assert searched["galleryMoments"]["cited_count"] == 0
    only = mk(select_moments(_outcome(session=turn[:1], turn=turn[:1], assets=assets,
                                      citations=[_cite("c", turn[0].recency, "a0")])))
    assert only["subtitle"] == "Cited in this answer"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_manifest_carries_the_turns_first_query_for_the_tab_search_box():
    from render.moments import QUERY_MAX, build_moments_manifest
    h = {"asset_id": "a0", "start_ms": 0, "end_ms": 1000, "final_score": 0.5}
    turn = [_sh(h, i=0)]
    assets = {"a0": ResolvedAsset("a0", "video", "name-a0", "indexed")}
    sel = select_moments(_outcome(turn=turn, assets=assets))
    with_q = build_moments_manifest(sel, session_id="s", turn_id="t", created_at="now", query="  night drive  ")
    assert with_q["galleryMoments"]["query"] == "night drive"
    long = build_moments_manifest(sel, session_id="s", turn_id="t", created_at="now", query="x" * 500)
    assert len(long["galleryMoments"]["query"]) == QUERY_MAX
    for q in (None, "", "   "):
        assert "query" not in build_moments_manifest(sel, session_id="s", turn_id="t", created_at="now",
                                                     query=q)["galleryMoments"]

