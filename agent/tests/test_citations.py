"""Property tests for the Python Citation_Formatter / Citation_Parser (task 3.5).

Feature: media-harness-search-qa-rendering, Component 3.

Property 6 (Citation format and parse round trip), Python half. The TypeScript
twin lives in ``ui/src/components/chat/citations.property.test.ts`` (task 3.6).

Two statements, each its own test:

- ``test_round_trip``: for a Display_Name of 1-255 code points with no
  leading/trailing ``TRIM_CHARS`` and no ``[`` ``]`` ``@`` ``#`` CR or LF, and a
  whole-second ``start_ms <= end_ms`` below 100 hours, parsing
  ``prefix + format_citation(name, start, end) + suffix`` (prefix/suffix with no
  ``[`` or ``]``, suffix not starting with ``(``) returns exactly one Citation
  carrying that name/start/end whose ``text`` is the formatted string.
- ``test_format_shape``: for any ``0 <= start_ms <= end_ms`` (not necessarily
  whole seconds), ``format_citation`` writes ``floor(start_ms / 1000)`` and
  ``ceil(end_ms / 1000)`` in the Req 2.1 shape — one space on each side of
  ``@``, an en dash with no surrounding spaces, ``mm:ss`` below 3,600 s, and
  ``h:mm:ss`` with an unpadded ``h`` at or above 3,600 s.

Requirements: 2.1, 2.2, 2.4
"""

from __future__ import annotations

import math
import os
import sys

from hypothesis import assume, given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search.citations import (  # noqa: E402
    NAME_MAX,
    PAGE_CITATION_PATTERN,
    TRIM_CHARS,
    format_citation,
    format_timecode,
    parse_citations,
)

# One hundred hours in seconds / milliseconds. The grammar accepts an unpadded
# ``h`` of one or two digits, so a whole-second range strictly below 100 h round
# trips through the parser; a three-digit ``h`` would not.
_HUNDRED_HOURS_S = 100 * 3600
_HUNDRED_HOURS_MS = _HUNDRED_HOURS_S * 1000

_TRIM_SET = frozenset(TRIM_CHARS)

# Display_Name alphabet: any code point except the five characters the property
# forbids (``[`` ``]`` ``@`` ``#``), CR, LF, and surrogates. ``TRIM_CHARS`` may
# appear in the interior; the generator strips them from the ends afterwards so
# the "no leading/trailing TRIM_CHARS" condition always holds.
_NAME_ALPHABET = st.characters(
    min_codepoint=1,
    blacklist_characters="[]@#\r\n",
    blacklist_categories=("Cs",),
)


def _strip_trim(value: str) -> str:
    """Strip the explicit ``TRIM_CHARS`` set from both ends (reference impl)."""
    start, end = 0, len(value)
    while start < end and value[start] in _TRIM_SET:
        start += 1
    while end > start and value[end - 1] in _TRIM_SET:
        end -= 1
    return value[start:end]


@st.composite
def display_names(draw: st.DrawFn) -> str:
    """A Display_Name: 1-255 code points, no leading/trailing ``TRIM_CHARS``,
    and none of ``[`` ``]`` ``@`` ``#`` CR LF."""
    raw = draw(st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=255))
    name = _strip_trim(raw)
    assume(1 <= len(name) <= NAME_MAX)
    return name


# prefix / suffix carry no ``[`` or ``]`` so they introduce no second bracket;
# the suffix additionally must not start with ``(`` (the grammar rejects a
# ``]`` immediately followed by ``(`` — the markdown-link guard).
_AFFIX_ALPHABET = st.characters(blacklist_characters="[]", blacklist_categories=("Cs",))
_prefixes = st.text(alphabet=_AFFIX_ALPHABET, max_size=12)
_suffixes = st.text(alphabet=_AFFIX_ALPHABET, max_size=12).filter(
    lambda s: not s.startswith("(")
)


