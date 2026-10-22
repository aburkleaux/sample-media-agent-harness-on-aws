"""Task 7 — dispatch action tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import dispatch_tool  # noqa: E402
from hooks.actions import dispatch_action, get_action, interpolate  # noqa: E402


def test_dispatch_registered():
    assert get_action("dispatch") is not None


def test_interpolate_dotted_paths():
    ctx = {"job": {"status": "SUCCEEDED"}, "tool": {"name": "diarize"}}
    assert interpolate("Job {job.status} for {tool.name}", ctx) == "Job SUCCEEDED for diarize"
    assert interpolate("missing {no.such.path}!", ctx) == "missing !"


def test_dispatch_action_invokes_target_with_interpolated_prompt(monkeypatch):
    sent = {}

    class FakeClient:
        def invoke_agent_runtime(self, **kw):
            sent["arn"] = kw["agentRuntimeArn"]
            import json
            sent["payload"] = json.loads(kw["payload"].decode())
            return {"response": b""}

    monkeypatch.setattr(dispatch_tool, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(dispatch_tool, "_consume_stream", lambda it: "summary text")
    monkeypatch.setattr(dispatch_tool, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/task")

    res = dispatch_action(
        {"prompt_template": "Summarize job {job.status}", "target": "task"},
        {"job": {"status": "SUCCEEDED"}},
    )
    assert res.status == "ok"
    assert res.detail == "summary text"
    assert sent["payload"]["prompt"] == "Summarize job SUCCEEDED"
    assert sent["arn"].endswith("/task")


def test_dispatch_action_skips_empty_prompt():
    res = dispatch_action({"prompt_template": "{nothing.here}"}, {})
    assert res.status == "skipped"


def test_dispatch_action_errors_without_arn(monkeypatch):
    monkeypatch.setattr(dispatch_tool, "TASK_AGENT_ARN", "")
    # The ARN now also resolves at call time (env, then SSM via runtime_arns), so "unconfigured"
    # means unset everywhere — another test module setdefaults this env var at import.
    monkeypatch.delenv("TASK_AGENT_RUNTIME_ARN", raising=False)
    monkeypatch.delenv("RUNTIME_ARN_PARAM_PATH", raising=False)
    res = dispatch_action({"prompt_template": "do it", "target": "task"}, {})
    assert res.status == "error"
