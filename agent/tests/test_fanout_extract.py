"""Tests for the dynamic fan-out collection extractor (Phase 0).

Covers the grammar in docs/dynamic-fanout-design.md §3.4 plus the loud-failure
contract: every unresolved path raises FanoutExtractError with a useful message,
and a legitimately empty collection returns [] (skip-vs-fail is the caller's call).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fanout_extract import FanoutExtractError, extract_collection, validate_path  # noqa: E402


# --- the real diarization shape this feature exists for ---------------------

DIARIZE = {
    "speakers": [
        {"id": "SPEAKER_00", "segments": 12},
        {"id": "SPEAKER_01", "segments": 8},
        {"id": "SPEAKER_02", "segments": 3},
    ],
    "duration_sec": 180.0,
}


def test_explode_to_scalar_field():
    assert extract_collection(DIARIZE, "speakers[*].id") == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]


def test_explode_to_elements():
    assert extract_collection(DIARIZE, "speakers[*]") == DIARIZE["speakers"]


def test_plain_key_returning_list():
    assert extract_collection(DIARIZE, "speakers") == DIARIZE["speakers"]


def test_nested_navigation_before_explode():
    value = {"result": {"analysis": {"scenes": [{"n": 1}, {"n": 2}]}}}
    assert extract_collection(value, "result.analysis.scenes[*].n") == [1, 2]


def test_nested_navigation_no_explode():
    value = {"a": {"b": ["x", "y", "z"]}}
    assert extract_collection(value, "a.b") == ["x", "y", "z"]


def test_empty_path_on_list_value():
    assert extract_collection(["a", "b"], "") == ["a", "b"]


def test_returned_list_is_a_copy():
    src = {"speakers": [1, 2, 3]}
    out = extract_collection(src, "speakers")
    out.append(4)
    assert src["speakers"] == [1, 2, 3]  # extractor must not alias the source


# --- empty collections are valid (skip-vs-fail is the caller's decision) ----

def test_empty_collection_returns_empty_list():
    assert extract_collection({"speakers": []}, "speakers") == []
    assert extract_collection({"speakers": []}, "speakers[*].id") == []
    assert extract_collection([], "") == []


# --- loud failures ----------------------------------------------------------

def test_missing_key_raises_with_available_keys():
    with pytest.raises(FanoutExtractError) as e:
        extract_collection(DIARIZE, "people[*].id")
    assert "people" in str(e.value)
    assert "speakers" in str(e.value)  # lists what WAS available


def test_terminal_not_a_list_raises():
    with pytest.raises(FanoutExtractError):
        extract_collection(DIARIZE, "duration_sec")


def test_explode_target_not_a_list_raises():
    with pytest.raises(FanoutExtractError):
        extract_collection({"speakers": {"id": "x"}}, "speakers[*].id")


def test_navigate_through_non_dict_raises():
    with pytest.raises(FanoutExtractError):
        extract_collection({"a": 5}, "a.b.c")


def test_empty_path_on_non_list_raises():
    with pytest.raises(FanoutExtractError):
        extract_collection({"speakers": []}, "")


def test_field_missing_on_some_element_raises():
    value = {"items": [{"id": "a"}, {"nope": "b"}]}
    with pytest.raises(FanoutExtractError):
        extract_collection(value, "items[*].id")


# --- static path validation (used by the Phase 1 validator) -----------------

def test_validate_rejects_double_explode():
    with pytest.raises(FanoutExtractError):
        validate_path("a[*].b[*].c")


def test_validate_rejects_empty_segments():
    with pytest.raises(FanoutExtractError):
        validate_path("a..b")


def test_validate_accepts_supported_paths():
    for p in ("", "speakers", "speakers[*]", "speakers[*].id", "a.b.c", "a.b[*].c.d"):
        validate_path(p)  # must not raise


def test_validate_rejects_none():
    with pytest.raises(FanoutExtractError):
        validate_path(None)  # type: ignore[arg-type]


# --- tolerant recovery from a prose-wrapped / multi-object producer result -----
# Regression for session 466addc8: the model prepended prose ("I'll start ...") and the
# captured node value was a DIFFERENT object ({"uris":[...]}), so a strict parse missed the
# real {"batches":[...]}. find_collection_in_text must recover it from the raw result text.
from fanout_extract import find_collection_in_text  # noqa: E402


def test_find_collection_recovers_prose_prefixed_json():
    text = 'I\'ll start by presigning the manifest URL.{"batches": [{"batch_id": 0}, {"batch_id": 1}]}'
    got = find_collection_in_text(text, "batches")
    assert got == [{"batch_id": 0}, {"batch_id": 1}]


def test_find_collection_picks_the_object_with_the_path_among_several():
    # A wrong object ({"uris":...}) appears BEFORE the right one — the 466addc8 shape.
    text = 'log line\n{"uris": ["a", "b"]}\nthen the real output:\n{"batches": [{"batch_id": 0}]}\ndone'
    assert find_collection_in_text(text, "batches") == [{"batch_id": 0}]


def test_find_collection_handles_fenced_json():
    text = "Here you go:\n```json\n{\"batches\": [{\"batch_id\": 0}, {\"batch_id\": 1}, {\"batch_id\": 2}]}\n```"
    assert len(find_collection_in_text(text, "batches")) == 3


def test_find_collection_is_string_aware_braces_in_values_dont_break_it():
    text = 'note {"batches": [{"caption": "a } b { c", "id": 0}]}'
    got = find_collection_in_text(text, "batches")
    assert got == [{"caption": "a } b { c", "id": 0}]


def test_find_collection_supports_explode_and_nested_path():
    text = 'prefix {"result": {"items": [{"v": 1}, {"v": 2}]}} suffix'
    assert find_collection_in_text(text, "result.items") == [{"v": 1}, {"v": 2}]
    assert find_collection_in_text(text, "result.items[*].v") == [1, 2]


def test_find_collection_raises_when_no_object_has_the_path():
    with pytest.raises(FanoutExtractError):
        find_collection_in_text('just prose, no json here', "batches")
    with pytest.raises(FanoutExtractError):
        find_collection_in_text('{"uris": [1,2]} {"other": 3}', "batches")
