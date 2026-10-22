"""Plan-node fast path: execute a fully specified single tool call without a model loop.

A ``kind: "tool"`` DAG node names ONE gateway call with complete arguments (``config.tool`` +
``config.arguments``); the Plan Chainer forwards it as ``payload["tool_call"]`` only when nothing
can make those arguments stale (no dependencies, no external inputs, not a retry). Running a model
to arrive at a call that is already written down cost each node two model calls (staging plan
81d9198e: every shot node made two Opus calls around one fal submit).

The call goes through the tool's own ``stream`` — the exact path Strands' executor uses — so every
wrap still applies: argument normalisation, the off-plan-endpoint and elided-url refusals, async
job registration, media mirroring. The node's output is the tool result as the ``json`` block the
model is instructed to end with, so everything after it (structured-output extraction, the
declared-output contract, ``_all_tool_calls_failed``, queue-handle/SFN registration, the plan-state
and manifest writes) runs unchanged.

Whenever the call cannot be made as written — the tool is not bound, the arguments do not fit its
schema, the call raises, or the result is an error — the caller falls back to the ordinary model
turn on the node's prompt, i.e. exactly today's behaviour.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _tool_names(tool) -> set[str]:
    names = set()
    for n in (getattr(tool, "tool_name", None),
              getattr(getattr(tool, "mcp_tool", None), "name", None)):
        if isinstance(n, str) and n:
            names.add(n)
    return names


def _bare(name: str) -> str:
    return name.rsplit("___", 1)[-1]


def _is_mcp_tool(tool) -> bool:
    try:
        from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
    except Exception:  # noqa: BLE001
        return False
    return isinstance(tool, MCPAgentTool)


def arguments_fit_schema(tool, arguments: dict) -> str:
    """"" when ``arguments`` plausibly fit the tool's input schema, else the reason they do not.

    Deliberately shallow (required keys, and unknown keys only when the schema forbids them): it
    only has to catch a plan that guessed the argument NAMES, so the node falls back to a model
    that reads the real schema instead of spending a call on a certain rejection.
    """
    spec = getattr(tool, "tool_spec", None) or {}
    schema = ((spec.get("inputSchema") or {}).get("json")) if isinstance(spec, dict) else None
    if not isinstance(schema, dict):
        return ""
    required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    missing = [r for r in required if r not in arguments]
    if missing:
        return f"missing required argument(s) {missing}"
    props = schema.get("properties")
    if isinstance(props, dict) and schema.get("additionalProperties") is False:
        unknown = [k for k in arguments if k not in props]
        if unknown:
            return f"unknown argument(s) {unknown}"
    return ""


def resolve_fast_path_call(tool_call, tools_list, resolved_tools) -> Optional[tuple[Any, dict]]:
    """``(tool, arguments)`` when the node's planned call can be made directly, else None."""
    if not isinstance(tool_call, dict):
        return None
    name, arguments = tool_call.get("name"), tool_call.get("arguments")
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        return None
    if name not in (tools_list or []):
        logger.info("fast path: %r is not one of the node's tools; using the model turn", name)
        return None
    exact = [t for t in (resolved_tools or []) if name in _tool_names(t)]
    by_bare = [t for t in (resolved_tools or [])
               if any(_bare(n) == _bare(name) for n in _tool_names(t))]
    candidates = exact or by_bare
    if len(candidates) != 1 or not _is_mcp_tool(candidates[0]):
        logger.info("fast path: %r resolved to %d gateway tool(s); using the model turn",
                    name, len(candidates))
        return None
    misfit = arguments_fit_schema(candidates[0], arguments)
    if misfit:
        logger.info("fast path: %r arguments do not fit its schema (%s); using the model turn",
                    name, misfit)
        return None
    return candidates[0], arguments


async def run_tool_call(tool, arguments: dict, node_id: str = "") -> Optional[dict]:
    """Make one call through ``tool.stream`` and return its ToolResult (None if none arrived).

    Stops at the first ToolResultEvent, as Strands' tool executor does — the wraps do their work
    (job registration, mirroring) BEFORE yielding it for exactly that reason.
    """
    from strands.types._events import ToolResultEvent

    tool_use = {"toolUseId": f"plan-fastpath-{node_id or 'node'}", "name": tool.tool_name,
                "input": dict(arguments)}
    stream = tool.stream(tool_use, {})
    result = None
    try:
        async for event in stream:
            if isinstance(event, ToolResultEvent):
                result = event.tool_result
                break
    finally:
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001 - closing an exhausted/broken generator
            pass
    return result


def result_value(result: dict):
    """The tool result's value: its json block, else its single text block parsed as JSON, else
    ``{"text": ...}`` — the same value a model would have restated in its closing json block."""
    texts, value = [], None
    for c in (result or {}).get("content") or []:
        if not isinstance(c, dict):
            continue
        if "json" in c and value is None:
            value = c["json"]
        elif isinstance(c.get("text"), str):
            texts.append(c["text"])
    if value is None and len(texts) == 1:
        try:
            value = json.loads(texts[0])
        except (ValueError, TypeError):
            value = None
    if value is None:
        value = {"text": "\n".join(texts)}
    return value


def fast_path_output(result: dict) -> str:
    """The node's text output for a direct call: the result as a closing ```json block."""
    return "```json\n" + json.dumps(result_value(result), default=str) + "\n```"
