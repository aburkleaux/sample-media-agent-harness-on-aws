"""Helpers for converting Strands tool events into useful step labels.

The orchestrator's HTTP stream surfaces step events as
``{"type":"step","status":"...","label":"..."}`` JSON. Without a label
helper, every gateway tool, sub-agent dispatch, and skill activation comes
through as a generic ``"Using <tool>..."`` line — useless to the user
because the *what* (which skill, which sub-agent task, which gateway tool)
is locked inside the tool's input dict.

This module owns three things:

1. The static ``TOOL_LABELS`` map (legacy fallback labels by tool name).
2. ``label_for_tool_use(tool_info, status)`` — the new entry point. It
   reads the tool's name + input dict and returns a label that mentions
   the actual subject of the call: ``"Activating skill: speech-diarization"``,
   ``"Dispatching: extract audio + start_speech_diarization"``, etc.
3. ``label_for_tool_done(tool_info, result, duration_ms)`` — same idea on
   completion, with success/failed differentiation. When the tool returned
   ``status="error"`` we emit a failed step with a short error preview so
   the user can see *which* call failed instead of seeing it silently
   close out as ``done``.

Kept dependency-free so ``main.py`` can import it without dragging in
boto3/strands modules.
"""
from __future__ import annotations

from typing import Any


# Legacy short labels keyed by tool name. Used as a fallback when the new
# context-aware label_for_tool_use can't find anything richer to say.
TOOL_LABELS: dict[str, str] = {
    "code_interpreter": "Running code...",
    "file_read": "Reading file...",
    "artifact": "Managing artifacts...",
    "skills": "Activating skill...",
    "dispatch_tool": "Dispatching to sub-agent...",
    "plan_and_execute_tool": "Planning and executing...",
}


def coerce_input(tool_info: dict) -> dict:
    """Strands wraps the tool input under different keys in different
    versions of the SDK. Normalize to a single dict.

    Public so callers in main.py can compare two tool_info snapshots
    when the same toolUseId arrives via multiple ``current_tool_use``
    events (input is assembled from streaming deltas; later events
    carry more keys than earlier ones).
    """
    raw = tool_info.get("input")
    if isinstance(raw, dict):
        return raw
    # Some streams nest input under "tool_use_input" or pass an opaque str
    raw = tool_info.get("tool_use_input")
    if isinstance(raw, dict):
        return raw
    return {}


# Internal alias kept so existing helpers in this module don't churn.
_coerce_input = coerce_input


def _truncate(text: str, limit: int = 60) -> str:
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _short_gateway_name(name: str) -> str:
    """Drop MCP gateway/target prefixes (`gateway___target___tool`) so the
    user sees a concise tool name without the routing breadcrumbs."""
    if "___" in name:
        return name.rsplit("___", 1)[-1]
    return name


# Public alias — callers outside this module (task_agent, dispatch_tool)
# also need to strip MCP prefixes before emitting step events.
short_tool_name = _short_gateway_name


def has_input_payload(tool_info: dict | None) -> bool:
    """Return True when the tool_info dict carries any usable input keys.

    Strands streams the tool name first and only later assembles the input
    dict from incremental deltas. Emitting a step on the very first
    ``current_tool_use`` event therefore produces a generic
    ``"Activating skill..."`` label because ``input`` is still empty.

    Callers should defer the running-step emit until this returns True so
    the label benefits from the full input context.
    """
    if not isinstance(tool_info, dict):
        return False
    inp = _coerce_input(tool_info)
    return bool(inp)


