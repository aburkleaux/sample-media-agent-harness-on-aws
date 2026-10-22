"""Unit tests for the deterministic step -> Capability join (task 3.3).

Covers the pure ``resolve(steps, index) -> ResolutionReport`` classifier:
- mapped (exactly one published match) with the ``f"{service}__{action}"`` id.
- unmappable (no match, and tool-less ``bare_tool is None`` nodes, R2.3).
- ambiguous (>1 published match, conflicting capabilities recorded, R3.4).
- unpublished (needed capability present but no published tool name, R4.2/R4.3),
  distinct from generic unmappable.
- no fuzzy/case-insensitive/substring matching (R3.5).
- aggregate ``ok`` and itemization of every offender (R2.2, R12.3).

Requirements: 2.1, 2.2, 2.3, 3.3, 3.4, 3.5, 4.2, 4.3
"""

from __future__ import annotations

import os
import sys

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.capability_resolver import resolve
from persistence.models import (
    Capability,
    CapabilityIndex,
    ResolutionReport,
    WorkflowStep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index(capabilities: list[Capability]) -> CapabilityIndex:
    """Build a CapabilityIndex with a bare-name lookup over *published* caps only.

    Mirrors the invariant of the (not-yet-implemented) ``build_capability_index``:
    the lookup indexes only Capabilities that publish a bare tool name; the full
    ``capabilities`` list still carries unpublished ones for gap detection.
    """
    lookup: dict[str, list[Capability]] = {}
    for cap in capabilities:
        if cap.bare_tool_name is not None:
            lookup.setdefault(cap.bare_tool_name, []).append(cap)
    return CapabilityIndex(capabilities=list(capabilities), lookup=lookup)


def _published(service: str, action: str, bare: str) -> Capability:
    return Capability(
        service=service,
        action=action,
        gateway_tool_name=f"{service}-tgt___{bare}",
        bare_tool_name=bare,
    )


# ---------------------------------------------------------------------------
# mapped
# ---------------------------------------------------------------------------


def test_single_exact_match_is_mapped_with_derived_task_id():
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="separate_audio_stems")

    report = resolve([step], _index([cap]))

    assert report.ok
    assert len(report.mapped) == 1
    mapped = report.mapped[0]
    assert mapped.service == "audio"
    assert mapped.action == "stem_separation"
    # task_id follows the assembler's f"{service}__{action}" convention (R5.4).
    assert mapped.task_id == "audio__stem_separation"
    assert not report.unmappable and not report.ambiguous and not report.unpublished


# ---------------------------------------------------------------------------
# unmappable
# ---------------------------------------------------------------------------


def test_no_match_is_unmappable():
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="transcode_video")

    report = resolve([step], _index([cap]))

    assert not report.ok
    assert [s.sub_goal_id for s in report.unmappable] == ["sg1"]
    assert not report.mapped and not report.ambiguous and not report.unpublished


def test_tool_less_step_is_unmappable():
    # A pure agent/model/prompt node carries no tool reference (R2.3).
    step = WorkflowStep(sub_goal_id="sg-agent", bare_tool=None)

    report = resolve([step], _index([]))

    assert not report.ok
    assert [s.sub_goal_id for s in report.unmappable] == ["sg-agent"]


# ---------------------------------------------------------------------------
# ambiguous
# ---------------------------------------------------------------------------


def test_multiple_matches_are_ambiguous_and_record_conflicts():
    cap_a = _published("audio", "stem_separation", "separate_audio_stems")
    cap_b = _published("music", "split", "separate_audio_stems")
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="separate_audio_stems")

    report = resolve([step], _index([cap_a, cap_b]))

    assert not report.ok
    assert len(report.ambiguous) == 1
    offender, conflicts = report.ambiguous[0]
    assert offender.sub_goal_id == "sg1"
    # Both conflicting Capabilities are recorded so a human can disambiguate.
    assert set((c.service, c.action) for c in conflicts) == {
        ("audio", "stem_separation"),
        ("music", "split"),
    }
    assert not report.mapped and not report.unmappable and not report.unpublished


# ---------------------------------------------------------------------------
# unpublished — distinct dependency gap (R4.2/R4.3)
# ---------------------------------------------------------------------------


def test_separatorless_tool_name_is_unpublished_not_unmappable():
    # Capability exists in the registry but its Gateway_Tool_Name has no ``___``
    # separator, so it publishes no usable bare name -> unpublished gap.
    cap = Capability(
        service="audio",
        action="stem_separation",
        gateway_tool_name="separate_audio_stems",  # separator-less
        bare_tool_name=None,
    )
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="separate_audio_stems")

    report = resolve([step], _index([cap]))

    assert not report.ok
    assert len(report.unpublished) == 1
    offender, gap_cap = report.unpublished[0]
    assert offender.sub_goal_id == "sg1"
    assert gap_cap is cap
    # Crucially NOT reported as generic unmappable.
    assert not report.unmappable
    assert not report.mapped


def test_missing_tool_name_uses_action_intent_as_unpublished():
    # No Gateway_Tool_Name at all -> the action is the capability's intended bare
    # identity; a step naming it is an unpublished gap, not unmappable.
    cap = Capability(
        service="audio",
        action="separate_audio_stems",
        gateway_tool_name=None,
        bare_tool_name=None,
    )
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="separate_audio_stems")

    report = resolve([step], _index([cap]))

    assert not report.ok
    assert [s.sub_goal_id for s, _ in report.unpublished] == ["sg1"]
    assert not report.unmappable


