"""Property test for Compact_Hit field reduction and truncation
(media-harness-search-qa-rendering design Component 2, Correctness Property 1).

### Property 1: Compact_Hit field reduction and truncation

For any Hit dict, with arbitrary extra keys (``doc_id``, ``rrf_score``, ``match_sources``,
``*_parent_id``, ``genres``, unknown names), ``text`` and ``named_entities`` of 0 to 4,000 code
points including astral characters, and ``named_entities`` as a string, list or dict,
``compact_hit(h)`` has only keys from ``COMPACT_FIELDS`` and ``OPTIONAL_FIELDS``. Each of those keys
is present exactly when ``h`` has it with a non-``None`` value (the five ``COMPACT_FIELDS`` whenever
present). ``asset_id``, ``start_ms``, ``end_ms``, ``source_index``, ``final_score``, ``title``,
``highlights`` and ``frame_s3_key`` equal ``h``'s values. ``text`` equals ``h["text"]`` when it is at
most 1,500 code points and otherwise its first 1,500 code points followed by ``…``. ``named_entities``
equals the same rule applied to ``serialize_entities(h["named_entities"])``.
``json.loads(ShapedResult.model_text())["results"]`` equals ``compact``.

Amended 2026-10-16 (search speed): ``text`` is cut at 800 code points (1,500 for a document hit, whose
text is the page), ``named_entities`` at 300, a float ``final_score`` is rounded to 3 places, and
``highlights`` -- which repeat ``text`` with ``<em>`` marks -- become ``matched_terms`` (their ``<em>``
terms) plus only the passages the compact text does not already show. The ledger still keeps every Hit
whole, so citations and Moments are unaffected.

**Validates: Requirements 1.18, 1.23**

This is a property-based test (Hypothesis). The expected shaping is recomputed from the requirement
text by an in-test oracle, independent of ``compact.py``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import ledger  # noqa: E402
from project_search import document_recall  # noqa: E402
from project_search.compact import (  # noqa: E402
    ASSET_KINDS,
    COMPACT_FIELDS,
    DERIVED_FIELDS,
    OPTIONAL_FIELDS,
    ShapedResult,
    compact_hit,
)
from render.moments import highlight_terms  # noqa: E402
from project_search.errors import SearchOutcome  # noqa: E402
from project_search.token_ref import ProjectTokenRef  # noqa: E402
from project_search.tool import ProjectSearchTool  # noqa: E402

# ---------------------------------------------------------------------------
# Oracle: the Property 1 rules re-stated from the requirement text, independent
# of the module under test.
# ---------------------------------------------------------------------------

# Caps as amended 2026-10-16 (model-visible text only; the ledger keeps every Hit whole).
_TEXT_MAX = 800
_DOC_TEXT_MAX = 1500
_NE_MAX = 300
_SNIPPET_MAX = 300
_ELLIPSIS = "\u2026"  # …
_KNOWN = set(COMPACT_FIELDS) | set(OPTIONAL_FIELDS) | set(DERIVED_FIELDS)
# Fields copied verbatim (the invariants except the rounded final_score, plus title and frame_s3_key).
_COPIED_UNCHANGED = (
    "asset_id",
    "start_ms",
    "end_ms",
    "source_index",
    "title",
    "frame_s3_key",
)


def _oracle_truncate(s: str, limit: int = _TEXT_MAX) -> str:
    return s if len(s) <= limit else s[:limit] + _ELLIPSIS


def _oracle_is_document(hit) -> bool:
    start = hit.get("start_ms")
    return (isinstance(hit.get("asset_id"), str) and hit["asset_id"].startswith("document:")
            and isinstance(start, int) and not isinstance(start, bool) and start >= 0)


def _oracle_serialize(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Code points across the BMP (minus surrogates) and the astral planes.
_bmp_chars = st.characters(blacklist_categories=("Cs",), max_codepoint=0xFFFF)
_astral_chars = st.characters(min_codepoint=0x10000, max_codepoint=0x10FFFF)
_any_char = st.one_of(_bmp_chars, _astral_chars)

# Strings of 0 to 4,000 code points, including astral characters.
_long_text = st.text(alphabet=_any_char, min_size=0, max_size=4000)

# JSON values usable as field contents / extra keys. NaN/Infinity are excluded so
# that the json round-trip in the final assertion compares equal.
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=8,
)

# named_entities: a string (0-4,000 code points), a list, or a dict.
_named_entities = st.one_of(
    _long_text,
    st.lists(_json_values, max_size=5),
    st.dictionaries(st.text(max_size=8), _json_values, max_size=5),
)

# Extra keys that must not survive compaction. Drawn from the names called out in
# Property 1 plus arbitrary unknown names, never colliding with a known field.
_extra_key = st.one_of(
    st.sampled_from(
        [
            "doc_id",
            "rrf_score",
            "match_sources",
            "shot_parent_id",
            "scene_parent_id",
            "utterance_parent_id",
            "genres",
            "score",
            "collection_id",
        ]
    ),
    st.text(max_size=10).filter(lambda k: k not in _KNOWN),
)
_extras = st.dictionaries(_extra_key, _json_values, max_size=6)

_ABSENT = object()


def _slot(value_strat):
    """Absent, present-and-None, or present-with-a-value."""
    return st.one_of(st.just(_ABSENT), st.none(), value_strat)


@st.composite
def _hits(draw):
    hit: dict = {}
    # Arbitrary extra keys first; real fields below win on any (filtered-out) collision.
    hit.update(draw(_extras))

    # The five COMPACT_FIELDS: any JSON value, None, or absent.
    for field in COMPACT_FIELDS:
        val = draw(_slot(_json_values))
        if val is not _ABSENT:
            hit[field] = val

    # title / highlights / frame_s3_key: copied unchanged.
    for field in ("title", "highlights", "frame_s3_key"):
        val = draw(_slot(_json_values))
        if val is not _ABSENT:
            hit[field] = val

    # text: a string of 0-4,000 code points (or None/absent).
    text_val = draw(_slot(_long_text))
    if text_val is not _ABSENT:
        hit["text"] = text_val

    # named_entities: str / list / dict (or None/absent).
    ne_val = draw(_slot(_named_entities))
    if ne_val is not _ABSENT:
        hit["named_entities"] = ne_val

    return hit


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(_hits())
# Truncation boundary: at, just over, and well over 1,500 code points, incl. astral.
@example({"asset_id": "video:a", "text": "a" * 1500})
@example({"asset_id": "video:a", "text": "a" * 1501})
@example({"asset_id": "video:a", "text": "\U0001D11E" * 1600})  # astral char repeated
@example({"asset_id": "video:a", "named_entities": "n" * 1501})
@example({"asset_id": "video:a", "named_entities": [{"k": "v"}, 1, "two"]})
@example({"asset_id": "video:a", "named_entities": {"people": ["x"], "n": 2}})
# Present-but-None optionals drop; present COMPACT_FIELDS (even None) stay.
@example({"asset_id": None, "title": None, "text": None, "named_entities": None})
@example({"doc_id": "d", "rrf_score": 0.5, "genres": ["g"]})  # extras-only
def test_property1_compact_hit_field_reduction_and_truncation(hit):
    out = compact_hit(hit)

    # Only keys from COMPACT_FIELDS and OPTIONAL_FIELDS survive.
    assert set(out).issubset(_KNOWN), f"unexpected keys: {set(out) - _KNOWN}"

    # COMPACT_FIELDS: present exactly when present in the hit (regardless of None), equal to the hit's
    # (final_score: a float rounded to 3 places, it only orders hits).
    for field in COMPACT_FIELDS:
        if field in hit:
            assert field in out
            expected = hit[field]
            if field == "final_score" and isinstance(expected, float):
                expected = round(expected, 3)
            assert out[field] == expected
        else:
            assert field not in out

    # title / text / named_entities / frame_s3_key: present exactly when present with a non-None value.
    for field in ("title", "text", "named_entities", "frame_s3_key"):
        expected_present = field in hit and hit[field] is not None
        assert (field in out) == expected_present

    # Copied-unchanged fields equal the hit's values.
    for field in _COPIED_UNCHANGED:
        if field in hit and hit[field] is not None:
            assert out[field] == hit[field]

    # text truncation rule (a document hit keeps the longer cap: its text is the page).
    if hit.get("text") is not None:
        limit = _DOC_TEXT_MAX if _oracle_is_document(hit) else _TEXT_MAX
        assert out["text"] == _oracle_truncate(str(hit["text"]), limit)

    # named_entities: serialize then truncate.
    if hit.get("named_entities") is not None:
        assert out["named_entities"] == _oracle_truncate(_oracle_serialize(hit["named_entities"]), _NE_MAX)

    # highlights: the matched <em> terms once, plus only passages the compact text/entities do not show.
    if hit.get("highlights") is None:
        assert "matched_terms" not in out and "highlights" not in out
    else:
        terms = highlight_terms(hit["highlights"])
        assert out.get("matched_terms", []) == terms
        assert ("matched_terms" in out) == bool(terms)
        shown = [v for v in (out.get("text"), out.get("named_entities")) if isinstance(v, str)]
        for passage in out.get("highlights", []):
            assert isinstance(passage, str) and "<em>" not in passage.lower()
            assert len(passage) <= _SNIPPET_MAX + 2
            assert not any(passage in s for s in shown)
        if "highlights" in out:
            assert out["highlights"]

    # ShapedResult.model_text() carries compact verbatim under "results".
    shaped = ShapedResult(
        delivered=[hit],
        compact=[out],
        removed=None,
        kinds=None,
        skipped_indices=None,
    )
    assert json.loads(shaped.model_text())["results"] == [out]


# ===========================================================================
# Property 2: Delivered list, Asset_Kind_Filter and removed count
# (media-harness-search-qa-rendering design Component 2, Correctness Property 2).
#
# For any search_content result (Hits whose asset_id is video:…, image:…,
# audio:…, another prefix, or not a string, with or without skipped_indices),
# any valid k or none, and any valid asset_kinds or none, a
# ProjectSearchTool._run call (driven through .stream with a fake client and the
# real in-memory ledger) satisfies:
#   - delivered == [h for h in hits if kinds is None or (asset_id is a str and
#     starts with "<kind>:" for some kind in kinds)][: k or 10];
#   - the reported asset_kinds.removed equals the number of result Hits that fail
#     the filter, and asset_kinds is absent from the model text when the call had
#     none;
#   - skipped_indices is in the model text exactly when the result carried it,
#     with the same value;
#   - the arguments passed to client.call never contain asset_kinds;
#   - the ledger entry's Hits equal delivered (full, untruncated), and an arm-B
#     search_result frame carries the same list.
#
# Validates: Requirements 1.18, 1.22, 1.23
#
# The expected shaping is recomputed from the requirement text by an in-test
# oracle, independent of compact.py.
# ===========================================================================

_PID = "p" + "a" * 32
_ABSENT_RESULT = object()  # distinct from the Property 1 _ABSENT sentinel


def _tok() -> str:
    """A JWT-shaped Project_Token that ProjectTokenRef accepts and returns verbatim (iat now, 1 h life)."""
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    header = seg({"alg": "RS256", "typ": "JWT"})
    body = seg({"sub": "u1", "project_id": _PID, "iat": iat, "exp": iat + 3600, "jti": "j1"})
    return f"{header}.{body}.sig"


_SPEC = {
    "name": "tgt___hybrid-search___search_content",
    "description": "d",
    "inputSchema": {"json": {"type": "object"}},
}


class _FakeClient:
    """Records every (token, args) and returns a fixed SearchOutcome."""

    tool_name = _SPEC["name"]

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def call(self, token, args):
        self.calls.append((token, dict(args)))  # snapshot: asset_kinds is popped before this call
        return self.outcome


def _stream_result(tool, args, tuid):
    async def go():
        return [ev async for ev in tool.stream({"toolUseId": tuid, "name": _SPEC["name"], "input": args}, {})]

    return asyncio.run(go())[-1].tool_result


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_kind_prefixed = st.builds(lambda k, s: f"{k}:{s}", st.sampled_from(ASSET_KINDS), st.text(max_size=5))
_other_prefixed = st.builds(
    lambda p, s: f"{p}:{s}",
    st.sampled_from(["thumb", "clip", "other", "Video", "VIDEO", "AUDIO"]),
    st.text(max_size=5),
)
_plain_string = st.text(max_size=8)  # may or may not contain a colon
_non_string_asset_id = st.one_of(st.none(), st.integers(min_value=-10, max_value=10), st.booleans())
_asset_id = st.one_of(_kind_prefixed, _other_prefixed, _plain_string, _non_string_asset_id)

_P2_ABSENT = object()


@st.composite
def _result_hit(draw):
    """A Hit dict with a (possibly absent / non-string) asset_id and a few JSON-safe optional fields."""
    hit: dict = {}
    aid = draw(st.one_of(st.just(_P2_ABSENT), _asset_id))
    if aid is not _P2_ABSENT:
        hit["asset_id"] = aid
    if draw(st.booleans()):
        hit["start_ms"] = draw(st.integers(min_value=0, max_value=10**7))
    if draw(st.booleans()):
        hit["end_ms"] = draw(st.integers(min_value=0, max_value=10**7))
    if draw(st.booleans()):
        hit["source_index"] = draw(st.sampled_from(["shot", "scene", "utterance", "topic"]))
    if draw(st.booleans()):
        hit["final_score"] = draw(st.integers(min_value=0, max_value=100))
    if draw(st.booleans()):
        hit["text"] = draw(st.text(max_size=40))
    if draw(st.booleans()):
        hit["frame_s3_key"] = draw(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20))
    return hit


_hits_list = st.lists(_result_hit(), max_size=12)
_k = st.one_of(st.none(), st.integers(min_value=1, max_value=50))
_asset_kinds = st.one_of(
    st.none(),
    st.lists(st.sampled_from(ASSET_KINDS), min_size=1, max_size=3, unique=True),
)
_skipped = st.one_of(st.just(_ABSENT_RESULT), st.lists(st.integers(min_value=0, max_value=50), max_size=5))


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


def _oracle_filter(hits, kinds):
    if kinds is None:
        return list(hits), 0
    prefixes = tuple(f"{x}:" for x in kinds)
    kept = [h for h in hits if isinstance(h.get("asset_id"), str) and h["asset_id"].startswith(prefixes)]
    return kept, len(hits) - len(kept)


def _oracle_removed(hits, kinds, k):
    """Hits the filter dropped while filling the model's first `k` (2026-10-10: M2C now returns up to 50)."""
    if kinds is None:
        return 0
    prefixes = tuple(f"{x}:" for x in kinds)
    kept = removed = 0
    for h in hits:
        if kept >= k:
            break
        if isinstance(h.get("asset_id"), str) and h["asset_id"].startswith(prefixes):
            kept += 1
        else:
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(hits=_hits_list, k=_k, kinds=_asset_kinds, skipped=_skipped)
# Empty result, no filter.
@example(hits=[], k=None, kinds=None, skipped=_ABSENT_RESULT)
# A filter that removes everything (all hits are a different kind / non-string asset_id).
@example(
    hits=[{"asset_id": "image:1"}, {"asset_id": "audio:2"}, {"asset_id": 7}],
    k=None,
    kinds=["video"],
    skipped=_ABSENT_RESULT,
)
# Mixed kinds, filter keeps a subset; default k.
@example(
    hits=[{"asset_id": "video:1"}, {"asset_id": "image:2"}, {"asset_id": "video:3"}, {"asset_id": "audio:4"}],
    k=None,
    kinds=["video", "audio"],
    skipped=[0, 2],
)
# k smaller than the kept list truncates after filtering.
@example(
    hits=[{"asset_id": "video:1"}, {"asset_id": "video:2"}, {"asset_id": "video:3"}],
    k=1,
    kinds=["video"],
    skipped=_ABSENT_RESULT,
)
# skipped_indices carried through with no asset_kinds.
@example(hits=[{"asset_id": "video:1"}], k=None, kinds=None, skipped=[1, 4])
def test_property2_delivered_asset_kind_filter_and_removed_count(hits, k, kinds, skipped):
    # Build the M2C result exactly as the gateway returns it: a JSON string.
    result_obj: dict = {"results": hits}
    if skipped is not _ABSENT_RESULT:
        result_obj["skipped_indices"] = skipped
    result_json = json.dumps(result_obj, ensure_ascii=False)

    client = _FakeClient(SearchOutcome("call", "ok", result=result_json))
    turn_id = "p2-" + uuid.uuid4().hex
    frames: list[dict] = []
    tool = ProjectSearchTool(
        ProjectTokenRef(_tok()),
        _SPEC,
        project_id=_PID,
        turn_id=turn_id,
        client=client,
        dispatch_nonce="n1",
        frame_sink=frames.append,
    )

    args: dict = {"collection_id": _PID, "search_term": "q"}
    if k is not None:
        args["k"] = k
    if kinds is not None:
        args["asset_kinds"] = list(kinds)

    known_docs = document_recall.project_has_documents(_PID)
    try:
        result = _stream_result(tool, args, "tu-p2")
        assert result["status"] == "success"

        # Oracle computed from the Hits the tool actually parses (post JSON round trip).
        src_hits = json.loads(result_json)["results"]
        kept, _ = _oracle_filter(src_hits, kinds)
        expected_delivered = kept[: (k or 10)]
        removed = _oracle_removed(src_hits, kinds, k or 10)
        expected_overflow = kept[len(expected_delivered):50]

        # (1) The ledger entry holds exactly the delivered Hits, full and untruncated.
        entries = ledger.results_for(turn_id)
        assert len(entries) == 1
        assert entries[0].ok is True
        assert entries[0].hits == expected_delivered
        # (1b) Hits ranked past the model's k go to the ledger's extra_hits only (the Moments tab).
        assert entries[0].extra_hits == expected_overflow

        # (2) The arm-B search_result frame carries the same list.
        assert len(frames) == 1
        frame = frames[0]
        assert frame["type"] == "search_result" and frame["ok"] is True and frame["nonce"] == "n1"
        assert frame["hits"] == expected_delivered

        # (3) client.call was reached once for the query (plus, when the kinds allow documents, its `#scene`
        #     document companion), and no call's arguments carried asset_kinds.
        #     Since 2026-10-16 the companion of a non-document query ("q") is DEFERRED: it runs only when the
        #     plain result shows documents (one past the model's k, none inside it), unless the project is
        #     already known to hold documents (then it runs in parallel, as before).
        companion = [c for c in client.calls if c[1]["search_term"] == f"{args['search_term']} #scene"]
        if kinds is not None and "document" not in kinds:
            expected_companions = 0
        elif kinds is not None or known_docs:
            expected_companions = 1  # asset_kinds names documents / documents seen: parallel
        else:
            def is_doc(h):
                return isinstance(h.get("asset_id"), str) and h["asset_id"].startswith("document:")
            limit = k or 10
            expected_companions = int(any(is_doc(h) for h in src_hits[limit:])
                                      and not any(is_doc(h) for h in src_hits[:limit]))
        assert len(companion) == expected_companions
        assert len(client.calls) == 1 + len(companion)
        assert all("asset_kinds" not in c[1] for c in client.calls)
        _token, sent_args = client.calls[0]
        assert "asset_kinds" not in sent_args
        # k is raised to TAB_FETCH_K (50) for the Moments tab; everything else passes through.
        assert sent_args == {**{k2: v for k2, v in args.items() if k2 != "asset_kinds"}, "k": 50}

        # (4) Model text: results, removed count, asset_kinds presence, skipped_indices passthrough.
        payload = json.loads(result["content"][0]["text"])
        assert payload["results"] == [compact_hit(h) for h in expected_delivered]

        if kinds is None:
            assert "asset_kinds" not in payload
        else:
            assert payload["asset_kinds"] == {"kinds": list(kinds), "removed": removed}

        if skipped is _ABSENT_RESULT:
            assert "skipped_indices" not in payload
        else:
            assert payload["skipped_indices"] == skipped
    finally:
        ledger.clear_turn(turn_id)


