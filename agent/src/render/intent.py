"""Recover the user's intent for an auto-render from conversation memory.

An artifact is auto-rendered by the render hook when a tool result comes back -- often on a
turn whose text is useless for intent ("retrieve the results and summarise"). The request that
actually explains the artifact ("give me the shot segments") was made turns earlier. This module
reads the last N *genuine human* turns from AgentCore Memory so the render agent can determine
what the user wanted and pick the right deliverable.

Measured on a real session (28df6f8f): 94 raw events -> 5 human turns. The event stream is ~95%
tool traffic and agent-state blobs (~19 events per turn), so a naive "last 15 events" is only
~2 turns and misses the intent. We therefore FILTER to role=user text messages (excluding
toolResult/toolUse) and PAGINATE until we have enough human turns or hit a scan cap. Bounded and
best-effort: any failure returns [] so the render still proceeds on the skill's default.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A single conversation turn expands to ~19 memory events (user msg, tool uses, tool results,
# assistant text, per-turn state blobs). Scanning 200 events collects ~10 human turns, which is
# comfortably more than we surface -- while staying bounded so a long session can't run away.
_DEFAULT_MAX_TURNS = 8
_MAX_EVENTS_SCANNED = 200
_PAGE_SIZE = 100
# Human text longer than this is truncated -- the render agent needs the ask, not an essay.
_MAX_TURN_CHARS = 500


def _human_text_from_event(event: dict) -> Optional[str]:
    """Genuine human text from one memory event, or None.

    A memory event's payload is a list; a conversational entry carries a JSON-stringified message
    ``{"message": {"role", "content": [...]}}``. We keep only role=user messages whose content is
    real text -- a user message that is purely a ``toolResult`` is the harness feeding a tool's
    output back to the model, not something the person typed, so it must not count as intent.
    """
    if not isinstance(event, dict):
        return None
    for part in event.get("payload") or []:
        if not isinstance(part, dict):
            continue
        conv = part.get("conversational")
        if not isinstance(conv, dict):
            continue
        text = (conv.get("content") or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            message = json.loads(text).get("message", {})
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        # A tool-result-only user message is harness plumbing, not a human ask.
        if any(isinstance(p, dict) and "toolResult" in p for p in content):
            continue
        pieces = [p["text"] for p in content
                  if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()]
        if pieces:
            return " ".join(pieces).strip()[:_MAX_TURN_CHARS]
    return None


def read_recent_user_turns(memory_client: Any, memory_id: str, actor_id: str, session_id: str,
                           *, max_turns: int = _DEFAULT_MAX_TURNS,
                           max_events: int = _MAX_EVENTS_SCANNED) -> list[str]:
    """Up to ``max_turns`` most-recent genuine human user turns, NEWEST FIRST.

    Best-effort: missing ids, no client, or any API failure returns []. The render path treats an
    empty list as "no intent signal" and falls back to the skill's primary deliverable.
    """
    if not (memory_client and memory_id and actor_id and session_id):
        return []
    turns: list[str] = []
    token: Optional[str] = None
    scanned = 0
    try:
        while scanned < max_events and len(turns) < max_turns:
            kwargs = {"memoryId": memory_id, "actorId": actor_id,
                      "sessionId": session_id, "maxResults": _PAGE_SIZE}
            if token:
                kwargs["nextToken"] = token
            resp = memory_client.list_events(**kwargs) or {}
            events = resp.get("events") or []
            if not events:
                break
            for event in events:
                scanned += 1
                human = _human_text_from_event(event)
                if human:
                    turns.append(human)
                    if len(turns) >= max_turns:
                        break
            token = resp.get("nextToken")
            if not token:
                break
    except Exception:  # noqa: BLE001 - intent recovery must never break a render
        logger.info("read_recent_user_turns: memory read failed (non-fatal)", exc_info=True)
        return turns
    return turns