def label_for_tool_use(tool_info: dict | None) -> str:
    """Build a 'running' label for a tool-use event.

    Falls back to the legacy ``TOOL_LABELS`` entry (or ``"Using <name>..."``)
    when the tool's input doesn't supply usable context.
    """
    if not isinstance(tool_info, dict):
        return "Working..."

    name = tool_info.get("name") or ""
    if not name:
        return "Working..."

    inp = _coerce_input(tool_info)

    # --- Per-tool enrichment ------------------------------------------------
    # The orchestrator fires ``skills`` to activate a skill by name. The
    # legacy fallback says "Activating skill..." — we add the actual name.
    if name == "skills":
        skill_name = inp.get("name") or inp.get("skill") or ""
        if skill_name:
            return f"Activating skill: {skill_name}"
        return TOOL_LABELS["skills"]

    # ``dispatch_tool`` ships a ``tools=[...]`` list (gateway tool names)
    # and a ``prompt`` (the task the sub-agent should run). Surface the
    # task summary if available; otherwise fall back to the tool list.
    if name == "dispatch_tool":
        prompt = inp.get("prompt") or ""
        tools = inp.get("tools") or []
        short_tools = [_short_gateway_name(t) for t in tools if isinstance(t, str)]
        if prompt:
            return f"Dispatching: {_truncate(prompt, 60)}"
        if short_tools:
            joined = ", ".join(short_tools[:3])
            tail = f" +{len(short_tools) - 3} more" if len(short_tools) > 3 else ""
            return f"Dispatching with tools: {joined}{tail}"
        return TOOL_LABELS["dispatch_tool"]

    # ``plan_and_execute_tool`` carries a ``goal`` string. Show the first
    # ~60 chars so the user sees what the planner is being asked to do.
    if name == "plan_and_execute_tool":
        goal = inp.get("goal") or ""
        if goal:
            return f"Planning and executing: {_truncate(goal, 60)}"
        return TOOL_LABELS["plan_and_execute_tool"]

    # ``code_interpreter`` has many actions; the most useful is the
    # ``code`` snippet's first line.
    if name == "code_interpreter":
        action = inp.get("action") or {}
        if isinstance(action, dict):
            code = action.get("code") or ""
            if code:
                first_line = code.splitlines()[0].strip() if code.strip() else ""
                if first_line:
                    return f"Running code: {_truncate(first_line, 50)}"
        return TOOL_LABELS["code_interpreter"]

    # ``file_read`` carries a ``path`` (or ``paths``). Show the basename.
    if name == "file_read":
        path = inp.get("path") or ""
        if isinstance(path, list):
            path = path[0] if path else ""
        if isinstance(path, str) and path:
            base = path.rsplit("/", 1)[-1]
            return f"Reading file: {base}"
        return TOOL_LABELS["file_read"]

    # ``http_request`` carries ``method`` + ``url``.
    if name == "http_request":
        method = (inp.get("method") or "GET").upper()
        url = inp.get("url") or ""
        if url:
            # Strip scheme + trailing path noise for brevity
            host = url.split("://", 1)[-1].split("/", 1)[0]
            return f"HTTP {method} {host}"
        return f"Making HTTP {method} request..."

    # ``artifact`` has a verb (upload/download/list).
    if name == "artifact":
        action = (inp.get("action") or "").lower()
        key = inp.get("key") or ""
        if action and key:
            return f"Artifact {action}: {key.rsplit('/', 1)[-1]}"
        if action:
            return f"Artifact {action}..."
        return TOOL_LABELS["artifact"]

    # Lab CRUD tools follow a *_draft naming pattern. Hide the operation
    # spam by collapsing to a short verb.
    if name.endswith("_draft") or name in {
        "publish_asset", "edit_asset", "discard_changes",
        "list_asset_versions", "list_my_published_assets", "update_skill_md",
    }:
        # e.g. "create_skill_draft" -> "create skill draft"
        readable = name.replace("_", " ")
        return f"Lab: {readable}"

    # --- Gateway tools ------------------------------------------------------
    # Anything matching "<gateway>___<target>___<tool>" is an MCP gateway
    # call. Drop the prefixes for the user-facing label.
    if "___" in name:
        return f"Calling {_short_gateway_name(name)}..."

    # --- Fallback -----------------------------------------------------------
    return TOOL_LABELS.get(name, f"Using {name}...")


def label_for_tool_done(
    tool_info: dict | None,
    result: Any = None,
    error_text: str | None = None,
) -> tuple[str, str]:
    """Return ``(status, label)`` for a tool's terminal step event.

    ``status`` is ``"done"`` on success and ``"failed"`` on error — this
    is what lets the UI render a different icon for failed dispatches.
    ``label`` mirrors the running label but with the past tense subject
    plus an error preview when applicable.

    Strands tool results are dicts shaped like::

        {"toolUseId": ..., "status": "success" | "error",
         "content": [{"text": "..."}]}

    The caller may pass ``result`` (the full dict) or just ``error_text``.
    Either is fine.
    """
    base = label_for_tool_use(tool_info).rstrip(".…").rstrip()

    # Determine error state. ``result`` wins if it's a dict; otherwise
    # ``error_text`` is the explicit signal.
    is_error = False
    err_preview = ""
    if isinstance(result, dict):
        if result.get("status") == "error":
            is_error = True
            content = result.get("content") or []
            if content and isinstance(content, list):
                first = content[0]
                if isinstance(first, dict):
                    err_preview = _truncate(first.get("text", ""), 80)
    if error_text:
        is_error = True
        err_preview = err_preview or _truncate(error_text, 80)

    if is_error:
        # Strip leading verbs like "Activating", "Dispatching", "Calling"
        # so the failed message reads cleanly: "Failed: speech-diarization"
        if err_preview:
            return "failed", f"{base} — failed: {err_preview}"
        return "failed", f"{base} — failed"

    return "done", base