# --- lineage-first: an original is never crowded out by the job outputs made from it (golden 2026-10-17) ---

_ORIG = "netflix_open_content_meridian"


def _lh(title, i, start=23090):
    return {"asset_id": f"video:{i}", "title": title, "start_ms": start, "end_ms": start + 6000, "final_score": 1 - i / 100}


def test_an_original_in_overflow_is_promoted_ahead_of_its_outputs():
    from project_search.compact import shape_result
    outputs = [_lh(f"{_ORIG} — segment shots (segment_{i:04d}).mp4", i) for i in range(12)]
    original = _lh(f"{_ORIG}.mp4", 30)
    r = shape_result(json.dumps({"results": outputs + [original]}), k=10, asset_kinds=None)
    assert r.delivered[0] is not None and r.delivered[0]["title"] == f"{_ORIG}.mp4"
    assert len(r.delivered) == 10
    # Same set of hits overall: the displaced output moved to the head of overflow.
    assert len(r.delivered) + len(r.overflow) == 13
    assert r.overflow[0]["title"].endswith("(segment_0009).mp4")
    assert "derived_from" not in r.compact[0]
    assert all(c["derived_from"] == f"{_ORIG}.mp4" for c in r.compact[1:])


def test_an_original_already_ahead_is_left_where_it_is():
    from project_search.compact import shape_result
    hits = [_lh(f"{_ORIG}.mp4", 0), _lh(f"{_ORIG} — process media (meridian_trim).mp4", 1)]
    r = shape_result(json.dumps({"results": hits}), k=10, asset_kinds=None)
    assert [h["asset_id"] for h in r.delivered] == ["video:0", "video:1"]
    assert r.compact[1]["derived_from"] == f"{_ORIG}.mp4"


