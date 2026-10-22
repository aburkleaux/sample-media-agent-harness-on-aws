"""Decode AgentCore Memory events into conversation turns + tool trajectory.

This is the Python port of the UI's ``memoryEventsToChatMessages`` (ui/src/
lab-api.ts). AgentCore Memory stores each turn's conversational ``content.text``
in one of two shapes:

1. **Strands SessionMessage envelope** (current): ``text`` is
   ``json.dumps({"message": {"role": ..., "content": [{"text": ...}, {"toolUse": {...}}, ...]}, ...})``.
   We concatenate the text blocks (user-visible content) and, for assistant
   turns, collect ``toolUse`` names in order — the observed tool trajectory.
2. **Legacy raw text**: ``text`` is the bare user/assistant string.

Kept dependency-free so it is unit-testable without AWS.
"""
from __future__ import annotations

import json
from typing import Any


def decode_event_text(raw_text: str) -> tuple[str, list[str]]:
    """Return ``(visible_text, tool_names)`` for one conversational payload.

    ``visible_text`` is the concatenated text blocks (empty if the message was
    purely tool-use/tool-result). ``tool_names`` are the ordered ``toolUse``
    names found in the message (empty for user turns / raw text).
    """
    if not raw_text or not raw_text.strip():
        return "", []

    try:
        obj: Any = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        # Legacy raw text — surface as-is.
        return raw_text.strip(), []

    # Strands SessionMessage envelope.
    if isinstance(obj, dict) and isinstance(obj.get("message"), dict):
        msg = obj["message"]
        content = msg.get("content") or []
        texts: list[str] = []
        tools: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text_block = block.get("text")
                if isinstance(text_block, str) and text_block.strip():
                    texts.append(text_block)
                tool_use = block.get("toolUse")
                if isinstance(tool_use, dict) and tool_use.get("name"):
                    tools.append(str(tool_use["name"]))
        return "\n".join(texts).strip(), tools

    # A JSON string that isn't an envelope.
    if isinstance(obj, str):
        return obj.strip(), []

    return raw_text.strip(), []


def _normalize_role(role: str) -> str:
    r = (role or "").lower()
    if r in ("user", "human"):
        return "user"
    if r in ("assistant", "ai", "agent"):
        return "assistant"
    return r


def events_to_turns(events: list[dict]) -> list[dict]:
    """Convert Memory events (chronological) into ordered turns.

    Each returned turn is ``{"role", "text", "tools"}``. Empty messages (e.g.
    an envelope carrying only tool-use blocks and no text) are dropped for the
    visible transcript, but their tool names are preserved on the turn so the
    trajectory is not lost.
    """
    turns: list[dict] = []
    for ev in events:
        payload = ev.get("payload") or []
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            conv = item.get("conversational")
            if not isinstance(conv, dict):
                continue
            role = _normalize_role(conv.get("role", ""))
            content = conv.get("content") or {}
            raw = content.get("text", "") if isinstance(content, dict) else ""
            text, tools = decode_event_text(raw)
            if not text and not tools:
                continue
            turns.append({"role": role, "text": text, "tools": tools})
    return turns


def observed_trajectory(turns: list[dict]) -> list[str]:
    """Flatten the ordered tool names across all turns."""
    traj: list[str] = []
    for turn in turns:
        traj.extend(turn.get("tools") or [])
    return traj


def user_turns(turns: list[dict]) -> list[str]:
    """Return the ordered user-visible prompts (the scenario turn inputs)."""
    return [t["text"] for t in turns if t.get("role") == "user" and t.get("text")]
