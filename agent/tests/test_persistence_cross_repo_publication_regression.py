"""Cross-repo publication regression test (task 14.4).

Guards the harness↔bridge contract (Option B): the per-action gateway tool name
is published by ``mcp-oms-bridge`` into the OMS service registry and read back
*verbatim* from ``get_integration``'s ``action_tool_names`` map — **never** from
``get_action_schema``. Two directions are covered:

- PUBLISHED: a ``get_integration`` detail whose ``action_tool_names`` carries an
  entry is read into the ``CapabilityIndex`` as a *published* Capability with the
  correct bare tool name (bare suffix after the final ``___``), and is joinable
  (R4.1).
- REGRESSION (the currently-verified gap): ``audio.stem_separation`` with **no**
  entry in ``action_tool_names`` is a *distinct* ``Bridge_Tool_Name_Publication``
  dependency gap. When a plan/step needs that capability during resolve, the
  harness surfaces ``UnpublishedToolNameError`` naming ``audio.stem_separation``
  (R4.2, R4.3) — driven both at the point the resolver's report is raised and
  end-to-end through the pipeline, which fails at the ``resolve`` stage and never
  calls ``save_template``.

The fake OMS client's ``get_action_schema`` raises if the builder ever calls it
for a tool name — a regression back to the pre-Option-B contract is caught.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.capability_resolver import build_capability_index, resolve
from persistence.models import UnpublishedToolNameError
from persistence.persistence_service import PersistenceService
from persistence.plan_reader import extract_steps


# ---------------------------------------------------------------------------
# Fake OMS client — records calls; get_action_schema is forbidden for tool_name
# ---------------------------------------------------------------------------


class _RecordingFakeOMSClient:
    """In-memory ``OMSMcpClient`` stand-in driving the registry + write seam.

    ``integrations`` maps integration_id -> list of ``{action, tool_name}``
    entries. ``get_integration`` surfaces the per-action gateway tool name as the
    ``action_tool_names`` map (Option B), omitting entries whose ``tool_name`` is
    ``None`` (an unpublished capability). ``get_action_schema`` raises: the
    builder must never read a tool name from it.
    """

    def __init__(self, integrations: dict, validate_response=None) -> None:
        self._integrations = integrations
        self._validate_response = validate_response or {"valid": True, "findings": []}
        self.calls: list[tuple[str, dict]] = []
        self.templates: dict[str, dict] = {}

    def list_integrations(self) -> list[dict]:
        self.calls.append(("list_integrations", {}))
        return [{"integration_id": iid} for iid in self._integrations]

    def get_integration(self, integration_id: str) -> dict:
        self.calls.append(("get_integration", {"integration_id": integration_id}))
        entries = self._integrations.get(integration_id, [])
        actions = [a["action"] for a in entries]
        action_tool_names = {
            a["action"]: a["tool_name"] for a in entries if a.get("tool_name") is not None
        }
        return {"actions": actions, "action_tool_names": action_tool_names}

    def get_action_schema(self, integration_id: str, action: str) -> dict:
        self.calls.append(
            ("get_action_schema", {"integration_id": integration_id, "action": action})
        )
        raise AssertionError(
            "tool_name must come from get_integration's action_tool_names "
            "(Option B), never from get_action_schema"
        )

    def validate_template(self, template: dict) -> dict:
        self.calls.append(("validate_template", {"template": template}))
        return self._validate_response

    def save_template(self, template: dict, update: bool) -> dict:
        self.calls.append(("save_template", {"template": template, "update": update}))
        template_id = template["template_id"]
        self.templates[template_id] = template
        return {"template_id": template_id, "created": not update, "updated": update}

    def activate_template(self, template_id: str) -> dict:
        self.calls.append(("activate_template", {"template_id": template_id}))
        return {"template_id": template_id, "activated": True}

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakePlanReader:
    """Stand-in ``PlanReader`` returning a scripted plan; real step extraction."""

    def __init__(self, plan: HighLevelPlan) -> None:
        self._plan = plan

    async def read_plan(self, session_id: str) -> HighLevelPlan:
        return self._plan

    def extract_steps(self, plan: HighLevelPlan):
        return extract_steps(plan)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _plan(sub_goals: list[SubGoal]) -> HighLevelPlan:
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning="separate the vocals from the backing track",
    )


def _sub_goal(sub_goal_id: str, tools: list[str]) -> SubGoal:
    return SubGoal(
        id=sub_goal_id,
        description="do the thing",
        reasoning="separate vocals",
        suggested_tools=tools,
        suggested_skills=[],
        depends_on=[],
    )


def _run(coro):
    return asyncio.run(coro)


# A published entry for audio.stem_separation (bare name = separate_audio_stems).
_PUBLISHED = {
    "audio": [
        {"action": "stem_separation", "tool_name": "audio-stem___separate_audio_stems"},
    ],
}

# The currently-verified gap: audio.stem_separation with NO published tool_name.
_UNPUBLISHED = {
    "audio": [
        {"action": "stem_separation", "tool_name": None},
    ],
}


# ---------------------------------------------------------------------------
# PUBLISHED: action_tool_names entry -> published Capability with bare name (R4.1)
# ---------------------------------------------------------------------------


def test_published_action_tool_name_becomes_published_capability():
    client = _RecordingFakeOMSClient(_PUBLISHED)
    index = build_capability_index(client)

    # Read from get_integration's action_tool_names, never get_action_schema.
    assert client.called_tools.count("get_action_schema") == 0

    cap = next(c for c in index.capabilities if c.action == "stem_separation")
    assert cap.service == "audio"
    assert cap.gateway_tool_name == "audio-stem___separate_audio_stems"
    # Correct bare name = suffix after the final ``___`` separator.
    assert cap.bare_tool_name == "separate_audio_stems"
    # Published capabilities are joinable via the bare-name lookup.
    assert index.lookup.get("separate_audio_stems") == [cap]


def test_published_capability_resolves_a_matching_step():
    client = _RecordingFakeOMSClient(_PUBLISHED)
    index = build_capability_index(client)

    steps = extract_steps(_plan([_sub_goal("sg1", ["separate_audio_stems"])]))
    report = resolve(steps, index)

    assert report.ok
    assert len(report.mapped) == 1
    assert (report.mapped[0].service, report.mapped[0].action) == ("audio", "stem_separation")


# ---------------------------------------------------------------------------
# REGRESSION: no action_tool_names entry -> UnpublishedToolNameError (R4.2, R4.3)
# ---------------------------------------------------------------------------


def test_stem_separation_without_entry_is_classified_unpublished():
    client = _RecordingFakeOMSClient(_UNPUBLISHED)
    index = build_capability_index(client)

    cap = next(c for c in index.capabilities if c.action == "stem_separation")
    # Present in the registry but with no published bare name -> not joinable.
    assert cap.gateway_tool_name is None
    assert cap.bare_tool_name is None
    assert all(cap not in bucket for bucket in index.lookup.values())

    # A step needing it lands in the distinct unpublished bucket (not unmappable).
    steps = extract_steps(_plan([_sub_goal("sg1", ["stem_separation"])]))
    report = resolve(steps, index)
    assert not report.ok
    assert report.unmappable == []
    assert len(report.unpublished) == 1
    _, gap_cap = report.unpublished[0]
    assert gap_cap.service_action == "audio.stem_separation"


def test_resolution_report_raises_unpublished_tool_name_error_naming_capability():
    client = _RecordingFakeOMSClient(_UNPUBLISHED)
    index = build_capability_index(client)
    steps = extract_steps(_plan([_sub_goal("sg1", ["stem_separation"])]))
    report = resolve(steps, index)

    # Drive the exact point the harness raises the typed gap error.
    with pytest.raises(UnpublishedToolNameError) as exc_info:
        PersistenceService._raise_resolution_error(report)

    named = {cap.service_action for _, cap in exc_info.value.unpublished}
    assert named == {"audio.stem_separation"}
    assert "audio.stem_separation" in str(exc_info.value)


def test_pipeline_surfaces_unpublished_gap_and_never_saves():
    client = _RecordingFakeOMSClient(_UNPUBLISHED)
    service = PersistenceService(
        oms_client=client,
        plan_reader=_FakePlanReader(_plan([_sub_goal("sg1", ["stem_separation"])])),
    )

    report = _run(service.persist("sess-1"))

    assert report.status == "failed"
    assert report.failing_stage == "resolve"
    assert report.errors[0]["error"] == "UnpublishedToolNameError"
    assert report.errors[0]["service_action"] == "audio.stem_separation"
    # Fail-closed: no template ever written to OMS Core.
    assert "save_template" not in client.called_tools
