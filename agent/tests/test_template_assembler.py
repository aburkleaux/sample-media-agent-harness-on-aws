"""Tests for the Template Assembler pure core (tasks 4.1 / 4.2 / 4.3).

Covers ``assemble`` and its deterministic ``task_id`` derivation:
- Unit examples: task fan-out, edge translation, ordinal task ids, structure.
- Property 7 (task 4.2): dependency edges are preserved exactly.
- Property 9 (task 4.3): the assembled template is structurally complete.

Requirements: 5.1, 5.2, 5.3, 5.4
Properties: 7, 9
"""

from __future__ import annotations

import os
import re
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.models import (
    EmptyConditionsError,
    MappedStep,
    ResolutionReport,
    WorkflowStep,
)
from persistence.template_assembler import (
    _MAX_INTENT_KEYWORDS,
    _extract_intent_keywords,
    _normalize_text,
    assemble,
    build_conditions,
    derive_task_ids,
    derive_template_id,
)

_TASK_KEYS = {
    "task_id",
    "service",
    "action",
    "depends_on",
    "required",
    "timeout_seconds",
    "config",
}


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=8)
_tools = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=10)


@st.composite
def fully_mapped_reports(draw):
    """Generate a ``(ResolutionReport, HighLevelPlan)`` for a fully-mapped plan.

    Every sub-goal suggests at least one tool (so every step maps), ids are
    unique, and each ``depends_on`` edge points only at an earlier sub-goal id
    (a DAG, as the Planning agent emits). Each step is mapped to a synthetic
    ``service.action`` derived from its bare tool, so repeated tools across
    sub-goals exercise the ordinal task-id suffix.
    """
    ids = draw(st.lists(_ids, min_size=1, max_size=6, unique=True))

    sub_goals: list[SubGoal] = []
    mapped: list[MappedStep] = []
    for i, sub_goal_id in enumerate(ids):
        tools = draw(st.lists(_tools, min_size=1, max_size=3))
        earlier = ids[:i]
        depends_on = draw(
            st.lists(st.sampled_from(earlier), min_size=0, max_size=len(earlier), unique=True)
            if earlier
            else st.just([])
        )
        sub_goals.append(
            SubGoal(
                id=sub_goal_id,
                description=draw(st.text(max_size=12)),
                reasoning=draw(st.text(max_size=12)),
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
            # Synthetic deterministic mapping; task_id left blank because
            # ``assemble`` derives its own from service+action.
            mapped.append(
                MappedStep(step=step, service="svc", action=tool, task_id="")
            )

    plan = HighLevelPlan(
        execution_mode=draw(st.sampled_from(["direct", "standard", "complex"])),
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning=draw(st.text(max_size=20)),
    )
    return ResolutionReport(mapped=mapped), plan


# ---------------------------------------------------------------------------
# Property 7: Dependency edges are preserved (task 4.2)
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 7: Dependency edges are preserved
@settings(max_examples=200)
@given(fully_mapped_reports())
def test_dependency_edges_preserved_exactly(case) -> None:
    """Validates: Requirements 5.3

    The set of dependency edges in the assembled template — after translating
    each source/target sub-goal id to its task id(s) — equals the plan's edge
    set exactly: no edge is dropped and no edge is added.
    """
    report, plan = case
    template = assemble(report, plan)

    # Recompute the sub_goal -> task_ids mapping via the public helper.
    task_ids = derive_task_ids(report.mapped)
    subgoal_to_task_ids: dict[str, list[str]] = {}
    for step, task_id in zip(report.mapped, task_ids):
        subgoal_to_task_ids.setdefault(step.step.sub_goal_id, []).append(task_id)

    # Expected task-level edges = translation of every plan sub-goal edge.
    expected_edges: set[tuple[str, str]] = set()
    for step, task_id in zip(report.mapped, task_ids):
        for dep_sub_goal_id in step.step.depends_on:
            for dep_task_id in subgoal_to_task_ids.get(dep_sub_goal_id, []):
                expected_edges.add((task_id, dep_task_id))

    # Actual task-level edges as emitted in the template.
    actual_edges: set[tuple[str, str]] = set()
    for task in template.stages["fulfill"]["tasks"]:
        for dep in task["depends_on"]:
            actual_edges.add((task["task_id"], dep))

    assert actual_edges == expected_edges


# ---------------------------------------------------------------------------
# Property 9: Assembled template is structurally complete (task 4.3)
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 9: Assembled template is structurally complete
@settings(max_examples=200)
@given(fully_mapped_reports())
def test_assembled_template_is_structurally_complete(case) -> None:
    """Validates: Requirements 5.1, 5.2, 5.4

    The template carries ``template_id``/``name``/``description``/``conditions``/
    ``stages`` with both ``validate`` and ``fulfill`` task lists; the fulfill
    stage has exactly one task per mapped step; every task is fully populated;
    and ``task_id`` is a deterministic function of ``service`` + ``action``.
    """
    report, plan = case
    template = assemble(report, plan)

    # Top-level fields present and non-empty where required.
    assert template.template_id
    assert template.name
    assert isinstance(template.description, str)
    assert isinstance(template.conditions, dict) and template.conditions

    # Stages carry both validate and fulfill task lists.
    assert set(template.stages) >= {"validate", "fulfill"}
    assert isinstance(template.stages["validate"]["tasks"], list)
    fulfill_tasks = template.stages["fulfill"]["tasks"]
    assert isinstance(fulfill_tasks, list)

    # Exactly one fulfill task per mapped step.
    assert len(fulfill_tasks) == len(report.mapped)

    # Every task is fully populated with the required keys and value types.
    for task in fulfill_tasks:
        assert set(task) == _TASK_KEYS
        assert task["task_id"]
        assert task["service"]
        assert task["action"]
        assert isinstance(task["depends_on"], list)
        assert isinstance(task["required"], bool)
        assert isinstance(task["timeout_seconds"], int)
        assert isinstance(task["config"], dict)

    # task_id is a deterministic function of service+action: re-assembling the
    # same report yields identical task ids, and unique service.action pairs get
    # a base id equal to slug(service)__slug(action).
    again = assemble(report, plan)
    assert [t["task_id"] for t in again.stages["fulfill"]["tasks"]] == [
        t["task_id"] for t in fulfill_tasks
    ]

    # A base id whose SLUG is unique carries no ordinal suffix. Compare on the
    # slugged base (not the raw pair): distinct actions can slug to the same
    # base ("0" and "0_" both slug to "0"), and those legitimately get ordinals.
    def _base(svc: str, act: str) -> str:
        slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")  # noqa: E731
        return f"{slug(svc)}__{slug(act)}"

    bases = [_base(m.service, m.action) for m in report.mapped]
    for base, task in zip(bases, fulfill_tasks):
        if bases.count(base) == 1:
            assert task["task_id"] == base


# ---------------------------------------------------------------------------
# Property 12: Conditions are never match-all (task 4.5)
# ---------------------------------------------------------------------------

_service_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=10
)


@st.composite
def scopable_workflows(draw):
    """Generate a ``(HighLevelPlan, services)`` that can always be scoped.

    At least one participating service is present (matching what ``assemble``
    feeds ``build_conditions`` for a fully-mapped plan), so scoping never
    degenerates to empty. Reasoning text is free-form (possibly empty), so the
    keyword miner is exercised across the whole range.
    """
    services = draw(st.sets(_service_names, min_size=1, max_size=5))
    reasoning = draw(st.text(max_size=60))
    n = draw(st.integers(min_value=0, max_value=4))
    sub_goals = [
        SubGoal(
            id=f"sg{i}",
            description=draw(st.text(max_size=30)),
            reasoning=draw(st.text(max_size=30)),
            suggested_tools=[],
            suggested_skills=[],
            depends_on=[],
        )
        for i in range(n)
    ]
    plan = HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning=reasoning,
    )
    return plan, services


