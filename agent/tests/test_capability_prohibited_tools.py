"""Drift check: the prohibition list vs. the Runtime's actual tool names (R10).

``capability/descriptors.PROHIBITED_TOOLS`` is a hardcoded set because the
Capability API and the projection Lambda must not import Runtime modules to
enumerate it — doing so would pull Strands in and defeat R1.

A hardcoded list rots. When someone adds a Lab CRUD tool or renames a
persistence tool, the failure mode without this test is silent and bad: the new
tool becomes eligible to appear as a shared Tools card and, if it ever reaches a
tool collection, to be projected into the harness-shared Registry. So this test
imports the real Runtime modules (available in the test environment, not in the
deployed control plane) and asserts the hardcoded set still covers them.

Fix a failure by adding the name to the appropriate set in
``capability/descriptors.py`` — not by relaxing this test.
"""

from __future__ import annotations

import pytest

from capability.descriptors import PROHIBITED_TOOLS


def _tool_names(tools) -> set[str]:
    return {tool.tool_spec["name"] for tool in tools}


def test_every_lab_crud_tool_is_prohibited():
    main_lab_tools = pytest.importorskip("main_lab_tools")

    bound = _tool_names(main_lab_tools.LAB_TOOLS)
    individual = _tool_names(main_lab_tools._INDIVIDUAL_LAB_TOOLS)
    missing = sorted((bound | individual) - PROHIBITED_TOOLS)
    assert not missing, (
        "Lab control-plane tools missing from PROHIBITED_TOOLS: "
        f"{missing}. Add them to _LAB_CRUD_TOOLS in capability/descriptors.py — "
        "these must never render as shared Tools cards or enter the Registry."
    )


def test_every_persistence_tool_is_prohibited():
    persistence_tool = pytest.importorskip("persistence.persistence_tool")

    missing = sorted(
        _tool_names(persistence_tool.PERSISTENCE_TOOLS) - PROHIBITED_TOOLS
    )
    assert not missing, (
        "Persistence/OMS tools missing from PROHIBITED_TOOLS: "
        f"{missing}. Add them to _PERSISTENCE_TOOLS in capability/descriptors.py."
    )


def test_execution_primitives_and_orchestration_internals_are_prohibited():
    """The names R10 lists explicitly, asserted as a set.

    Kept literal rather than derived: R10 enumerates these by name, so the test
    should fail if one is dropped from the prohibition set even if the Runtime
    stops binding it.
    """
    required = {
        # execution primitives
        "file_read",
        "http_request",
        "current_time",
        "load_tool",
        "shell",
        "artifact",
        "code_interpreter",
        # orchestration / job internals
        "dispatch_tool",
        # Same class as dispatch_tool: the orchestrator's route onto another
        # runtime, not a capability a user selects from the Tools catalog. The
        # derived halves of this suite cannot catch it (it is in neither
        # LAB_TOOLS nor PERSISTENCE_TOOLS), so the literal set is the only place
        # the convention can be held.
        "invoke_custom_agent",
        "plan_and_execute_tool",
        "get_job",
        "list_my_jobs",
        "get_plan_results",
        "read_result_files",
    }
    assert required <= PROHIBITED_TOOLS, sorted(required - PROHIBITED_TOOLS)


def test_gateway_tools_the_catalog_must_keep_are_not_prohibited():
    """Guard against an over-broad prohibition emptying the Tools tab.

    The prohibition list failing closed is right; failing closed on the M2C
    workflow tools would leave the Tools catalog with nothing in it, which is
    the opposite mistake and just as invisible.
    """
    keep = {
        "process_media",
        "diarize_speech",
        "separate_stems",
        "get_job_status",
        "shot_segmentation",
        "face_recognition",
        "hybrid_search",
        "tts",
    }
    overlap = sorted(keep & PROHIBITED_TOOLS)
    assert not overlap, f"shared gateway tools wrongly prohibited: {overlap}"
