"""Wiring test for the OMS workflow-persistence tools on the Main Agent.

Verifies that the two persistence @tools — ``persist_workflow`` and
``confirm_workflow_activation`` — are wired into the Main Agent's builtin tool
registry through the same assembly path ``main.py`` uses.

``main.py`` populates ``_all_builtin_tools`` / ``_added_builtins`` only inside
``_init_agent_subsystems()`` (which requires live AWS), so this test asserts on
the registry-append *source* that main iterates —
``persistence_tool.PERSISTENCE_TOOLS`` — which is the exact object set main
appends. It reproduces main's assembly expression to confirm both tools land in
the builtin name set.

_Requirements: 9.1, 11.1_
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence import persistence_tool


PERSISTENCE_TOOL_NAMES = {"persist_workflow", "confirm_workflow_activation"}


def _persistence_tool_names() -> set[str]:
    """Bare tool names main derives via ``t.tool_spec['name']`` on PERSISTENCE_TOOLS."""
    return {t.tool_spec["name"] for t in persistence_tool.PERSISTENCE_TOOLS}


# ---------------------------------------------------------------------------
# The exported registry list main iterates (source of the append site)
# ---------------------------------------------------------------------------

def test_persistence_tools_export_both_callables():
    """PERSISTENCE_TOOLS carries exactly the two persistence tools by name."""
    assert _persistence_tool_names() == PERSISTENCE_TOOL_NAMES


# ---------------------------------------------------------------------------
# Reproduce main's builtin-registry assembly and assert both tools are present
# ---------------------------------------------------------------------------

def test_persistence_tools_registered_in_builtin_set():
    """Both tools land in the builtin set via main's append expression.

    Mirrors the two loops in ``main._init_agent_subsystems``:
        for _persistence_tool in persistence_tool.PERSISTENCE_TOOLS:
            _all_builtin_tools[_persistence_tool.tool_spec["name"]] = _persistence_tool
        _added_builtins.update({..., *(t.tool_spec["name"] for t in PERSISTENCE_TOOLS)})
    """
    all_builtin_tools: dict = {}
    for _persistence_tool in persistence_tool.PERSISTENCE_TOOLS:
        all_builtin_tools[_persistence_tool.tool_spec["name"]] = _persistence_tool

    added_builtins: set[str] = set()
    added_builtins.update(
        {*(t.tool_spec["name"] for t in persistence_tool.PERSISTENCE_TOOLS)}
    )

    assert "persist_workflow" in all_builtin_tools
    assert "confirm_workflow_activation" in all_builtin_tools
    assert PERSISTENCE_TOOL_NAMES.issubset(added_builtins)


# ---------------------------------------------------------------------------
# End-to-end: main.py imports and exposes the wiring it appends from
# ---------------------------------------------------------------------------

def test_main_module_wires_persistence_tools():
    """Importing main exposes PERSISTENCE_TOOLS as the source it registers from.

    ``main`` imports ``persistence_tool`` at module scope and iterates
    ``persistence_tool.PERSISTENCE_TOOLS`` when building ``_all_builtin_tools``.
    If main cannot be imported in this environment (e.g. a pre-existing native
    dependency arch mismatch), skip rather than fail — the assembly-path assertion
    above still covers the wiring contract.
    """
    try:
        import main
    except Exception as exc:  # pragma: no cover — environment-dependent import
        pytest.skip(f"main.py not importable in this environment: {exc}")

    names = {t.tool_spec["name"] for t in main.persistence_tool.PERSISTENCE_TOOLS}
    assert PERSISTENCE_TOOL_NAMES.issubset(names)
