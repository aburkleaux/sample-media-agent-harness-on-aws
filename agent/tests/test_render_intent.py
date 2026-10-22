"""Tests for render_intent.read_recent_user_turns.

Shapes mirror real AgentCore Memory events observed live on session 28df6f8f: conversational
entries carry a JSON-stringified message; the stream is dominated by tool traffic and state
blobs (~19 events per human turn), so filtering + pagination are the whole point.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render.intent as ri  # noqa: E402


def _conv(role: str, parts: list) -> dict:
    return {"payload": [{"conversational": {"content": {
        "text": json.dumps({"message": {"role": role, "content": parts}})}}}]}


def _user_text(t: str) -> dict:
    return _conv("user", [{"text": t}])


def _user_toolresult() -> dict:
    return _conv("user", [{"toolResult": {"toolUseId": "x", "content": [{"text": "output"}]}}])


def _assistant(t: str) -> dict:
    return _conv("assistant", [{"text": t}])


def _state_blob() -> dict:
    return {"payload": [{"blob": '{"agent_id": "default", "state": {"agent_skills": {}}}'}]}


class _FakeMemory:
    def __init__(self, pages: list[list[dict]]):
        self._pages = pages

    def list_events(self, **kwargs):
        tok = kwargs.get("nextToken")
        idx = int(tok) if tok else 0
        page = self._pages[idx] if idx < len(self._pages) else []
        nxt = str(idx + 1) if idx + 1 < len(self._pages) else None
        return {"events": page, **({"nextToken": nxt} if nxt else {})}


def test_extracts_only_genuine_human_turns_newest_first():
    events = [
        _assistant("All three jobs completed."),
        _user_toolresult(),
        _user_text("Retrieve the results and summarise."),
        _state_blob(),
        _user_text("now take the proxy and give me the shot segment"),
        _user_toolresult(),
    ]
    turns = ri.read_recent_user_turns(_FakeMemory([events]), "m", "a", "s")
    assert turns == ["Retrieve the results and summarise.",
                     "now take the proxy and give me the shot segment"]


def test_tool_result_only_user_messages_are_not_intent():
    turns = ri.read_recent_user_turns(_FakeMemory([[_user_toolresult(), _user_toolresult()]]),
                                      "m", "a", "s")
    assert turns == []


def test_paginates_until_enough_human_turns():
    # Each page is mostly noise (like the real ~19:1 ratio); the intent is on page 3.
    noise = [_assistant("working"), _user_toolresult(), _state_blob()]
    pages = [noise, noise, [_user_text("give me the shot segments")] + noise]
    turns = ri.read_recent_user_turns(_FakeMemory(pages), "m", "a", "s", max_turns=1)
    assert turns == ["give me the shot segments"]


def test_respects_max_turns():
    events = [_user_text(f"ask {i}") for i in range(10)]
    turns = ri.read_recent_user_turns(_FakeMemory([events]), "m", "a", "s", max_turns=3)
    assert turns == ["ask 0", "ask 1", "ask 2"]


def test_scan_cap_stops_runaway():
    # 5 pages of pure noise, cap at 200 events -> stops, returns [].
    noise_page = [_state_blob() for _ in range(100)]
    turns = ri.read_recent_user_turns(_FakeMemory([noise_page] * 5), "m", "a", "s")
    assert turns == []


def test_missing_ids_or_client_return_empty():
    assert ri.read_recent_user_turns(None, "m", "a", "s") == []
    assert ri.read_recent_user_turns(_FakeMemory([]), "", "a", "s") == []
    assert ri.read_recent_user_turns(_FakeMemory([]), "m", "", "s") == []


def test_api_failure_is_non_fatal():
    class Boom:
        def list_events(self, **k):
            raise RuntimeError("denied")
    assert ri.read_recent_user_turns(Boom(), "m", "a", "s") == []


def test_current_date_and_file_boilerplate_kept_as_is():
    turns = ri.read_recent_user_turns(
        _FakeMemory([[_user_text("give me a 480p proxy  [Current date: 2026-08-10]")]]),
        "m", "a", "s")
    assert turns and "480p proxy" in turns[0]