@st.composite
def whole_second_ranges(draw: st.DrawFn) -> tuple[int, int]:
    """``(start_ms, end_ms)`` on whole seconds, ``start <= end``, below 100 h."""
    start_s = draw(st.integers(min_value=0, max_value=_HUNDRED_HOURS_S - 1))
    end_s = draw(st.integers(min_value=start_s, max_value=_HUNDRED_HOURS_S - 1))
    return start_s * 1000, end_s * 1000


@st.composite
def ms_ranges(draw: st.DrawFn) -> tuple[int, int]:
    """``(start_ms, end_ms)`` at millisecond resolution, ``0 <= start <= end``."""
    start_ms = draw(st.integers(min_value=0, max_value=_HUNDRED_HOURS_MS))
    end_ms = draw(st.integers(min_value=start_ms, max_value=_HUNDRED_HOURS_MS))
    return start_ms, end_ms


def _ref_timecode(total_seconds: int) -> str:
    """Independent reference formatter for the Req 2.1 timecode shape."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if total_seconds < 3600:
        return f"{minutes:02d}:{seconds:02d}"
    return f"{hours}:{minutes:02d}:{seconds:02d}"


# Feature: media-harness-search-qa-rendering, Property 6: Citation format and parse round trip
@settings(max_examples=400)
@given(
    name=display_names(),
    rng=whole_second_ranges(),
    prefix=_prefixes,
    suffix=_suffixes,
)
def test_round_trip(name: str, rng: tuple[int, int], prefix: str, suffix: str) -> None:
    """Validates: Requirements 2.1, 2.2, 2.4

    For a well-formed name and whole-second range below 100 h, the formatted
    citation embedded between bracket-free affixes parses back to exactly one
    Citation with that name/start/end whose ``text`` is the formatted string.
    """
    start_ms, end_ms = rng
    formatted = format_citation(name, start_ms, end_ms)

    citations = parse_citations(prefix + formatted + suffix)

    assert len(citations) == 1
    (citation,) = citations
    assert citation.display_name == name
    assert citation.start_ms == start_ms
    assert citation.end_ms == end_ms
    assert citation.text == formatted


# Feature: media-harness-search-qa-rendering, Property 6: Citation format and parse round trip
@settings(max_examples=400)
@given(name=display_names(), rng=ms_ranges())
def test_format_shape(name: str, rng: tuple[int, int]) -> None:
    """Validates: Requirements 2.1, 2.2, 2.4

    For any ``0 <= start_ms <= end_ms``, ``format_citation`` writes
    ``floor(start_ms / 1000)`` and ``ceil(end_ms / 1000)`` seconds in the Req 2.1
    shape (one space each side of ``@``, en dash with no spaces, ``mm:ss`` below
    one hour, unpadded ``h:mm:ss`` from one hour).
    """
    start_ms, end_ms = rng
    start_s = start_ms // 1000
    end_s = math.ceil(end_ms / 1000)

    expected = f"[{name} @ {_ref_timecode(start_s)}\u2013{_ref_timecode(end_s)}]"

    assert format_citation(name, start_ms, end_ms) == expected


# ---------------------------------------------------------------------------
# Property 7: Malformed bracketed text stays plain (task 3.7), Python half.
#
# For any text built from valid Citations interleaved with bracketed candidates
# that break exactly one rule (a minute or second field of 60 or more, a
# one-digit mm, a three-digit h, a missing field, start later than end, a name
# empty after trimming, a name longer than 255 code points after trimming, or a
# closing ``]`` followed by ``(``), parse_citations returns no Citation for any
# malformed candidate, and the Citations it returns are exactly those returned
# for the same text with the malformed candidates removed (compared by text,
# display_name, start_ms and end_ms). The TypeScript half is task 3.8 in
# ui/src/components/chat/citations.property.test.ts.
#
# Requirements: 2.3
# ---------------------------------------------------------------------------

# Separators placed around every segment carry no bracket, so they introduce no
# new citation boundary, and no ``(`` so they can never suppress a following
# valid citation's ``]`` via the markdown-link guard. Surrogates are excluded so
# the strings are always encodable.
_SEP_ALPHABET = st.characters(blacklist_characters="[]()", blacklist_categories=("Cs",))
_separators = st.text(alphabet=_SEP_ALPHABET, max_size=8)

# Valid minute/second fields (00-59) and hour field (0-99) used to compose both
# the broken timecode and its valid companion. A constant valid companion keeps
# every malformed candidate breaking exactly one rule.
_FIELD = st.integers(min_value=0, max_value=59)
_HOUR = st.integers(min_value=0, max_value=99)
_GOOD_TC = "00:30"
_TRIM_ALPHABET = "".join(sorted(_TRIM_SET))


@st.composite
def _bad_candidate(draw: st.DrawFn) -> str:
    """One bracketed candidate that breaks exactly one Req 2.3 rule and therefore
    yields no Citation from ``parse_citations``."""
    name = draw(display_names())
    rule = draw(st.integers(min_value=0, max_value=7))

    if rule == 0:  # a minute or second field of 60 or more
        if draw(st.booleans()):
            minutes = draw(st.integers(min_value=60, max_value=99))
            bad_tc = f"{minutes}:{draw(_FIELD):02d}"
        else:
            seconds = draw(st.integers(min_value=60, max_value=99))
            bad_tc = f"{draw(_FIELD):02d}:{seconds}"
        return f"[{name} @ {bad_tc}\u2013{_GOOD_TC}]"

    if rule == 1:  # a one-digit ss (a one-digit mm, `m:ss`, is VALID since 2026-10-10 -- Req 2.2 amended)
        return f"[{name} @ {draw(_FIELD):02d}:{draw(st.integers(0, 9))}\u2013{_GOOD_TC}]"

    if rule == 2:  # a three-digit h
        hours = draw(st.integers(min_value=100, max_value=999))
        bad_tc = f"{hours}:{draw(_FIELD):02d}:{draw(_FIELD):02d}"
        return f"[{name} @ {bad_tc}\u2013{_GOOD_TC}]"

    if rule == 3:  # a missing field (a bare number, no colon)
        return f"[{name} @ {draw(_FIELD):02d}\u2013{_GOOD_TC}]"

    if rule == 4:  # start later than end (both valid whole-second timecodes)
        start_s = draw(st.integers(min_value=1, max_value=_HUNDRED_HOURS_S - 1))
        end_s = draw(st.integers(min_value=0, max_value=start_s - 1))
        return f"[{name} @ {format_timecode(start_s)}\u2013{format_timecode(end_s)}]"

    if rule == 5:  # a name empty after trimming
        trim_name = draw(st.text(alphabet=_TRIM_ALPHABET, min_size=1, max_size=4))
        return format_citation(trim_name, 0, 30_000)

    if rule == 6:  # a name longer than 255 code points after trimming
        return format_citation("a" * draw(st.integers(256, 300)), 0, 30_000)

    # rule == 7: a well-formed citation whose closing ``]`` is followed by ``(``
    start_ms, end_ms = draw(whole_second_ranges())
    return format_citation(name, start_ms, end_ms) + "("


@st.composite
def _interleaved(draw: st.DrawFn) -> tuple[str, str, list, list]:
    """Build ``(full, cleaned, expected, bad_texts)``: ``full`` interleaves valid
    citations and malformed candidates with bracket-free separators; ``cleaned``
    is the same text with every malformed candidate removed; ``expected`` is the
    reduced tuple of the valid citations in order; ``bad_texts`` are the removed
    candidate strings."""
    first_sep = draw(_separators)
    full = [first_sep]
    cleaned = [first_sep]
    expected: list = []
    bad_texts: list = []

    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        if draw(st.booleans()):
            name = draw(display_names())
            start_ms, end_ms = draw(whole_second_ranges())
            text = format_citation(name, start_ms, end_ms)
            full.append(text)
            cleaned.append(text)
            expected.append((text, name, start_ms, end_ms))
        else:
            candidate = draw(_bad_candidate())
            full.append(candidate)
            bad_texts.append(candidate)
        sep = draw(_separators)
        full.append(sep)
        cleaned.append(sep)

    return "".join(full), "".join(cleaned), expected, bad_texts


def _reduced(citations: list) -> list:
    """Each Citation reduced to the tuple Property 7 compares on."""
    return [(c.text, c.display_name, c.start_ms, c.end_ms) for c in citations]


# Feature: media-harness-search-qa-rendering, Property 7: Malformed bracketed text stays plain
@settings(max_examples=400)
@given(data=_interleaved())
def test_malformed_bracketed_text_stays_plain(
    data: tuple[str, str, list, list],
) -> None:
    """Validates: Requirements 2.3

    Malformed bracketed candidates produce no Citation, and the Citations parsed
    from the full text are exactly those parsed from the same text with every
    malformed candidate removed.
    """
    full, cleaned, expected, bad_texts = data

    cleaned_citations = _reduced(parse_citations(cleaned))
    full_citations = _reduced(parse_citations(full))

    # The valid citations survive on their own, in order.
    assert cleaned_citations == expected
    # The malformed candidates add nothing: full parses to exactly the cleaned set.
    assert full_citations == cleaned_citations

    # No malformed candidate was returned as a Citation.
    returned_texts = {text for text, _, _, _ in full_citations}
    for candidate in bad_texts:
        assert candidate not in returned_texts


# ---------------------------------------------------------------------------
# Property 8: Python and TypeScript parsers agree (task 3.9), Python half.
#
# The TypeScript half is ui/src/components/chat/citations.test.ts. Because
# cross-running Python from a TS runner is impractical, agreement is proved
# through two committed artifacts both languages read:
#
#   * infra/lib/projects/fixtures/citation-cases.json  -- the `parse` cases,
#     each carrying its expected `citations`;
#   * infra/lib/projects/fixtures/citation-corpus.json -- 2,000 input strings
#     (task 3.4), inputs only;
#   * infra/lib/projects/fixtures/citation-parse-expected.json -- the committed
#     Python parse results for the fixture `parse` inputs AND the corpus inputs,
#     generated here from parse_citations (this module below).
#
# This Python suite parses both input sets, asserts CITATION_PATTERN is
# byte-identical to the fixture `pattern`, and compares its results against the
# committed artifact (regenerating it on MH_WRITE_CITATION_ARTIFACT=1). The TS
# suite parses the same inputs and compares against the SAME artifact. Both
# parsers matching one Python-produced artifact proves the two parsers equal.
#
# `test_corpus_in_sync` additionally re-runs agent/scripts/gen_citation_corpus.py
# in memory so the committed corpus cannot drift from its generator.
#
# Requirements: 2.2, 2.3, 2.5
# ---------------------------------------------------------------------------

import json  # noqa: E402

from project_search.citations import CITATION_PATTERN  # noqa: E402

# agent/scripts holds the (seeded) corpus generator; import it like the parser.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gen_citation_corpus  # noqa: E402

_FIXTURES_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "infra", "lib", "projects", "fixtures"
    )
)
_CASES_PATH = os.path.join(_FIXTURES_DIR, "citation-cases.json")
_CORPUS_PATH = os.path.join(_FIXTURES_DIR, "citation-corpus.json")
_EXPECTED_PATH = os.path.join(_FIXTURES_DIR, "citation-parse-expected.json")


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _reduce(text: str) -> list:
    """parse_citations(text) reduced to the cross-language tuple of Property 8:
    [display_name, start_ms, end_ms, text]. Span offsets are omitted because
    Python counts code points and JS counts UTF-16 code units, which differ for
    astral characters."""
    return [
        [c.display_name, c.start_ms, c.end_ms, c.text] for c in parse_citations(text)
    ]


def _build_expected() -> dict:
    """The committed Python-results artifact, computed from parse_citations over
    the fixture `parse` inputs and the corpus inputs (both in file order)."""
    cases = _load_json(_CASES_PATH)
    corpus = _load_json(_CORPUS_PATH)
    return {
        "description": (
            "Committed Python parse_citations results for Property 8 "
            "(media-harness-search-qa-rendering task 3.9). Generated from "
            "agent/src/project_search/citations.py over the `parse` inputs of "
            "citation-cases.json and the `inputs` of citation-corpus.json, in "
            "file order. The Python test (agent/tests/test_citations.py) and the "
            "TypeScript test (ui/src/components/chat/citations.test.ts) each parse "
            "the same inputs and assert equality to this artifact, proving the two "
            "parsers agree. Each result is [display_name, start_ms, end_ms, text]. "
            "Regenerate with: MH_WRITE_CITATION_ARTIFACT=1 pytest "
            "agent/tests/test_citations.py -k corpus_parse_artifact"
        ),
        "pattern": CITATION_PATTERN,
        "pagePattern": PAGE_CITATION_PATTERN,
        "fixtureParse": [_reduce(case["input"]) for case in cases["parse"]],
        "corpus": [_reduce(text) for text in corpus["inputs"]],
    }


# Feature: media-harness-search-qa-rendering, Property 8: Python and TypeScript parsers agree
def test_pattern_matches_fixture() -> None:
    """Validates: Requirements 2.2

    CITATION_PATTERN is byte-identical to the fixture `pattern`. The TS suite
    asserts the same for its own constant, so both grammars are the one fixture
    grammar.
    """
    cases = _load_json(_CASES_PATH)
    assert CITATION_PATTERN == cases["pattern"]
    assert PAGE_CITATION_PATTERN == cases["pagePattern"]


# Feature: media-harness-search-qa-rendering, Property 8: Python and TypeScript parsers agree
def test_fixture_parse_cases() -> None:
    """Validates: Requirements 2.2, 2.3

    parse_citations over each fixture `parse` input yields exactly the case's
    recorded citations (reduced to the cross-language tuple).
    """
    cases = _load_json(_CASES_PATH)
    for case in cases["parse"]:
        expected = [
            [c["display_name"], c["start_ms"], c["end_ms"], c["text"]]
            for c in case["citations"]
        ]
        assert _reduce(case["input"]) == expected, case["input"]


# Feature: media-harness-search-qa-rendering, Property 8: Python and TypeScript parsers agree
def test_corpus_in_sync() -> None:
    """Validates: Requirements 2.2, 2.3

    The committed corpus is exactly what agent/scripts/gen_citation_corpus.py
    produces for its pinned seed, so it cannot drift from its generator.
    """
    committed = _load_json(_CORPUS_PATH)
    regenerated = gen_citation_corpus.build_document()
    assert committed == regenerated, (
        "infra/lib/projects/fixtures/citation-corpus.json is out of sync with its "
        "generator; regenerate with: python3 agent/scripts/gen_citation_corpus.py"
    )


# Feature: media-harness-search-qa-rendering, Property 8: Python and TypeScript parsers agree
def test_corpus_parse_artifact() -> None:
    """Validates: Requirements 2.2, 2.3, 2.5

    The committed Python-results artifact equals what parse_citations produces
    now for the fixture `parse` inputs and the corpus inputs. The TS suite
    compares against this same artifact, so the two parsers are proven equal.
    Set MH_WRITE_CITATION_ARTIFACT=1 to (re)write the artifact.
    """
    expected = _build_expected()

    if os.environ.get("MH_WRITE_CITATION_ARTIFACT") == "1":
        with open(_EXPECTED_PATH, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(expected, ensure_ascii=False, indent=1) + "\n")

    committed = _load_json(_EXPECTED_PATH)
    assert committed["pattern"] == expected["pattern"]
    assert committed["pagePattern"] == expected["pagePattern"]
    assert committed["fixtureParse"] == expected["fixtureParse"]
    assert committed["corpus"] == expected["corpus"]

    # Cross-check: the fixture `parse` cases' own expected citations agree with
    # the artifact, so the three committed files describe one grammar.
    cases = _load_json(_CASES_PATH)
    fixture_expected = [
        [
            [c["display_name"], c["start_ms"], c["end_ms"], c["text"]]
            for c in case["citations"]
        ]
        for case in cases["parse"]
    ]
    assert committed["fixtureParse"] == fixture_expected
