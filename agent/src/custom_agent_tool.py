"""Strands tool for delegating a task to one of the user's Custom Agents.

Custom Agents have no runtime of their own — they multiplex onto the shared
Task Agent runtime. This tool is the chat-side entrance to that path.

Division of labour: the orchestrator validates the *reference* and pins the
version; the Task Agent resolves the *config*. Resolved config is deliberately
never sent on the wire. ``custom_agent_resolver.resolve_config`` filters tool
collections against the tool names bindable on the runtime it runs on, and this
process is the orchestrator, whose builtin set and gateway connections differ
from the Task Agent's — resolving here would drop collections the Task Agent
can bind and admit names it cannot.
"""
import asyncio
import json
import logging
import os
from typing import Optional

from strands import tool

import runtime_arns
from orchestration.progress_channel import get_active_channel
from orchestration.dispatch_tool import (
    _forward_request_context,
    _invoke_runtime_stream,
)
from main_lab_tools import lookup_published_agent as _lookup_published_agent

logger = logging.getLogger(__name__)

TASK_AGENT_ARN = os.environ.get("TASK_AGENT_RUNTIME_ARN", "")


def _err(code: str, message: str) -> str:
    return json.dumps({"error": code, "message": message})


@tool
async def invoke_custom_agent(
    agent_id: str, prompt: str, version: Optional[int] = None
) -> str:
    """Delegate a task to one of the user's own PUBLISHED Custom Agents.

    The agent runs with its own persona, model, skills, and tool allowlist.
    Call `lab_asset(action="list_custom_agents")` first to get the agent_id and
    to read each agent's description. Listing is an action on the lab_asset
    multiplexer, NOT a top-level tool of its own.

    The agent's saved configuration is authoritative — you cannot override its
    tools, skills, model, or prompt from here, by design.

    Args:
        agent_id: The Custom Agent's assetId, from
            `lab_asset(action="list_custom_agents")`.
        prompt: The task for the agent. It does NOT see this conversation, so
            include any context it needs.
        version: OMIT THIS. Defaults to the agent's current published version,
            which is what the user expects. Set it only to reproduce a
            specific historical version.
    """
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return _err("invalid_request", "agent_id is required")
    if not prompt:
        return _err("invalid_request", "prompt is required")
    # Import-time constant first (tests patch it); else resolve at call time — the deploy no longer
    # puts the ARN in runtime env (it is an SSM parameter). Off the loop: a cache miss is a
    # blocking SSM read, and this is an async tool awaited ON the event loop.
    task_arn = TASK_AGENT_ARN or await asyncio.to_thread(
        runtime_arns.get, "TASK_AGENT_RUNTIME_ARN"
    )
    if not task_arn:
        return _err(
            "not_configured",
            "No task runtime configured (TASK_AGENT_RUNTIME_ARN unset)",
        )

    # --- validate + pin, orchestrator-side ---
    ref = await _lookup_published_agent(agent_id, version)
    if not isinstance(ref, dict):
        return _err("agent_not_found", "Could not resolve that Custom Agent.")
    if ref.get("error"):
        # Pass the service's error through verbatim: its code and message name
        # the remedy (publish it / check the id / pick a real version), which
        # is what lets the model self-correct in one turn.
        return json.dumps(ref)

    pinned_version = ref.get("version")
    agent_name = ref.get("name") or agent_id

    # --- dispatch by reference only ---
    payload = {
        "prompt": prompt,
        "agent_id": agent_id,
        "agent_source": "published",
        # Marks this as agent-scoped so the runtime resolves the config and,
        # crucially, fails closed if it cannot (see task_agent._agent_scoped).
        "delegated": True,
        # ALWAYS explicit. A missing/None `tools` means "load ALL tools" on
        # the runtime, which would bypass the owner's allowlist.
        "tools": [],
    }
    if pinned_version is not None:
        # Pinned so the runtime's own version selection is a no-op and the two
        # implementations can never disagree.
        payload["version"] = pinned_version
    _forward_request_context(payload)

    channel = get_active_channel()
    if channel is not None:
        channel.push(
            "running", f"↳ {agent_name}: working...", agent="custom-agent"
        )

    logger.info(
        "Delegating to custom agent %s (%s) version=%r",
        agent_id, agent_name, pinned_version,
    )

    try:
        # OFF the event loop. `_invoke_runtime_stream` is fully blocking
        # (boto3 `invoke_agent_runtime` plus `body.iter_lines()` under a 300s
        # wall-clock budget), and Strands dispatches the two tool shapes
        # differently: a module-style SYNC tool like `dispatch_tool` is run via
        # `asyncio.to_thread` (strands/tools/tools.py:259), but a `@tool async
        # def` is awaited directly ON the loop
        # (strands/tools/decorator.py:628). So calling it inline blocks the loop
        # for the whole delegation, which blocks `main._progress_poller` — the
        # task whose own comment reads "The Strands stream blocks during long
        # tool executions. A background poller drains progress events and keeps
        # the HTTP stream active." The visible effects were: the
        # "↳ <Agent>: working..." step above could not be drained until the
        # delegation finished (start and finish arrived together), the
        # sub-agent's forwarded step events queued and flushed at the end
        # instead of streaming, and the 30s keepalive stopped — so a
        # multi-minute delegation streamed nothing at all.
        #
        # Safe to thread: `ProgressChannel.push` is thread-safe by design
        # ("Tools running in Strands tool handler threads push events here"),
        # `_active_channel` is a module global rather than a contextvar so the
        # thread sees the same channel, and `asyncio.to_thread` copies the
        # contextvars the request-context forwarding relies on.
        text, had_error, saw_completion = await asyncio.to_thread(
            _invoke_runtime_stream, task_arn, payload
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to invoke custom agent %s", agent_id)
        if channel is not None:
            channel.push(
                "failed", f"↳ {agent_name}: failed", agent="custom-agent"
            )
        return _err("delegation_failed", str(e))

    if channel is not None:
        channel.push(
            "failed" if had_error else "done",
            f"↳ {agent_name}: {'failed' if had_error else 'done'}",
            agent="custom-agent",
        )

    if had_error:
        return _err(
            "delegation_failed",
            text or f"{agent_name} encountered an error",
        )
    if not text and not saw_completion:
        return _err("delegation_failed", f"{agent_name} returned no output")

    return json.dumps({"agent": agent_name, "version": pinned_version,
                       "result": text})
