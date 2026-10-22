"""Smoke test to verify specialty_crud module imports and has expected interface."""

from __future__ import annotations

import sys
import os

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.specialty_crud import (
    SpecialtyCRUD,
    SpecialtyCreateResult,
    SpecialtyReadResult,
)


def test_specialty_crud_has_expected_methods():
    """SpecialtyCRUD class exposes the required CRUD methods."""
    assert hasattr(SpecialtyCRUD, "create")
    assert hasattr(SpecialtyCRUD, "read")
    assert hasattr(SpecialtyCRUD, "update")
    assert hasattr(SpecialtyCRUD, "delete")
    assert hasattr(SpecialtyCRUD, "list")


def test_no_virtual_core_task_agent_reference():
    """The virtual core-task-agent row is gone from this module.

    It was a hardcoded dict the service layer appended to every
    ListSpecialtyAgentDrafts response, with no AssetIndex row behind it — so it
    rendered a second "Task Agent" next to the Built-in agents browser and every
    mutation addressed to it failed. The built-in runtimes are surfaced by
    `mcp_list_agents` instead.
    """
    import lab.specialty_crud as specialty_crud

    assert not hasattr(specialty_crud, "_CORE_TASK_AGENT_REF")


def test_create_result_dataclass():
    """SpecialtyCreateResult has the expected fields."""
    result = SpecialtyCreateResult(asset_id="test-id", s3_prefix="drafts/specialty-agents/test-id/")
    assert result.asset_id == "test-id"
    assert result.s3_prefix == "drafts/specialty-agents/test-id/"


def test_read_result_dataclass():
    """SpecialtyReadResult has the expected fields."""
    result = SpecialtyReadResult(
        asset_id="test-id",
        name="Test Specialty",
        persona="You are a test agent.",
        compatible_skills=["skill-1", "skill-2"],
        metadata={"category": "specialty-agent"},
    )
    assert result.asset_id == "test-id"
    assert result.name == "Test Specialty"
    assert result.persona == "You are a test agent."
    assert result.compatible_skills == ["skill-1", "skill-2"]