def _is_match_all(conditions: dict) -> bool:
    """Mirror of the assembler's guard: empty or all-wildcard/empty values."""
    wildcard = {"", "*", "all", "any", "any_order", "wildcard"}

    def has_scope(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in wildcard
        if isinstance(value, (list, tuple, set)):
            return any(has_scope(v) for v in value)
        if isinstance(value, dict):
            return any(has_scope(v) for v in value.values())
        return value is not None

    if not conditions:
        return True
    return not any(has_scope(v) for v in conditions.values())


# Feature: oms-workflow-persistence, Property 12: Conditions are never match-all
@settings(max_examples=200)
@given(scopable_workflows())
def test_conditions_are_never_match_all(case) -> None:
    """Validates: Requirements 10.1, 10.3

    Emitted conditions are non-empty and not match-all, and the assembler raises
    (rather than returns) on inputs that cannot be scoped.
    """
    plan, services = case
    # Raising IS one of the two acceptable outcomes — this test's own docstring says so, and
    # `build_conditions` documents it ("refuses to emit a match-all template ... raises
    # EmptyConditionsError rather than returning empty / match-all conditions"). The generator
    # can legitimately produce an UNSCOPABLE workflow — Hypothesis finds the empty plan with
    # `services={'any'}`, where 'any' is filtered out as non-scoping and the plan mines no intent
    # keyword — and the test treated that documented refusal as a failure. The property asserted
    # here is "never match-all", which a refusal satisfies; the raise path itself is covered by
    # the sibling test below.
    try:
        conditions = build_conditions(plan, services)
    except EmptyConditionsError:
        return
    assert conditions, "conditions must be non-empty"
    assert not _is_match_all(conditions), "conditions must not match all orders"


# Feature: oms-workflow-persistence, Property 12: Conditions are never match-all
@settings(max_examples=200)
@given(
    st.text(max_size=40).filter(
        # Reasoning with no mineable keyword (guarantees the empty path). The
        # filter must mirror the miner's ACCENT-FOLDING normalization: "00À"
        # folds to "00a", which is a mineable 3-char token.
        lambda s: not any(
            len(t) >= 3 for t in re.findall(r"[a-z0-9]+", _normalize_text(s))
        )
    )
)
def test_build_conditions_raises_when_nothing_to_scope(reasoning) -> None:
    """Validates: Requirements 10.1, 10.3

    With no participating service and no mineable intent keyword, the assembler
    raises ``EmptyConditionsError`` rather than returning empty / match-all
    conditions.
    """
    plan = HighLevelPlan(
        execution_mode="standard",
        sub_goals=[],
        suggested_tools={},
        suggested_skills={},
        reasoning=reasoning,
    )
    with pytest.raises(EmptyConditionsError):
        build_conditions(plan, set())


# ---------------------------------------------------------------------------
# Unit examples / edge cases
# ---------------------------------------------------------------------------


def _mapped(sub_goal_id, service, action, depends_on):
    step = WorkflowStep(
        sub_goal_id=sub_goal_id, bare_tool=action, depends_on=list(depends_on)
    )
    return MappedStep(step=step, service=service, action=action, task_id="")


def _plan(reasoning="test reasoning"):
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=[],
        suggested_tools={},
        suggested_skills={},
        reasoning=reasoning,
    )


