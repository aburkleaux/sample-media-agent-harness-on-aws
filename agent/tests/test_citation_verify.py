# Feature: media-harness-search-qa-rendering, Property 9: Citation classification
"""Property 9: Citation classification (media-harness-search-qa-rendering task 6.2).

**Validates: Requirements 2.5, 3.4**

Hypothesis over the whole input space `classify_citations` is defined on: an Active_Project id, a list
of `SessionHit`s whose `project_id`s are drawn from the Active_Project and others and whose Hits span
valid and invalid shapes with distinct recencies, an asset map (statuses including `removed`,
Display_Names that differ from the cited name only by case or by `TRIM_CHARS`, and missing ids) or
`None`, and a list of Citations with repeats.

The oracle is written independently below (`_oracle` and the `_o_*` helpers reimplement the Req 2.5
rule, `is_hit`, `whole_second_range` and the frame-key choice) and the test asserts
`classify_citations(...)` equals it field for field, in list order. `SessionHit`, `ResolvedAsset` and
`Citation` are reused from the modules under their own tasks; `normalize_name` is reused as the agreed
Req 2.5 comparison primitive.

The example-based tests below pin the named boundary cases the property's random search reaches only
rarely or not at all: `assets is None`, a case-only and a `TRIM_CHARS`-only name match, a `removed`
asset, the shot frame-key rule, the recency tie-break, other-Project and invalid-shape Hits, the
distinct-key / first-appearance collapse, and the 100-record cap.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from project_search.assets import ResolvedAsset
from project_search.citations import TRIM_CHARS, Citation, normalize_name
from project_search.session_hits import SessionHit
from project_search.verify import (
    MAX_RECORDS,
    AssetReference,
    CitationRecord,
    classify_citations,
)

ACTIVE = "proj-active"
PROJECTS = (ACTIVE, ACTIVE, "proj-x", "proj-y")  # ACTIVE weighted so verified records occur often
ASSET_IDS = ("a1", "a2", "a3")
NAME_BASES = ("alpha", "beta scene")  # already lowercase and trim-clean


# ---------------------------------------------------------------------------
# Independent oracle (reimplements the Req 2.5 / Property 9 rule from scratch).
# ---------------------------------------------------------------------------
def _o_is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _o_is_hit(h: Any) -> bool:
    if not isinstance(h, dict):
        return False
    if not isinstance(h.get("asset_id"), str):
        return False
    start_ms, end_ms = h.get("start_ms"), h.get("end_ms")
    if not _o_is_int(start_ms) or not _o_is_int(end_ms):
        return False
    if not start_ms < end_ms:
        return False
    score = h.get("final_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    return True


def _o_whole_second_range(h: dict) -> tuple[int, int]:
    return (h["start_ms"] // 1000 * 1000, math.ceil(h["end_ms"] / 1000) * 1000)


def _o_frame_key(h: dict) -> Optional[str]:
    if h.get("source_index") != "shot":
        return None
    key = h.get("frame_s3_key")
    return key if isinstance(key, str) and key else None


def _oracle(
    citations: Sequence[Citation],
    *,
    project_id: str,
    session_hits: Sequence[SessionHit],
    assets: Optional[Mapping[str, ResolvedAsset]],
) -> list[CitationRecord]:
    records: list[CitationRecord] = []
    seen: set[str] = set()
    for c in citations:
        if c.key in seen:
            continue
        seen.add(c.key)
        records.append(_oracle_one(c, project_id, session_hits, assets))
        if len(records) >= 100:
            break
    return records


def _oracle_one(
    c: Citation,
    project_id: str,
    session_hits: Sequence[SessionHit],
    assets: Optional[Mapping[str, ResolvedAsset]],
) -> CitationRecord:
    base = dict(
        key=c.key, text=c.text, display_name=c.display_name,
        start_ms=c.start_ms, end_ms=c.end_ms,
    )
    if assets is None:
        return CitationRecord(**base, verified=False, reason="registry_unavailable")

    cited = normalize_name(c.display_name)
    best: Optional[SessionHit] = None
    best_asset: Optional[ResolvedAsset] = None
    for sh in session_hits:
        if sh.project_id != project_id:
            continue
        if not _o_is_hit(sh.hit):
            continue
        asset = assets.get(sh.hit["asset_id"])
        if asset is None or asset.status == "removed":
            continue
        if normalize_name(asset.display_name) != cited:
            continue
        lo, hi = _o_whole_second_range(sh.hit)
        if not (lo <= c.start_ms and c.end_ms <= hi):
            continue
        if best is None or sh.recency > best.recency:
            best, best_asset = sh, asset

    if best is None or best_asset is None:
        return CitationRecord(**base, verified=False, reason="no_matching_hit")
    return CitationRecord(
        **base, verified=True, reason=None,
        ref=AssetReference(
            project_id=project_id,
            asset_id=best.hit["asset_id"],
            frame_s3_key=_o_frame_key(best.hit),
        ),
        kind=best_asset.kind,
        asset_display_name=best_asset.display_name,
        hit_recency=best.recency,
    )


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------
@st.composite
def _name_variant(draw, base: str) -> str:
    """`base` under a case transform, surrounded by `TRIM_CHARS`. Normalizes back to `base`."""
    form = draw(st.sampled_from([base, base.upper(), base.title(), base.swapcase(), base.capitalize()]))
    lead = draw(st.text(alphabet=TRIM_CHARS, max_size=2))
    tail = draw(st.text(alphabet=TRIM_CHARS, max_size=2))
    return lead + form + tail


@st.composite
def _valid_hit(draw) -> dict:
    start = draw(st.integers(min_value=0, max_value=3000))
    end = draw(st.integers(min_value=start + 1, max_value=4000))
    h: dict = {
        "asset_id": draw(st.sampled_from(ASSET_IDS)),
        "start_ms": start,
        "end_ms": end,
        "final_score": draw(st.one_of(
            st.integers(min_value=0, max_value=100),
            st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
        )),
        "title": draw(st.sampled_from(["old title", "", "stale"])),
    }
    source = draw(st.sampled_from(["shot", "caption", "transcript", None]))
    if source is not None:
        h["source_index"] = source
    frame = draw(st.one_of(st.none(), st.just(""), st.sampled_from(["k1", "frames/x.jpg"])))
    if frame is not None:
        h["frame_s3_key"] = frame
    return h


_INVALID_HIT = st.one_of(
    st.sampled_from([None, 5, "x", []]),
    st.just({"start_ms": 1, "end_ms": 2, "final_score": 1.0}),           # missing asset_id
    st.just({"asset_id": "a1", "start_ms": 5, "end_ms": 5, "final_score": 1.0}),  # start == end
    st.just({"asset_id": "a1", "start_ms": "0", "end_ms": 2, "final_score": 1.0}),  # str start
    st.just({"asset_id": "a1", "start_ms": 1, "end_ms": 2, "final_score": True}),   # bool score
    st.just({"asset_id": "a1", "start_ms": 1, "end_ms": 2}),             # missing score
)

# Valid shapes dominate so verification is exercised, but invalid shapes appear too.
_HIT = st.one_of(_valid_hit(), _valid_hit(), _valid_hit(), _INVALID_HIT)


@st.composite
def _resolved(draw, asset_id: str) -> ResolvedAsset:
    base = draw(st.sampled_from(NAME_BASES))
    return ResolvedAsset(
        asset_id=asset_id,
        kind=draw(st.sampled_from(["video", "image", "audio"])),
        display_name=draw(_name_variant(base)),
        status=draw(st.sampled_from(["indexed", "removed", "failed", "queued"])),
    )


@st.composite
def _assets_map(draw) -> dict[str, ResolvedAsset]:
    chosen = draw(st.lists(st.sampled_from(ASSET_IDS), unique=True, max_size=len(ASSET_IDS)))
    return {aid: draw(_resolved(aid)) for aid in chosen}


@st.composite
def _citation(draw) -> Citation:
    name = draw(_name_variant(draw(st.sampled_from(NAME_BASES))))
    start_s = draw(st.integers(min_value=0, max_value=3))
    end_s = draw(st.integers(min_value=start_s, max_value=4))
    text = f"[{name} @ {start_s:02d}:00\u2013{end_s:02d}:00]"
    return Citation(display_name=name, start_ms=start_s * 1000, end_ms=end_s * 1000,
                    span=(0, len(text)), text=text)


@st.composite
def _scenario(draw):
    n = draw(st.integers(min_value=0, max_value=8))
    recencies = draw(st.lists(st.integers(min_value=0, max_value=10_000),
                              min_size=n, max_size=n, unique=True))  # distinct recencies
    hits = [
        SessionHit(
            hit=draw(_HIT),
            project_id=draw(st.sampled_from(PROJECTS)),
            turn_id="t1",
            tool_use_id=f"u{i}",
            index=i,
            recency=(recencies[i], 0, 0),
        )
        for i in range(n)
    ]
    assets = draw(st.one_of(st.none(), _assets_map()))

    citations = draw(st.lists(_citation(), max_size=10))
    if citations:  # inject repeats (same object -> same key), then shuffle
        citations = draw(st.permutations(citations + draw(st.lists(st.sampled_from(citations), max_size=5))))
    return list(citations), assets, hits


@settings(max_examples=200, deadline=None)
@given(_scenario())
def test_classify_citations_matches_oracle(scenario):
    citations, assets, hits = scenario
    expected = _oracle(citations, project_id=ACTIVE, session_hits=hits, assets=assets)
    actual = classify_citations(citations, project_id=ACTIVE, session_hits=hits, assets=assets)
    assert actual == expected


# ---------------------------------------------------------------------------
# Pinned boundary cases.
# ---------------------------------------------------------------------------
def _cite(name: str, start_s: int, end_s: int) -> Citation:
    text = f"[{name} @ {start_s:02d}:00\u2013{end_s:02d}:00]"
    return Citation(display_name=name, start_ms=start_s * 1000, end_ms=end_s * 1000,
                    span=(0, len(text)), text=text)


def _hit(asset_id: str, start_ms: int, end_ms: int, *, score=1.0, source=None, frame=None) -> dict:
    h = {"asset_id": asset_id, "start_ms": start_ms, "end_ms": end_ms, "final_score": score}
    if source is not None:
        h["source_index"] = source
    if frame is not None:
        h["frame_s3_key"] = frame
    return h


def _sh(hit: dict, *, project_id=ACTIVE, recency=(1, 0, 0)) -> SessionHit:
    return SessionHit(hit=hit, project_id=project_id, turn_id="t", tool_use_id="u",
                      index=0, recency=recency)


def test_assets_none_is_registry_unavailable():
    cites = [_cite("Alpha", 0, 1)]
    hits = [_sh(_hit("a1", 0, 1500))]
    out = classify_citations(cites, project_id=ACTIVE, session_hits=hits, assets=None)
    assert len(out) == 1
    assert out[0].verified is False
    assert out[0].reason == "registry_unavailable"
    assert out[0].ref is None


def test_no_matching_hit_reason():
    cites = [_cite("Alpha", 0, 1)]
    out = classify_citations(cites, project_id=ACTIVE, session_hits=[], assets={})
    assert out[0].verified is False and out[0].reason == "no_matching_hit" and out[0].ref is None


def test_verified_case_only_name_match():
    hits = [_sh(_hit("a1", 0, 1500))]
    assets = {"a1": ResolvedAsset("a1", "video", "ALPHA", "indexed")}
    out = classify_citations([_cite("alpha", 0, 1)], project_id=ACTIVE, session_hits=hits, assets=assets)
    assert out[0].verified is True
    assert out[0].ref == AssetReference(ACTIVE, "a1", None)
    assert out[0].kind == "video" and out[0].asset_display_name == "ALPHA"


def test_verified_trim_only_name_match():
    padded = f"{TRIM_CHARS[0]}{TRIM_CHARS[3]}Alpha{TRIM_CHARS[-1]}"  # surrounded by TRIM_CHARS
    hits = [_sh(_hit("a1", 0, 1500))]
    assets = {"a1": ResolvedAsset("a1", "image", padded, "indexed")}
    out = classify_citations([_cite("Alpha", 0, 1)], project_id=ACTIVE, session_hits=hits, assets=assets)
    assert out[0].verified is True and out[0].asset_display_name == padded


def test_removed_asset_not_verified():
    hits = [_sh(_hit("a1", 0, 1500))]
    assets = {"a1": ResolvedAsset("a1", "video", "Alpha", "removed")}
    out = classify_citations([_cite("Alpha", 0, 1)], project_id=ACTIVE, session_hits=hits, assets=assets)
    assert out[0].verified is False and out[0].reason == "no_matching_hit"


def test_frame_key_only_for_shot_hit():
    assets = {"a1": ResolvedAsset("a1", "image", "Alpha", "indexed")}
    cite = [_cite("Alpha", 0, 1)]

    shot = [_sh(_hit("a1", 0, 1500, source="shot", frame="frames/x.jpg"))]
    assert classify_citations(cite, project_id=ACTIVE, session_hits=shot, assets=assets)[0].ref.frame_s3_key == "frames/x.jpg"

    non_shot = [_sh(_hit("a1", 0, 1500, source="caption", frame="frames/x.jpg"))]
    assert classify_citations(cite, project_id=ACTIVE, session_hits=non_shot, assets=assets)[0].ref.frame_s3_key is None

    empty_key = [_sh(_hit("a1", 0, 1500, source="shot", frame=""))]
    assert classify_citations(cite, project_id=ACTIVE, session_hits=empty_key, assets=assets)[0].ref.frame_s3_key is None


def test_recency_tie_break_chooses_largest():
    assets = {
        "a1": ResolvedAsset("a1", "video", "Alpha", "indexed"),
        "a2": ResolvedAsset("a2", "audio", "Alpha", "indexed"),
    }
    low = _sh(_hit("a1", 0, 1500), recency=(1, 0, 0))
    high = _sh(_hit("a2", 0, 1500), recency=(9, 0, 0))
    out = classify_citations([_cite("Alpha", 0, 1)], project_id=ACTIVE,
                             session_hits=[low, high], assets=assets)
    assert out[0].verified is True
    assert out[0].ref.asset_id == "a2" and out[0].hit_recency == (9, 0, 0) and out[0].kind == "audio"


def test_other_project_and_invalid_hits_ignored():
    assets = {"a1": ResolvedAsset("a1", "video", "Alpha", "indexed")}
    hits = [
        _sh(_hit("a1", 0, 1500), project_id="proj-x"),            # other project
        _sh({"asset_id": "a1", "start_ms": 5, "end_ms": 5, "final_score": 1.0}),  # invalid shape
    ]
    out = classify_citations([_cite("Alpha", 0, 1)], project_id=ACTIVE, session_hits=hits, assets=assets)
    assert out[0].verified is False and out[0].reason == "no_matching_hit"


def test_distinct_key_first_appearance_and_text():
    first = _cite("Alpha", 0, 1)
    second = Citation(display_name="ALPHA", start_ms=0, end_ms=1000, span=(0, 3), text="later")
    assert first.key == second.key  # same normalized name + times
    out = classify_citations([first, second, first], project_id=ACTIVE, session_hits=[], assets={})
    assert len(out) == 1  # collapsed to one record per distinct key
    assert out[0].text == first.text  # first occurrence's text wins, not "later"


def test_cap_at_max_records():
    cites = [_cite(f"name{i}", 0, 1) for i in range(MAX_RECORDS + 25)]
    out = classify_citations(cites, project_id=ACTIVE, session_hits=[], assets={})
    assert len(out) == MAX_RECORDS
    assert [r.key for r in out] == [c.key for c in cites[:MAX_RECORDS]]
