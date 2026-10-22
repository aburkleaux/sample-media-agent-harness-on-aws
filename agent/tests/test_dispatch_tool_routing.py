"""Unit tests for dispatch_tool Render Agent routing (task 3.2).

Validates:
- RENDER_AGENT_RUNTIME_ARN env var is read
- runtime_arn parameter routes to specified ARN
- Default routing (no runtime_arn) uses TASK_AGENT_RUNTIME_ARN
- Backward compatibility — existing dispatches unchanged
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add agent/src to path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Ensure the dispatch_tool module picks up test env vars on each import.

    Saves and restores ``sys.modules["orchestration.dispatch_tool"]`` around the test so the
    forced re-import (with this file's fake ARNs) does not leak a polluted
    module object into later tests (e.g. test_hook_dispatch_action, which
    monkeypatches the module directly and expects the original object).
    """
    import orchestration
    saved = sys.modules.get("orchestration.dispatch_tool")
    saved_attr = getattr(orchestration, "dispatch_tool", None)
    # Remove the cached module AND the parent-package attribute so a fresh
    # `from orchestration import dispatch_tool` re-executes with the test's env.
    # A submodule is cached BOTH in sys.modules and as an attribute on its
    # package; popping sys.modules alone leaves the stale attribute, and the
    # from-import short-circuits to it (the former flat module had no parent).
    sys.modules.pop("orchestration.dispatch_tool", None)
    if hasattr(orchestration, "dispatch_tool"):
        delattr(orchestration, "dispatch_tool")
    # Set up default env vars for tests
    monkeypatch.setenv("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock:us-west-2:123456:agent-runtime/task-agent")
    monkeypatch.setenv("RENDER_AGENT_RUNTIME_ARN", "arn:aws:bedrock:us-west-2:123456:agent-runtime/render-agent")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield
    # Restore the original module object (or clear) so a later test's
    # monkeypatch/import targets the same object the runtime uses.
    if saved is not None:
        sys.modules["orchestration.dispatch_tool"] = saved
    else:
        sys.modules.pop("orchestration.dispatch_tool", None)
    if saved_attr is not None:
        setattr(orchestration, "dispatch_tool", saved_attr)


def _make_tool_input(prompt="test prompt", runtime_arn=None, **kwargs):
    """Helper to build a tool invocation dict."""
    inp = {"prompt": prompt, **kwargs}
    if runtime_arn is not None:
        inp["runtime_arn"] = runtime_arn
    return {"toolUseId": "test-tool-use-id", "input": inp}


def test_render_agent_arn_env_var_is_read(monkeypatch):
    """RENDER_AGENT_RUNTIME_ARN env var should be read at module level."""
    monkeypatch.setenv("RENDER_AGENT_RUNTIME_ARN", "arn:render:test")
    from orchestration import dispatch_tool
    assert dispatch_tool.RENDER_AGENT_ARN == "arn:render:test"


def test_default_routing_uses_task_agent_arn(monkeypatch):
    """When no runtime_arn is passed, dispatch should target TASK_AGENT_RUNTIME_ARN."""
    from orchestration import dispatch_tool

    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = {"response": b"data: hello"}

    with patch.object(dispatch_tool, "_get_client", return_value=mock_client):
        tool = _make_tool_input(prompt="do something")
        dispatch_tool.dispatch_tool(tool)

    call_kwargs = mock_client.invoke_agent_runtime.call_args[1]
    assert call_kwargs["agentRuntimeArn"] == "arn:aws:bedrock:us-west-2:123456:agent-runtime/task-agent"


def test_explicit_runtime_arn_routes_to_render_agent(monkeypatch):
    """When runtime_arn is passed, dispatch should target that ARN."""
    from orchestration import dispatch_tool

    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = {"response": b"data: hello"}

    render_arn = "arn:aws:bedrock:us-west-2:123456:agent-runtime/render-agent"
    with patch.object(dispatch_tool, "_get_client", return_value=mock_client):
        tool = _make_tool_input(prompt="analyze media", runtime_arn=render_arn)
        dispatch_tool.dispatch_tool(tool)

    call_kwargs = mock_client.invoke_agent_runtime.call_args[1]
    assert call_kwargs["agentRuntimeArn"] == render_arn


def test_explicit_runtime_arn_overrides_default(monkeypatch):
    """An explicit runtime_arn should override the default TASK_AGENT_ARN."""
    from orchestration import dispatch_tool

    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = {"response": b"data: result"}

    custom_arn = "arn:aws:bedrock:us-west-2:999999:agent-runtime/custom-agent"
    with patch.object(dispatch_tool, "_get_client", return_value=mock_client):
        tool = _make_tool_input(prompt="custom task", runtime_arn=custom_arn)
        dispatch_tool.dispatch_tool(tool)

    call_kwargs = mock_client.invoke_agent_runtime.call_args[1]
    assert call_kwargs["agentRuntimeArn"] == custom_arn


def test_no_arn_configured_returns_error(monkeypatch):
    """When neither runtime_arn nor TASK_AGENT_RUNTIME_ARN is set, return error."""
    monkeypatch.setenv("TASK_AGENT_RUNTIME_ARN", "")
    monkeypatch.setenv("RENDER_AGENT_RUNTIME_ARN", "")
    from orchestration import dispatch_tool

    tool = _make_tool_input(prompt="test")
    result = dispatch_tool.dispatch_tool(tool)

    assert result["status"] == "error"
    assert "No target runtime ARN configured" in result["content"][0]["text"]


def test_backward_compatibility_no_runtime_arn_field(monkeypatch):
    """Existing dispatches without runtime_arn field should work unchanged."""
    from orchestration import dispatch_tool

    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = {"response": b"data: done"}

    with patch.object(dispatch_tool, "_get_client", return_value=mock_client):
        # Simulate old-style dispatch without runtime_arn
        tool = {"toolUseId": "old-style", "input": {"prompt": "old style task", "tools": ["tool1"]}}
        result = dispatch_tool.dispatch_tool(tool)

    assert result["status"] == "success"
    call_kwargs = mock_client.invoke_agent_runtime.call_args[1]
    assert call_kwargs["agentRuntimeArn"] == "arn:aws:bedrock:us-west-2:123456:agent-runtime/task-agent"


def test_runtime_arn_in_tool_spec():
    """The runtime_arn parameter should be in the tool spec inputSchema."""
    from orchestration import dispatch_tool

    properties = dispatch_tool.TOOL_SPEC["inputSchema"]["json"]["properties"]
    assert "runtime_arn" in properties
    assert properties["runtime_arn"]["type"] == "string"
    # It should NOT be in required — it's optional
    assert "runtime_arn" not in dispatch_tool.TOOL_SPEC["inputSchema"]["json"]["required"]