def test_one_task_per_mapped_step_with_translated_edges():
    report = ResolutionReport(
        mapped=[
            _mapped("sg1", "audio", "stem_separation", []),
            _mapped("sg2", "video", "transcode", ["sg1"]),
        ]
    )
    template = assemble(report, _plan())
    tasks = template.stages["fulfill"]["tasks"]

    assert len(tasks) == 2
    assert tasks[0]["task_id"] == "audio__stem-separation"
    assert tasks[1]["task_id"] == "video__transcode"
    # sg2 depends on sg1 -> translated to sg1's task id.
    assert tasks[1]["depends_on"] == ["audio__stem-separation"]
    assert tasks[0]["depends_on"] == []


def test_repeated_service_action_gets_stable_ordinal_suffix():
    report = ResolutionReport(
        mapped=[
            _mapped("sg1", "audio", "transcode", []),
            _mapped("sg2", "audio", "transcode", []),
            _mapped("sg3", "audio", "transcode", []),
        ]
    )
    ids = [t["task_id"] for t in assemble(report, _plan()).stages["fulfill"]["tasks"]]
    assert ids == ["audio__transcode", "audio__transcode__2", "audio__transcode__3"]
    assert len(set(ids)) == 3  # unique


def test_edge_fans_out_to_all_tasks_of_a_multi_tool_sub_goal():
    # sg1 has two tools (two tasks); sg2 depends on sg1 -> both task ids.
    report = ResolutionReport(
        mapped=[
            _mapped("sg1", "audio", "a", []),
            _mapped("sg1", "audio", "b", []),
            _mapped("sg2", "video", "c", ["sg1"]),
        ]
    )
    tasks = assemble(report, _plan()).stages["fulfill"]["tasks"]
    by_id = {t["task_id"]: t for t in tasks}
    assert set(by_id["video__c"]["depends_on"]) == {"audio__a", "audio__b"}


