"""Tests for bare-tool-name extraction in the Capability Resolver (task 3.1 / 3.2).

Covers the pure ``compute_bare_tool_name`` helper:
- Property 2: the computed bare name equals the substring after the FINAL ``___``.
- Unit examples: multi-segment prefixes, separator-less negatives, empty/absent.

Requirements: 3.2
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.capability_resolver import compute_bare_tool_name

_SEP = "___"

# Alnum words only, so a token joined by single underscores can never contain
# ``___`` and never leads/trails with ``_`` — keeping the ``<target>___<bare>``
# boundary an exact triple-underscore separator.
_word = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=8,
)


@st.composite
def _token(draw: st.DrawFn) -> str:
    """A realistic tool/target token: alnum words joined by single underscores."""
    words = draw(st.lists(_word, min_size=1, max_size=4))
    return "_".join(words)


@st.composite
def gateway_tool_names(draw: st.DrawFn) -> tuple[str, str | None]:
    """Generate `(tool_name, expected_bare)` pairs.

    Emits valid ``<target>___<bare>`` names (expected = the bare token) and
    separator-less negatives (expected = ``None``).
    """
    if draw(st.booleans()):
        target = draw(_token())
        bare = draw(_token())
        return f"{target}{_SEP}{bare}", bare
    # Separator-less negative: a plain token can never contain ``___``.
    return draw(_token()), None


# ---------------------------------------------------------------------------
# Property 2: Bare tool name extraction
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 2: Bare tool name extraction
@settings(max_examples=200)
@given(gateway_tool_names())
def test_bare_tool_name_extraction(case: tuple[str, str | None]) -> None:
    """Validates: Requirements 3.2

    For any gateway tool name of the form ``<target>___<bare>``, the computed
    bare tool name equals the substring following the final ``___`` separator;
    a separator-less name yields ``None`` (unpublished).
    """
    tool_name, expected = case
    assert compute_bare_tool_name(tool_name) == expected


# ---------------------------------------------------------------------------
# Unit examples / edge cases
# ---------------------------------------------------------------------------


def test_simple_valid_name():
    assert compute_bare_tool_name("audio-stem___separate_audio_stems") == "separate_audio_stems"


def test_multi_segment_prefix_uses_final_separator():
    # Only the FINAL separator delimits the bare name.
    assert compute_bare_tool_name("a___b___separate_audio_stems") == "separate_audio_stems"


def test_none_is_unpublished():
    assert compute_bare_tool_name(None) is None


def test_empty_string_is_unpublished():
    assert compute_bare_tool_name("") is None


def test_separator_less_name_is_unpublished():
    assert compute_bare_tool_name("separate_audio_stems") is None


def test_empty_suffix_is_unpublished():
    # A trailing separator leaves no bare name -> malformed / unpublished.
    assert compute_bare_tool_name("audio-stem___") is None
