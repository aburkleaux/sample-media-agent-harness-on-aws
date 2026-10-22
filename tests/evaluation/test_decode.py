"""Unit tests for the memory-event decoder (tests/evaluation/decode.py)."""
import json

from tests.evaluation.decode import (
    decode_event_text,
    events_to_turns,
    observed_trajectory,
    user_turns,
)


def _envelope(role: str, blocks: list[dict]) -> str:
    return json.dumps({"message": {"role": role, "content": blocks}, "message_id": "m1"})


def _event(role: str, text: str) -> dict:
    return {"payload": [{"conversational": {"role": role, "content": {"text": text}}}]}


class TestDecodeEventText:
    def test_raw_text_passthrough(self):
        assert decode_event_text("just a plain prompt") == ("just a plain prompt", [])

    def test_envelope_text_only(self):
        env = _envelope("user", [{"text": "Render this video"}])
        assert decode_event_text(env) == ("Render this video", [])

    def test_envelope_text_and_tooluse(self):
        env = _envelope("assistant", [
            {"text": "On it."},
            {"toolUse": {"name": "dispatch_tool", "toolUseId": "t1", "input": {}}},
        ])
        text, tools = decode_event_text(env)
        assert text == "On it."
        assert tools == ["dispatch_tool"]

    def test_envelope_tool_only_has_no_text(self):
        env = _envelope("assistant", [{"toolUse": {"name": "get_job"}}])
        text, tools = decode_event_text(env)
        assert text == ""
        assert tools == ["get_job"]

    def test_multiple_tools_preserve_order(self):
        env = _envelope("assistant", [
            {"toolUse": {"name": "dispatch_tool"}},
            {"toolUse": {"name": "list_my_jobs"}},
            {"toolUse": {"name": "get_job"}},
        ])
        _text, tools = decode_event_text(env)
        assert tools == ["dispatch_tool", "list_my_jobs", "get_job"]

    def test_empty_returns_empty(self):
        assert decode_event_text("") == ("", [])
        assert decode_event_text("   ") == ("", [])


class TestEventsToTurns:
    def test_orders_and_drops_empty(self):
        events = [
            _event("user", "Analyze https://x/video.mp4"),
            _event("assistant", _envelope("assistant", [
                {"text": "Rendering."},
                {"toolUse": {"name": "dispatch_tool"}},
            ])),
            _event("assistant", _envelope("assistant", [{"toolUse": {"name": "get_job"}}])),
        ]
        turns = events_to_turns(events)
        assert [t["role"] for t in turns] == ["user", "assistant", "assistant"]
        assert turns[0]["text"] == "Analyze https://x/video.mp4"
        assert turns[1]["tools"] == ["dispatch_tool"]
        # tool-only assistant turn kept (empty text) because it carries a tool
        assert turns[2]["text"] == "" and turns[2]["tools"] == ["get_job"]

    def test_role_normalization(self):
        turns = events_to_turns([_event("USER", "hi"), _event("ASSISTANT", "hello")])
        assert [t["role"] for t in turns] == ["user", "assistant"]

    def test_observed_trajectory_flattens_in_order(self):
        events = [
            _event("user", "go"),
            _event("assistant", _envelope("assistant", [{"toolUse": {"name": "dispatch_tool"}}])),
            _event("assistant", _envelope("assistant", [
                {"toolUse": {"name": "list_my_jobs"}},
                {"toolUse": {"name": "get_job"}},
            ])),
        ]
        turns = events_to_turns(events)
        assert observed_trajectory(turns) == ["dispatch_tool", "list_my_jobs", "get_job"]

    def test_user_turns_extracts_prompts(self):
        events = [
            _event("user", "first prompt"),
            _event("assistant", "some answer"),
            _event("user", "second prompt"),
        ]
        assert user_turns(events_to_turns(events)) == ["first prompt", "second prompt"]