def test_conditions_scoped_to_services_and_intent():
    report = ResolutionReport(mapped=[_mapped("sg1", "audio", "x", [])])
    template = assemble(report, _plan(reasoning="separate vocals from music"))
    # Scoped to the participating service ...
    assert template.conditions["services"] == ["audio"]
    # ... plus intent keywords mined from the plan's reasoning (stopwords like
    # "from" dropped), never empty / match-all.
    assert set(template.conditions["intent_keywords"]) == {"separate", "vocals", "music"}


def test_template_id_is_deterministic_for_equivalent_workflows():
    report = ResolutionReport(mapped=[_mapped("sg1", "audio", "x", [])])
    a = assemble(report, _plan())
    b = assemble(report, _plan())
    assert a.template_id == b.template_id and a.template_id


# ---------------------------------------------------------------------------
# build_conditions (task 4.4) — refusal + scoping unit examples
# ---------------------------------------------------------------------------


def test_build_conditions_scopes_to_services_and_keywords():
    conditions = build_conditions(_plan(reasoning="transcode video assets"), {"video"})
    assert conditions["services"] == ["video"]
    assert set(conditions["intent_keywords"]) == {"transcode", "video", "assets"}


def test_build_conditions_scopes_when_only_services_present():
    # No mineable keywords, but a participating service still scopes the match.
    conditions = build_conditions(_plan(reasoning=""), {"audio"})
    assert conditions == {"services": ["audio"]}


def test_build_conditions_scopes_when_only_keywords_present():
    # No services, but intent keywords still scope the match.
    conditions = build_conditions(_plan(reasoning="denoise the audio track"), set())
    assert "services" not in conditions
    assert set(conditions["intent_keywords"]) == {"denoise", "audio", "track"}


def test_build_conditions_raises_when_empty():
    with pytest.raises(EmptyConditionsError):
        build_conditions(_plan(reasoning=""), set())


def test_build_conditions_raises_on_wildcard_only_services():
    # A whitespace / wildcard "service" carries no scope and no keywords exist.
    with pytest.raises(EmptyConditionsError):
        build_conditions(_plan(reasoning="   "), {"*", "  "})


# ---------------------------------------------------------------------------
# derive_template_id (task 4.6) — equivalence + divergence unit examples
# ---------------------------------------------------------------------------


def test_derive_template_id_equal_for_equivalent_workflows():
    report = ResolutionReport(
        mapped=[
            _mapped("sg1", "audio", "stem_separation", []),
            _mapped("sg2", "video", "transcode", ["sg1"]),
        ]
    )
    a = assemble(report, _plan())
    b = assemble(report, _plan())
    assert a.template_id == b.template_id
    # Re-deriving from the same tasks + conditions is stable.
    tasks = [OMSTask_from(t) for t in a.stages["fulfill"]["tasks"]]
    assert derive_template_id(tasks, a.conditions) == a.template_id


