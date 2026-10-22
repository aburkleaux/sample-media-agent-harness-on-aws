"""Tests for the Save / Activate Driver (tasks 9.2 / 9.3).

Everything runs against a recording fake OMS client that models realistic OMS
create/update semantics (POST create rejects an existing id; PUT upserts) and
logs every call, so we can assert:

- create vs update paths report the right ``operation`` (9.2, R6.2/R6.3/R8.2);
- a save persists an inert draft and NEVER calls ``activate_template`` (9.2, R8.3);
- ``activate`` calls ``activate_template`` (9.2, R9.5);
- Property 10 (9.3): equivalent workflows yield the same ``template_id`` and a
  re-persist routes to an update (not a duplicate create).

Requirements: 6.2, 6.3, 8.1, 8.2, 8.3, 9.5
Properties: 10
"""

from __future__ import annotations

import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.models import (
    MappedStep,
    OMSTemplate,
    ResolutionReport,
    WorkflowStep,
)
from persistence.save_activate_driver import (
    ActivateResult,
    SaveActivateDriver,
    SaveResult,
)
from persistence.template_assembler import assemble


# ---------------------------------------------------------------------------
# Recording fake OMS client — models create/update semantics + logs calls
# ---------------------------------------------------------------------------


class RecordingFakeOMSClient:
    """A fake ``OMSMcpClient`` that records calls and models OMS write semantics.

    ``save_template(update=False)`` is a create: it rejects an id that already
    exists with an "already exists" envelope (mirroring a POST 409), otherwise
    stores it. ``save_template(update=True)`` is an upsert (PUT). The in-memory
    ``templates`` store lets tests assert no duplicate was ever created.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.templates: dict[str, dict] = {}

    def save_template(self, template: dict, update: bool) -> dict:
        self.calls.append(("save_template", {"template": template, "update": update}))
        template_id = template["template_id"]
        if update:
            self.templates[template_id] = template
            return {"template_id": template_id, "updated": True}
        # create (POST): reject a duplicate id.
        if template_id in self.templates:
            return {
                "error": f"template {template_id} already exists",
                "template_id": template_id,
            }
        self.templates[template_id] = template
        return {"template_id": template_id, "created": True}

    def activate_template(self, template_id: str) -> dict:
        self.calls.append(("activate_template", {"template_id": template_id}))
        return {"template_id": template_id, "activated": True}

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def save_calls(self) -> list[dict]:
        return [args for name, args in self.calls if name == "save_template"]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _template(template_id: str = "wf-abc123") -> OMSTemplate:
    """A minimal, structurally valid inert-draft template."""
    return OMSTemplate(
        template_id=template_id,
        name="test workflow",
        description="test",
        conditions={"services": ["audio"]},
        stages={
            "validate": {"tasks": []},
            "fulfill": {
                "tasks": [
                    {
                        "task_id": "audio__stem-separation",
                        "service": "audio",
                        "action": "stem_separation",
                        "depends_on": [],
                        "required": True,
                        "timeout_seconds": 3600,
                        "config": {},
                    }
                ]
            },
        },
    )


# ---------------------------------------------------------------------------
# 9.2 — create path (R6.2, R8.2)
# ---------------------------------------------------------------------------


def test_save_creates_when_id_absent():
    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)

    result = driver.save(_template("wf-new"))

    assert isinstance(result, SaveResult)
    assert result.operation == "created"
    assert result.created and not result.updated
    assert result.template_id == "wf-new"
    # A single create write, no update needed.
    assert client.save_calls == [{"template": _template("wf-new").to_dict(), "update": False}]


# ---------------------------------------------------------------------------
# 9.2 — update path (R6.3, R8.2)
# ---------------------------------------------------------------------------


def test_save_updates_when_id_already_exists():
    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)

    first = driver.save(_template("wf-dup"))
    second = driver.save(_template("wf-dup"))

    assert first.operation == "created"
    assert second.operation == "updated"
    assert second.updated and not second.created
    assert second.template_id == "wf-dup"

    # Re-persist attempted a create (which conflicted) then fell back to update.
    assert client.save_calls[0]["update"] is False  # first create
    assert client.save_calls[1]["update"] is False  # second create attempt (conflict)
    assert client.save_calls[2]["update"] is True   # fallback update
    # No duplicate stored.
    assert list(client.templates) == ["wf-dup"]


# ---------------------------------------------------------------------------
# 9.2 — inert draft: save NEVER activates (R8.1, R8.3)
# ---------------------------------------------------------------------------


def test_save_never_calls_activate_template():
    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)

    driver.save(_template("wf-1"))
    driver.save(_template("wf-1"))  # update path too

    assert "activate_template" not in client.called_tools


def test_save_submits_template_as_plain_dict_draft():
    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)
    template = _template("wf-draft")

    driver.save(template)

    submitted = client.save_calls[0]["template"]
    assert submitted == template.to_dict()
    assert isinstance(submitted, dict)


# ---------------------------------------------------------------------------
# 9.2 — activate path (R9.5)
# ---------------------------------------------------------------------------


def test_activate_calls_activate_template():
    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)

    result = driver.activate("wf-xyz")

    assert isinstance(result, ActivateResult)
    assert result.activated is True
    assert result.template_id == "wf-xyz"
    assert client.called_tools == ["activate_template"]


def test_activate_never_calls_save_template():
    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)

    driver.activate("wf-xyz")

    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# 9.3 — Property 10: deterministic id + upsert idempotence
# ---------------------------------------------------------------------------

_ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=8)
_tools = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=10)


@st.composite
def fully_mapped_workflows(draw):
    """Generate a fully-mapped ``(ResolutionReport, HighLevelPlan)`` (DAG plan).

    Mirrors the assembler test's generator: every sub-goal suggests at least one
    tool, ids are unique, and each ``depends_on`` edge points only at an earlier
    sub-goal (a DAG). Reasoning is non-empty so ``build_conditions`` always has an
    intent keyword to scope on.
    """
    ids = draw(st.lists(_ids, min_size=1, max_size=5, unique=True))
    sub_goals: list[SubGoal] = []
    mapped: list[MappedStep] = []
    for i, sub_goal_id in enumerate(ids):
        tools = draw(st.lists(_tools, min_size=1, max_size=2))
        earlier = ids[:i]
        depends_on = draw(
            st.lists(st.sampled_from(earlier), min_size=0, max_size=len(earlier), unique=True)
            if earlier
            else st.just([])
        )
        sub_goals.append(
            SubGoal(
                id=sub_goal_id,
                description="do the thing",
                reasoning="separate vocals",
                suggested_tools=tools,
                suggested_skills=[],
                depends_on=depends_on,
            )
        )
        for tool in tools:
            step = WorkflowStep(
                sub_goal_id=sub_goal_id,
                bare_tool=tool,
                depends_on=list(depends_on),
                description="",
            )
            mapped.append(MappedStep(step=step, service="svc", action=tool, task_id=""))

    plan = HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning="separate vocals from the music track",
    )
    return ResolutionReport(mapped=mapped), plan


def _clone_report(report: ResolutionReport) -> ResolutionReport:
    """Deep-clone a report into fresh, field-equal objects (an equivalent workflow)."""
    cloned: list[MappedStep] = []
    for m in report.mapped:
        step = WorkflowStep(
            sub_goal_id=m.step.sub_goal_id,
            bare_tool=m.step.bare_tool,
            depends_on=list(m.step.depends_on),
            description=m.step.description,
        )
        cloned.append(MappedStep(step=step, service=m.service, action=m.action, task_id=""))
    return ResolutionReport(mapped=cloned)


# Feature: oms-workflow-persistence, Property 10: Deterministic template id with upsert idempotence
@settings(max_examples=150)
@given(fully_mapped_workflows())
def test_deterministic_id_and_upsert_idempotence(case) -> None:
    """Validates: Requirements 6.1, 6.2, 6.3

    Two equivalent workflows (assembled independently via the assembler's
    ``derive_template_id``) yield the same ``template_id``; persisting the first
    creates and re-persisting the equivalent one routes to an update — never a
    duplicate create.
    """
    report, plan = case

    template_a = assemble(report, plan)
    template_b = assemble(_clone_report(report), plan)

    # Equivalent workflows -> identical deterministic template_id.
    assert template_a.template_id == template_b.template_id

    client = RecordingFakeOMSClient()
    driver = SaveActivateDriver(client)

    first = driver.save(template_a)
    second = driver.save(template_b)

    assert first.operation == "created"
    assert second.operation == "updated"
    # Upsert idempotence: exactly one template persisted, no duplicate id.
    assert list(client.templates) == [template_a.template_id]
    # The re-persist did route through an update write.
    assert any(c["update"] is True for c in client.save_calls)
    # And no activation happened anywhere in the save flow.
    assert "activate_template" not in client.called_tools