# ---------------------------------------------------------------------------
# no fuzzy / case / substring matching (R3.5)
# ---------------------------------------------------------------------------


def test_case_mismatch_never_maps():
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="Separate_Audio_Stems")

    report = resolve([step], _index([cap]))

    assert not report.mapped
    assert [s.sub_goal_id for s in report.unmappable] == ["sg1"]


def test_substring_never_maps():
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    step = WorkflowStep(sub_goal_id="sg1", bare_tool="separate")

    report = resolve([step], _index([cap]))

    assert not report.mapped
    assert [s.sub_goal_id for s in report.unmappable] == ["sg1"]


# ---------------------------------------------------------------------------
# aggregate / mixed report
# ---------------------------------------------------------------------------


def test_mixed_report_itemizes_every_bucket():
    mapped_cap = _published("audio", "stem_separation", "separate_audio_stems")
    amb_a = _published("music", "split", "mix_tracks")
    amb_b = _published("dj", "blend", "mix_tracks")
    unpub_cap = Capability(
        service="video",
        action="transcode",
        gateway_tool_name="transcode_video",  # separator-less
        bare_tool_name=None,
    )
    steps = [
        WorkflowStep(sub_goal_id="m", bare_tool="separate_audio_stems"),
        WorkflowStep(sub_goal_id="a", bare_tool="mix_tracks"),
        WorkflowStep(sub_goal_id="u", bare_tool="transcode_video"),
        WorkflowStep(sub_goal_id="x", bare_tool="does_not_exist"),
        WorkflowStep(sub_goal_id="n", bare_tool=None),
    ]

    report = resolve(steps, _index([mapped_cap, amb_a, amb_b, unpub_cap]))

    assert not report.ok
    assert [m.step.sub_goal_id for m in report.mapped] == ["m"]
    assert {s.sub_goal_id for s, _ in report.ambiguous} == {"a"}
    assert {s.sub_goal_id for s, _ in report.unpublished} == {"u"}
    assert {s.sub_goal_id for s in report.unmappable} == {"x", "n"}


def test_all_mapped_is_ok():
    caps = [
        _published("audio", "stem", "a_tool"),
        _published("video", "cut", "b_tool"),
    ]
    steps = [
        WorkflowStep(sub_goal_id="s1", bare_tool="a_tool"),
        WorkflowStep(sub_goal_id="s2", bare_tool="b_tool"),
    ]

    report = resolve(steps, _index(caps))

    assert report.ok
    assert isinstance(report, ResolutionReport)
    assert len(report.mapped) == 2


# ---------------------------------------------------------------------------
# skipped — status/polling tools OMS handles internally (not unmappable)
# ---------------------------------------------------------------------------


def _index_with_status(
    capabilities: list[Capability], status_tool_bare_names: set[str]
) -> CapabilityIndex:
    """Like ``_index`` but also carries the status-tool bare-name set."""
    idx = _index(capabilities)
    return CapabilityIndex(
        capabilities=idx.capabilities,
        lookup=idx.lookup,
        status_tool_bare_names=set(status_tool_bare_names),
    )


def test_status_tool_step_is_skipped_not_unmappable():
    # audio publishes stem_separation as separate_audio_stems and polls its job
    # status via get_job_status. A step naming the status tool must be SKIPPED.
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    index = _index_with_status([cap], {"get_job_status"})
    steps = [
        WorkflowStep(sub_goal_id="sep", bare_tool="separate_audio_stems"),
        WorkflowStep(sub_goal_id="poll", bare_tool="get_job_status"),
    ]

    report = resolve(steps, index)

    # A status/poll step never fails resolution: only mapped + skipped -> ok.
    assert report.ok
    assert [m.step.sub_goal_id for m in report.mapped] == ["sep"]
    assert [s.sub_goal_id for s in report.skipped] == ["poll"]
    assert not report.unmappable and not report.ambiguous and not report.unpublished


def test_unknown_tool_is_still_unmappable_even_with_status_set():
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    index = _index_with_status([cap], {"get_job_status"})
    step = WorkflowStep(sub_goal_id="x", bare_tool="does_not_exist")

    report = resolve([step], index)

    assert not report.ok
    assert [s.sub_goal_id for s in report.unmappable] == ["x"]
    assert not report.skipped


def test_capability_match_beats_status_tool_name_collision():
    # A real published capability whose bare name also appears in the status set
    # must map (capability match wins over status-tool classification).
    cap = _published("audio", "stem_separation", "separate_audio_stems")
    index = _index_with_status([cap], {"separate_audio_stems"})
    step = WorkflowStep(sub_goal_id="sep", bare_tool="separate_audio_stems")

    report = resolve([step], index)

    assert report.ok
    assert [m.step.sub_goal_id for m in report.mapped] == ["sep"]
    assert not report.skipped


def test_ambiguity_still_detected_with_status_set():
    cap_a = _published("audio", "stem_separation", "mix_tracks")
    cap_b = _published("music", "split", "mix_tracks")
    index = _index_with_status([cap_a, cap_b], {"get_job_status"})
    step = WorkflowStep(sub_goal_id="a", bare_tool="mix_tracks")

    report = resolve([step], index)

    assert not report.ok
    assert {s.sub_goal_id for s, _ in report.ambiguous} == {"a"}
    assert not report.skipped
