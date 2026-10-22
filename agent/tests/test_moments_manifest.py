"""Property test for highlight term extraction (design Property 16).

Drives ``render.moments.highlight_terms`` with hand-built ``highlights`` values shaped exactly as
the design describes: a string, or a list or dict nested up to depth 3, whose string leaves carry
``<em>…</em>`` spans with nested tags, HTML entities and surrounding whitespace, and whose non-string
leaves (ints, floats, bools, ``None``) must be ignored.

The expected term list is re-derived independently in this file (the design wording: collect the
string leaves no deeper than depth 3, and from each ``<em>…</em>`` take the tag-stripped,
entity-unescaped, ``TRIM_CHARS``-trimmed inner text, dropping empties and terms over 100 code
points, keeping the first occurrence per ``casefold()`` and at most 20). The assertions compare
that re-derivation against the module under test and also check the standalone invariants of
Property 16 directly.

**Validates: Requirements 3.10**
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_search.citations import TRIM_CHARS  # noqa: E402
from render.moments import (  # noqa: E402
    HIGHLIGHT_TERM_MAX_CHARS,
    HIGHLIGHT_TERMS_MAX,
    highlight_terms,
)

# `highlights` is walked no deeper than this many container levels (design wording; mirrors
# render.moments._HIGHLIGHT_MAX_DEPTH). The generator below never nests deeper, so every string
# leaf it produces is reachable.
_MAX_DEPTH = 3

_TRIM_SET = frozenset(TRIM_CHARS)
# Independent twins of the design's extraction regexes (re-derivation, not an import of the module).
_EM_RE = re.compile(r"<em>(.*?)</em>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")


# ---------------------------------------------------------------------------
# Independent re-derivation of the design wording (never the module under test).
# ---------------------------------------------------------------------------
def _trim(text: str) -> str:
    """Strip the explicit ``TRIM_CHARS`` set from both ends (not ``str.strip()``)."""
    start = 0
    end = len(text)
    while start < end and text[start] in _TRIM_SET:
        start += 1
    while end > start and text[end - 1] in _TRIM_SET:
        end -= 1
    return text[start:end]


def collect_leaves(node, depth: int = 1) -> list[str]:
    """Every string leaf of ``node``, walking lists and dicts no deeper than :data:`_MAX_DEPTH`.

    Mirrors the design: a string is a leaf at any depth; only dict *values* are walked, never keys;
    a non-string leaf contributes nothing.
    """
    if isinstance(node, str):
        return [node]
    if depth > _MAX_DEPTH:
        return []
    out: list[str] = []
    if isinstance(node, list):
        for item in node:
            out.extend(collect_leaves(item, depth + 1))
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(collect_leaves(value, depth + 1))
    return out


def extract_term(em_inner: str) -> str:
    """The design transform on one ``<em>`` span's inner text: strip tags, unescape entities, trim."""
    inner = _TAG_RE.sub("", em_inner)
    inner = html.unescape(inner)
    return _trim(inner)


def expected_terms(highlights) -> list[str]:
    """Re-derive ``highlight_terms`` from the design wording (Req 3.10, Property 16)."""
    terms: list[str] = []
    seen: set[str] = set()
    for leaf in collect_leaves(highlights):
        for match in _EM_RE.finditer(leaf):
            inner = extract_term(match.group(1))
            if not inner or len(inner) > HIGHLIGHT_TERM_MAX_CHARS:
                continue
            fold = inner.casefold()
            if fold in seen:
                continue
            seen.add(fold)
            terms.append(inner)
            if len(terms) >= HIGHLIGHT_TERMS_MAX:
                return terms
    return terms


# ---------------------------------------------------------------------------
# Strategies: build `highlights` whose string leaves carry <em> spans with
# nested tags, entities and surrounding whitespace, plus non-string leaves.
# ---------------------------------------------------------------------------
# A handful of TRIM_CHARS code points to surround an <em> span's inner text.
_WS = st.text(alphabet="\u0020\u0009\u00a0\u2003\u3000\ufeff", max_size=3)
# Nested markup that the tag-strip must remove (no `em` tag, no bare '>' inside).
_NESTED_TAG = st.sampled_from(["<b>", "</b>", "<i>", "</i>", "<span class='x'>", "<br/>"])
# HTML entities the unescape must resolve (`&nbsp;` becomes U+00A0, which is itself a TRIM char).
_ENTITY = st.sampled_from(["&amp;", "&lt;", "&gt;", "&quot;", "&#65;", "&nbsp;"])
# Core visible text, free of '<', '>', '&' so it introduces no accidental tag or entity.
_CORE = st.text(alphabet="abcABC 123\u00e9\u00f1\u65e5-_", max_size=8)
# `<em>` tags in several cases to exercise the IGNORECASE match.
_EM_OPEN = st.sampled_from(["<em>", "<EM>", "<Em>", "<eM>"])
_EM_CLOSE = st.sampled_from(["</em>", "</EM>", "</Em>"])
# Noise between spans, free of '<', '>', '&' so it holds no spurious span.
_NOISE = st.text(alphabet="xyz .,:;!?\n", max_size=5)


@st.composite
def em_span(draw) -> str:
    """One ``<em>…</em>`` span whose inner mixes core text, nested tags, entities and whitespace."""
    parts = draw(st.lists(st.one_of(_CORE, _NESTED_TAG, _ENTITY), max_size=6))
    inner = draw(_WS) + "".join(parts) + draw(_WS)
    return draw(_EM_OPEN) + inner + draw(_EM_CLOSE)