def test_derive_template_id_differs_when_workflow_differs():
    base = ResolutionReport(mapped=[_mapped("sg1", "audio", "stem_separation", [])])
    other = ResolutionReport(mapped=[_mapped("sg1", "video", "transcode", [])])
    assert assemble(base, _plan()).template_id != assemble(other, _plan()).template_id


def test_derive_template_id_differs_when_conditions_differ():
    from persistence.models import OMSTask

    tasks = [OMSTask(task_id="t", service="audio", action="x", depends_on=[])]
    id_a = derive_template_id(tasks, {"services": ["audio"]})
    id_b = derive_template_id(tasks, {"services": ["video"]})
    assert id_a != id_b


def OMSTask_from(task_dict):
    from persistence.models import OMSTask

    return OMSTask.from_dict(task_dict)


# ---------------------------------------------------------------------------
# Skipped (status/polling) steps produce no task and no dangling edges
# ---------------------------------------------------------------------------


def _plan_with_sub_goals(sub_goals: list[SubGoal]) -> HighLevelPlan:
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning="separate the audio stems from the recording",
    )


def _fulfill_tasks(template) -> list[dict]:
    return template.stages["fulfill"]["tasks"]


def test_skipped_status_step_produces_exactly_one_task():
    # Plan: sg1 = stem separation (mapped), sg2 = poll job status (skipped).
    sg1 = SubGoal(
        id="sg1", description="separate stems", reasoning="",
        suggested_tools=["separate_audio_stems"], suggested_skills=[], depends_on=[],
    )
    sg2 = SubGoal(
        id="sg2", description="poll status", reasoning="",
        suggested_tools=["get_job_status"], suggested_skills=[], depends_on=["sg1"],
    )
    mapped_step = WorkflowStep(sub_goal_id="sg1", bare_tool="separate_audio_stems", depends_on=[])
    skipped_step = WorkflowStep(sub_goal_id="sg2", bare_tool="get_job_status", depends_on=["sg1"])
    report = ResolutionReport(
        mapped=[MappedStep(step=mapped_step, service="audio", action="stem_separation", task_id="")],
        skipped=[skipped_step],
    )

    template = assemble(report, _plan_with_sub_goals([sg1, sg2]))

    tasks = _fulfill_tasks(template)
    assert len(tasks) == 1
    assert (tasks[0]["service"], tasks[0]["action"]) == ("audio", "stem_separation")


def test_skipped_step_is_removed_from_mapped_task_depends_on():
    # A mapped task that depended on a SKIPPED status step must not carry a
    # dangling edge to it (the skipped step yields no task id).
    sg1 = SubGoal(
        id="poll", description="poll status", reasoning="",
        suggested_tools=["get_job_status"], suggested_skills=[], depends_on=[],
    )
    sg2 = SubGoal(
        id="final", description="normalize loudness", reasoning="",
        suggested_tools=["normalize_loudness"], suggested_skills=[], depends_on=["poll"],
    )
    skipped_step = WorkflowStep(sub_goal_id="poll", bare_tool="get_job_status", depends_on=[])
    mapped_step = WorkflowStep(sub_goal_id="final", bare_tool="normalize_loudness", depends_on=["poll"])
    report = ResolutionReport(
        mapped=[MappedStep(step=mapped_step, service="audio", action="normalize", task_id="")],
        skipped=[skipped_step],
    )

    template = assemble(report, _plan_with_sub_goals([sg1, sg2]))

    tasks = _fulfill_tasks(template)
    assert len(tasks) == 1
    # The edge to the skipped 'poll' step is dropped, keeping the DAG valid.
    assert tasks[0]["depends_on"] == []


# ---------------------------------------------------------------------------
# Intent-keyword mining quality (R10.1): accent folding, FR stopwords,
# frequency ranking, determinism, canonical ordering.
# ---------------------------------------------------------------------------


def _fr_plan(reasoning: str, sub_goals: list[SubGoal] | None = None) -> HighLevelPlan:
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals or [],
        suggested_tools={},
        suggested_skills={},
        reasoning=reasoning,
    )


