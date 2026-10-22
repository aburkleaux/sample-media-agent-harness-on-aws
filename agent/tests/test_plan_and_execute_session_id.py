"""Unit test: plan_and_execute surfaces the plan-session id to the caller.

The Planning Agent writes the HighLevelPlan to the Plan State Table + S3 under
an internal `plan-session-<ts>` id. That id must be surfaced on the tool result
so the agent can report it and the user can pass it to persist_workflow().
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import plan_and_execute_tool as pae


def test_direct_path_surfaces_plan_session_id(monkeypatch):
    """Direct-dispatch path returns planSessionId and mentions it in the text."""
    # Planning Agent output: a valid HighLevelPlan in "direct" execution_mode.
    high_level_plan = {
        "execution_mode": "direct",
        "sub_goals": [],
        "suggested_tools": {},
        "suggested_skills": {},
        "reasoning": "single-shot task",
        "direct_dispatch_params": {
            "prompt": "do the thing",
            "tools": [],
            "skills": [],
        },
    }

    def fake_invoke_runtime(arn, payload):
        if arn == "planning-arn":
            return json.dumps(high_level_plan), {"inputTokens": 1, "outputTokens": 2}
        # Task Agent (direct dispatch)
        return "task result body", {"inputTokens": 3, "outputTokens": 4}

    monkeypatch.setattr(pae, "PLANNING_AGENT_ARN", "planning-arn")
    monkeypatch.setattr(pae, "TASK_AGENT_ARN", "task-arn")
    monkeypatch.setattr(pae, "_invoke_runtime", fake_invoke_runtime)

    tool = {"toolUseId": "tu-1", "input": {"goal": "do the thing"}}
    result = pae.plan_and_execute(tool)

    # Programmatic surface: top-level planSessionId, prefixed with plan-session-.
    assert "planSessionId" in result
    assert result["planSessionId"].startswith("plan-session-")

    # Human-readable surface: the id appears in the text content the agent reads.
    text = result["content"][0]["text"]
    assert result["planSessionId"] in text
    assert "persist_workflow" in text
    # Original body is preserved.
    assert "task result body" in text
    assert result["executionMode"] == "direct"