@st.composite
def leaf_string(draw) -> str:
    """A string leaf: noise interleaved with zero or more ``<em>`` spans."""
    chunks = draw(st.lists(st.one_of(_NOISE, em_span()), max_size=6))
    return "".join(chunks)


# A leaf is a string carrying spans, or a non-string value that must be ignored.
_LEAF = st.one_of(
    leaf_string(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
)
# Depth-3 structure: a leaf, or a list/dict of leaves, or a list/dict of (lists/dicts of leaves).
_LEVEL3 = _LEAF
_LEVEL2 = st.one_of(
    _LEAF,
    st.lists(_LEVEL3, max_size=3),
    st.dictionaries(st.text(max_size=3), _LEVEL3, max_size=3),
)
_HIGHLIGHTS = st.one_of(
    _LEAF,
    st.lists(_LEVEL2, max_size=3),
    st.dictionaries(st.text(max_size=3), _LEVEL2, max_size=3),
)


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------
@settings(max_examples=500)
@given(_HIGHLIGHTS)
def test_property16_highlight_term_extraction(highlights):
    terms = highlight_terms(highlights)

    # The module equals the independent re-derivation of the design wording (order, first-occurrence
    # dedup, cap and all).
    assert terms == expected_terms(highlights)

    # Every candidate term the design would ever accept, for the membership check below.
    candidates = []
    for leaf in collect_leaves(highlights):
        for match in _EM_RE.finditer(leaf):
            cand = extract_term(match.group(1))
            if cand and len(cand) <= HIGHLIGHT_TERM_MAX_CHARS:
                candidates.append(cand)
    candidate_set = set(candidates)

    seen_folds: set[str] = set()
    for term in terms:
        # Each term is the tag-stripped, entity-unescaped, trimmed inner text of some <em> span.
        assert term in candidate_set
        # Non-empty and at most 100 code points.
        assert term
        assert len(term) <= HIGHLIGHT_TERM_MAX_CHARS
        # Distinct under casefold().
        fold = term.casefold()
        assert fold not in seen_folds
        seen_folds.add(fold)

    # At most 20 terms.
    assert len(terms) <= HIGHLIGHT_TERMS_MAX

    # The result is a list of plain strings.
    assert isinstance(terms, list)
    assert all(isinstance(t, str) for t in terms)


# ---------------------------------------------------------------------------
# Concrete examples pinning the specific clauses of Property 16.
# ---------------------------------------------------------------------------
def test_absent_highlights_yields_empty():
    assert highlight_terms(None) == []


def test_no_em_span_yields_empty():
    assert highlight_terms("plain text with <b>bold</b> but no emphasis") == []
    assert highlight_terms(["a", {"k": "b"}, 7, None]) == []


def test_single_span_is_trimmed_and_tag_stripped():
    assert highlight_terms("see <em>  <b>city</b> lights  </em> here") == ["city lights"]


def test_entities_are_unescaped_after_tag_strip():
    # `&amp;` -> '&'; `&lt;` -> '<' (survives the tag strip because it carries no real '<'/'>').
    assert highlight_terms("<em>Tom &amp; Jerry</em>") == ["Tom & Jerry"]
    assert highlight_terms("<em>a &lt;b&gt; c</em>") == ["a <b> c"]


def test_em_tag_case_insensitive():
    assert highlight_terms("<EM>Loud</EM> and <Em>soft</Em>") == ["Loud", "soft"]


def test_distinct_under_casefold_keeps_first_occurrence():
    assert highlight_terms("<em>Cat</em> <em>CAT</em> <em>cat</em>") == ["Cat"]


def test_empty_after_processing_is_skipped():
    # Only whitespace / tags / an entity that becomes a trim char -> nothing survives.
    assert highlight_terms("<em>   </em><em><b></b></em><em>&nbsp;</em>") == []


def test_term_over_100_chars_is_dropped_boundary():
    kept = "k" * HIGHLIGHT_TERM_MAX_CHARS          # exactly 100 -> kept
    dropped = "d" * (HIGHLIGHT_TERM_MAX_CHARS + 1)  # 101 -> dropped
    terms = highlight_terms(f"<em>{kept}</em><em>{dropped}</em>")
    assert terms == [kept]


def test_cap_at_20_distinct_terms():
    spans = "".join(f"<em>term{i}</em>" for i in range(25))
    terms = highlight_terms(spans)
    assert terms == [f"term{i}" for i in range(HIGHLIGHT_TERMS_MAX)]


def test_nested_structure_collects_leaves_in_order():
    highlights = {
        "a": ["<em>one</em>", {"b": "<em>two</em>"}],
        "c": "<em>three</em>",
    }
    assert highlight_terms(highlights) == ["one", "two", "three"]


def test_non_string_leaves_are_ignored():
    highlights = [1, 2.5, True, None, "<em>only</em>", {"n": 9}]
    assert highlight_terms(highlights) == ["only"]


def test_depth_beyond_three_is_not_walked():
    # A list at depth 1 -> depth 2 -> depth 3 holds the string leaf (reachable); wrapping it one
    # level deeper puts the leaf below the walk limit, so nothing is collected.
    reachable = [[["<em>deep</em>"]]]           # string leaf sits at container depth 3
    assert highlight_terms(reachable) == ["deep"]
    unreachable = [[[["<em>toodeep</em>"]]]]     # one level deeper than the walk goes
    assert highlight_terms(unreachable) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
