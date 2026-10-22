"""A plan node that cannot be BUILT must fail at once, never sit `running` for the watchdog.

Plan 593333b5: eight fal video nodes listed the same gateway tool twice (``usr-…___submit_job`` and the bare
``submit_job``), Strands refused the duplicate registration in ``Agent(...)``, and the exception escaped before
anything wrote the node's status — so each node looked "slow" for 900s until the stall watchdog reaped it, was
retried, crashed again and waited another 900s. Now: one tool per name, and a construction failure is written
FAILED with the ``NODE_SETUP_FAILED`` prefix the chainer does not retry.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import task_agent as ta  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"


def _exec_node_src() -> str:
    body = (SRC / "task_agent.py").read_text()
    return body[body.index("async def _execute_plan_node"):body.index("async def _fetch_plan_node_results")]


def test_dedupe_keeps_one_tool_per_name_in_order():
    a = SimpleNamespace(tool_name="usr-1___submit_job")
    b = SimpleNamespace(tool_name="artifact")
    unnamed = object()
    assert ta._dedupe_tools_by_name([a, b, a, unnamed, SimpleNamespace(tool_name="artifact")]) == [a, b, unnamed]
    assert ta._dedupe_tools_by_name(None) == []


def test_node_agent_gets_deduped_tools_and_reports_a_build_failure():
    src = _exec_node_src()
    build = src.index("node_agent = Agent(")
    assert "resolved_tools = _dedupe_tools_by_name(resolved_tools)" in src[:build]
    after = src[build:build + 4000]
    assert "except Exception" in after and "_write_node_failed(error_msg)" in after
    assert "NODE_SETUP_FAILED" in after


def test_setup_failure_prefix_is_stable():
    # The plan chainer matches this literal (handler._should_retry_node); its test reads it back from here.
    assert ta.NODE_SETUP_FAILED == "Node setup failed"