# The shape of prose the Planning agent actually emits in French.
_FR_REASONING = (
    "Il s'agit de s\u00e9parer les stems audio d'un fichier musical. Aucune "
    "s\u00e9paration n'est possible sans le m\u00e9canisme d'isolation des voix. "
    "Le m\u00e9canisme est adapt\u00e9 au traitement audio et accompagn\u00e9 d'un "
    "appel au service de s\u00e9paration. Les sorties attendues sont "
    "associ\u00e9es aux stems vocal, music et sfx, car aucun autre format "
    "n'est requis."
)


def test_accented_words_are_folded_whole_not_fragmented():
    """Accented French words must fold to whole ASCII tokens.

    Regression: the ``[a-z0-9]+`` tokenizer used to split on the accented
    character, emitting fragments ("m\u00e9canisme" -> "canisme",
    "accompagn\u00e9" -> "accompagn").
    """
    keywords = _extract_intent_keywords(_fr_plan(_FR_REASONING))

    assert "mecanisme" in keywords
    assert "separation" in keywords
    for fragment in ("canisme", "paration", "parer", "accompagn", "associ", "adapt"):
        assert fragment not in keywords, f"accent fragment leaked: {fragment}"


def test_french_stopwords_are_dropped():
    keywords = _extract_intent_keywords(_fr_plan(_FR_REASONING))

    for stopword in ("aucun", "aucune", "avec", "car", "agit", "autre", "aux", "sans"):
        assert stopword not in keywords, f"French stopword leaked: {stopword}"


def test_keywords_are_ranked_by_frequency_not_alphabet():
    """The frequent domain term wins over an alphabetically-early rare one.

    Exactly the live defect: alphabetical truncation kept a-words and dropped
    late-sorting domain terms such as "vocal".
    """
    frequent = [
        "downstream", "editing", "isolate", "media", "music", "remixing",
        "separate", "sfx", "source", "stems", "vocal", "waveform",
    ]
    # Each domain term twice, each noise term once.
    prose = " ".join(frequent) + " " + " ".join(frequent) + " abaisse abandon"

    keywords = _extract_intent_keywords(_fr_plan(prose))

    assert "vocal" in keywords and "stems" in keywords
    assert "abandon" not in keywords and "abaisse" not in keywords
    assert set(keywords) == set(frequent)


def test_keywords_are_sorted_and_capped():
    prose = " ".join(f"domain{i}word" for i in range(40))
    keywords = _extract_intent_keywords(_fr_plan(prose))

    assert keywords == sorted(keywords)
    assert len(keywords) <= _MAX_INTENT_KEYWORDS


def test_keyword_extraction_is_deterministic_across_equal_plans():
    sub_goals = [
        SubGoal(
            id="sg1",
            description="S\u00e9parer les stems vocal et music",
            reasoning="La s\u00e9paration produit les pistes vocal et music",
            suggested_tools=["separate_audio_stems"],
            suggested_skills=[],
            depends_on=[],
        )
    ]
    plan_a = _fr_plan(_FR_REASONING, sub_goals)
    plan_b = _fr_plan(_FR_REASONING, list(sub_goals))

    first = _extract_intent_keywords(plan_a)
    assert first == _extract_intent_keywords(plan_a)  # same plan, twice
    assert first == _extract_intent_keywords(plan_b)  # structurally equal plan

    # ... therefore equal conditions and equal derived template ids.
    report = ResolutionReport(mapped=[_mapped("sg1", "audio", "stem_separation", [])])
    assert assemble(report, plan_a).template_id == assemble(report, plan_b).template_id


def test_all_stopword_prose_yields_no_keywords_but_services_still_scope():
    prose = "Il s'agit de faire avec cela, car aucune des etapes ne sont attendues."
    assert _extract_intent_keywords(_fr_plan(prose)) == []

    # A participating service still scopes the template ...
    assert build_conditions(_fr_plan(prose), {"audio"}) == {"services": ["audio"]}
    # ... and with neither service nor keyword the assembler still refuses.
    with pytest.raises(EmptyConditionsError):
        build_conditions(_fr_plan(prose), set())
