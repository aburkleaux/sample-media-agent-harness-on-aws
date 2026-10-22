"""The delegation seam: validate, pin, dispatch by reference only.

The parameter list is the security-relevant part. Accepting tools / skills /
model_id / system_prompt would recreate the /dispatch hole, where a caller
controls the tool set (omit `tools` and EVERY gateway tool binds) and the
system prompt.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import custom_agent_tool  # noqa: E402

# The @tool decorator wraps the coroutine. Use whichever attribute exposes it
# (commonly `.func` or `.__wrapped__`) — settle this the same way you did in
# Task 6 and bind it once here.
_invoke = custom_agent_tool.invoke_custom_agent


def _props():
    return custom_agent_tool.invoke_custom_agent.tool_spec[
        "inputSchema"
    ]["json"]["properties"]


def test_spec_exposes_exactly_three_parameters():
    assert set(_props()) == {"agent_id", "prompt", "version"}


def test_spec_rejects_config_parameters():
    """Regression guard against reopening the /dispatch hole."""
    for forbidden in ("tools", "skills", "model_id", "system_prompt"):
        assert forbidden not in _props()


@pytest.mark.asyncio
async def test_dispatches_a_reference_with_a_pinned_version(monkeypatch):
    sent = {}

    async def _lookup(agent_id, version=None):
        return {
            "assetId": agent_id,
            "name": "Transcript Cleaner",
            "description": "d",
            "version": 2,
        }

    def _stream(arn, payload):
        sent.update(payload)
        return ("cleaned up", False, True)

    monkeypatch.setattr(custom_agent_tool, "_lookup_published_agent", _lookup)
    monkeypatch.setattr(custom_agent_tool, "_invoke_runtime_stream", _stream)
    monkeypatch.setattr(custom_agent_tool, "TASK_AGENT_ARN", "arn:aws:bedrock:::r/x")

    out = json.loads(await _invoke(agent_id="ag-1", prompt="clean this"))

    assert out["result"] == "cleaned up"
    assert sent["agent_id"] == "ag-1"
    assert sent["agent_source"] == "published"
    assert sent["delegated"] is True
    assert sent["version"] == 2          # pinned, not left to the runtime
    assert sent["tools"] == []           # explicit; never None
    assert sent["prompt"] == "clean this"
    # Config never travels on the wire — the runtime resolves it.
    for forbidden in ("custom_agent", "system_prompt", "model_id", "skills"):
        assert forbidden not in sent


@pytest.mark.asyncio
async def test_validation_error_is_returned_without_dispatching(monkeypatch):
    dispatched = []

    async def _lookup(agent_id, version=None):
        return {
            "error": "agent_not_found",
            "message": "No published Custom Agent ag-x owned by you.",
        }

    def _stream(arn, payload):
        dispatched.append(payload)
        return ("", False, True)

    monkeypatch.setattr(custom_agent_tool, "_lookup_published_agent", _lookup)
    monkeypatch.setattr(custom_agent_tool, "_invoke_runtime_stream", _stream)
    monkeypatch.setattr(custom_agent_tool, "TASK_AGENT_ARN", "arn:aws:bedrock:::r/x")

    out = json.loads(await _invoke(agent_id="ag-x", prompt="p"))

    assert out["error"] == "agent_not_found"
    assert dispatched == []


@pytest.mark.asyncio
async def test_runtime_error_surfaces(monkeypatch):
    async def _lookup(agent_id, version=None):
        return {"assetId": agent_id, "name": "n", "description": "", "version": 1}

    monkeypatch.setattr(custom_agent_tool, "_lookup_published_agent", _lookup)
    monkeypatch.setattr(
        custom_agent_tool,
        "_invoke_runtime_stream",
        lambda arn, payload: ("agent_config_unresolved", True, True),
    )
    monkeypatch.setattr(custom_agent_tool, "TASK_AGENT_ARN", "arn:aws:bedrock:::r/x")

    out = json.loads(await _invoke(agent_id="ag-1", prompt="p"))
    assert out["error"] == "delegation_failed"
    assert "agent_config_unresolved" in out["message"]


@pytest.mark.asyncio
async def test_missing_prompt_is_rejected(monkeypatch):
    monkeypatch.setattr(custom_agent_tool, "TASK_AGENT_ARN", "arn:aws:bedrock:::r/x")
    out = json.loads(await _invoke(agent_id="ag-1", prompt=""))
    assert out["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_the_blocking_invoke_runs_off_the_event_loop(monkeypatch):
    """The delegation must not block the orchestrator's event loop.

    Strands dispatches the two tool shapes differently: a module-style SYNC
    tool (``dispatch_tool``) is run via ``asyncio.to_thread``
    (strands/tools/tools.py:259), but a ``@tool async def`` is awaited directly
    ON the loop (strands/tools/decorator.py:628). ``_invoke_runtime_stream`` is
    fully blocking, so calling it inline stalled ``main._progress_poller`` —
    the task that drains progress events and keeps the HTTP stream alive with a
    30s keepalive. Start and finish steps then arrived together and a
    multi-minute delegation streamed nothing.

    Asserts all three things that fix depends on: a different thread, a loop
    that keeps ticking while the call is in flight, and a progress channel that
    still receives pushes from the tool thread.
    """
    import asyncio
    import threading
    import time

    from orchestration.progress_channel import (
        ProgressChannel,
        get_active_channel,
        set_active_channel,
    )

    main_thread = threading.get_ident()
    observed: dict = {}
    ticks: list[int] = []

    async def _lookup(agent_id, version=None):
        return {"assetId": agent_id, "name": "Slow Agent", "version": 1}

    def _stream(_arn, _payload):
        observed["thread"] = threading.get_ident()
        # Push from the tool thread, exactly as the real dispatch does when it
        # forwards the sub-agent's step events.
        ch = get_active_channel()
        if ch is not None:
            ch.push("running", "pushed from the tool thread", agent="task")
        time.sleep(0.2)  # blocking, like boto3 invoke + iter_lines
        return ("finished", False, True)

    async def _poller():
        """Stands in for main._progress_poller."""
        while True:
            ticks.append(1)
            await asyncio.sleep(0.01)

    monkeypatch.setattr(custom_agent_tool, "_lookup_published_agent", _lookup)
    monkeypatch.setattr(custom_agent_tool, "_invoke_runtime_stream", _stream)
    monkeypatch.setattr(custom_agent_tool, "TASK_AGENT_ARN", "arn:aws:bedrock:::r/x")

    channel = ProgressChannel()
    set_active_channel(channel)
    poller = asyncio.create_task(_poller())
    try:
        out = json.loads(await _invoke(agent_id="ag-1", prompt="p"))
    finally:
        poller.cancel()
        set_active_channel(None)

    assert out["result"] == "finished"
    # 1. It ran on a worker thread, not the loop thread.
    assert observed["thread"] != main_thread
    # 2. The loop stayed live for the whole 0.2s blocking call. Called inline,
    #    the poller cannot tick at all while the tool is running.
    assert len(ticks) >= 5, (
        f"event loop was starved during the delegation: only {len(ticks)} tick(s)"
    )
    # 3. The channel still gets pushes across the thread boundary — both the
    #    tool thread's own and the "working..."/"done" steps the tool emits.
    labels = [e.label for e in channel.drain()]
    assert "pushed from the tool thread" in labels
    assert any("Slow Agent" in label for label in labels)