def test_a_delivered_original_moves_before_its_first_output():
    from project_search.compact import shape_result
    hits = [_lh("other.mp4", 0), _lh(f"{_ORIG} — clip (a).mp4", 1), _lh("x.mp4", 2), _lh(f"{_ORIG} (2).mp4", 3)]
    r = shape_result(json.dumps({"results": hits}), k=10, asset_kinds=None)
    assert [h["asset_id"] for h in r.delivered] == ["video:0", "video:3", "video:1", "video:2"]


def test_outputs_without_their_original_and_plain_hits_are_untouched():
    from project_search.compact import shape_result
    hits = [_lh(f"{_ORIG} — clip (a).mp4", 0), _lh("plain.mp4", 1), {"asset_id": "video:9", "start_ms": 0, "end_ms": 1}]
    r = shape_result(json.dumps({"results": hits}), k=10, asset_kinds=None)
    assert [h["asset_id"] for h in r.delivered] == ["video:0", "video:1", "video:9"]
    assert all("derived_from" not in c for c in r.compact)


def test_when_the_last_delivered_hit_is_the_output_the_original_takes_its_slot():
    from project_search.compact import shape_result
    hits = [_lh(f"p{i}.mp4", i) for i in range(9)] + [_lh(f"{_ORIG} — clip (a).mp4", 9), _lh(f"{_ORIG}.mp4", 10)]
    r = shape_result(json.dumps({"results": hits}), k=10, asset_kinds=None)
    assert r.delivered[-1]["asset_id"] == "video:10"
    assert r.overflow[0]["asset_id"] == "video:9"
