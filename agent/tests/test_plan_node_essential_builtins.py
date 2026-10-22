"""A plan/DAG node that names a non-empty tool list must STILL get the sub-agent's defining
code+artifact builtins.

The main dispatch path guarantees ``artifact`` + ``code_interpreter`` via
``_ensure_essential_builtins``; ``_execute_plan_node`` did NOT, so a node authored by the Design
Agent with only gateway ``media_*`` tools (it cannot list render-only tools, and ``dispatch_tool``
is banned from node tool lists) had no way to BUILD a deliverable and ADOPT it — a gallery/assembly
node therefore failed 100% (session 1a4b98f2): it could presign clips but not build/adopt the
clip-gallery manifest.

``_execute_plan_node`` is a large async generator with many runtime deps, so — following the same
source-contract pattern its sibling tests use (test_plan_node_loads_skills.py,
test_plan_node_durable_write.py) — this pins the invariant on the function's source. The helper's
own behavior (adds both builtins for an internal non-empty list; no-op for []/None/external) is
covered by test_task_agent_essential_tools.py.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _exec_node_src() -> str:
    body = (SRC / "task_agent.py").read_text()
    return body[body.index("async def _execute_plan_node"):body.index("async def _fetch_plan_node_results")]


def test_plan_node_binds_essential_builtins():
    src = _exec_node_src()
    assert "_ensure_essential_builtins(" in src, (
        "_execute_plan_node must call _ensure_essential_builtins so a named-tool-list plan node "
        "(e.g. a gallery/assembly node with only media_* tools) still gets code_interpreter + "
        "artifact to build and adopt its deliverable"
    )
    # It must bind on the RESOLVED list using the requested tools_list (the no-op guards live in
    # the helper), so a media-only gallery node comes out with the code+artifact capabilities.
    assert "resolved_tools = _ensure_essential_builtins(resolved_tools, tools_list" in src
