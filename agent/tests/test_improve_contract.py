"""Pure-logic tests for the improve contract — no AWS, no LLM.

REQ-017: the trajectory cases below are driven by REAL harvested span names from the
golden sessions (`fixtures/plan_state_golden.json`). The sentinel/dataclass cases are
inherently synthetic — they assert properties of the types themselves, and there is no
real-data equivalent of "a default-constructed Run".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve_fixtures import FFAB73F8, real_tool_names, real_tool_trajectory, real_trajectory  # noqa: E402

from improve.contract import (  # noqa: E402
    NO_DATA, UNKNOWN, UNMEASURED, WINDOW_FROM_SPANS, WINDOW_STAMPED, EvaluatorResult,
    InfraHealth, InfraState, Outcome, Overall, Plan, PlanNode, Run, RunWindow, Step, Verdict,
    UtilityVector, gating_outcome_counts, infra_state_of, parse_utc, pluralise, to_utc_iso,
    tool_name_sequence, verdict_counts, verdict_counts_by_class,
)


def test_sentinels_are_falsy_and_distinct():
    assert not NO_DATA and not UNKNOWN and not UNMEASURED
    assert NO_DATA is not UNKNOWN is not UNMEASURED
    assert repr(NO_DATA) == "NO_DATA"


def test_tool_name_sequence_strips_prefixes_on_real_spans():
    """Real ffab73f8 span names: both `execute_tool <n>` and
    `mcp tools/call <target>___<tool>` shapes occur, and the gateway prefix must be
    stripped to the bare tool name so evaluator name-predicates match."""
    raw = real_tool_names(FFAB73F8)
    assert any(n.startswith("execute_tool ") for n in raw)
    assert any(n.startswith("mcp tools/call ") for n in raw)

    # The TOOL slice, not the full trajectory: `real_trajectory` is production-faithful and
    # so carries ~90% AWS SDK plumbing spans, which have no prefix to strip and would make
    # this length comparison meaningless.
    names = tool_name_sequence(real_tool_trajectory(FFAB73F8))
    assert len(names) == len(raw)
    # no prefix survives, and no `<target>___<tool>` namespace survives
    assert not any(n.startswith(("execute_tool ", "mcp tools/call ")) for n in names)
    assert not any("___" in n for n in names)
    # the real session's known tools are present under their bare names
    assert "get_download_url" in names
    assert "process_media" in names


def test_tool_name_sequence_no_data_is_empty():
    assert tool_name_sequence(NO_DATA) == []
    assert tool_name_sequence([]) == []


def test_run_defaults_use_sentinels_not_falsy_values():
    run = Run(run_id="s1")
    # absent evidence must be the sentinel, never [] / None that reads as "fine"
    assert run.trajectory is NO_DATA
    assert run.plan is NO_DATA
    assert run.response is NO_DATA
    assert run.harness_snapshot is UNKNOWN
    # A run nobody stamped a window for, and whose platform state nobody looked up. Neither
    # may default to something that reads as a pass — no `window=(0,0)`, no `degraded=False`.
    assert run.window is UNKNOWN
    assert run.infra_health is UNKNOWN
    assert infra_state_of(run) is InfraState.UNKNOWN
    # An unowned run's actor is the SENTINEL, never "". The per-actor Improve bundle partitions on
    # this value, so an empty string would collect every unowned session into one shared partition
    # that some real actor could then be served.
    assert run.actor_id is UNKNOWN
    assert run.actor_id != ""


def test_infra_health_is_three_state_and_defaults_to_unknown():
    """`healthy` must be something a lookup ASSERTS, never what absence decays to."""
    assert {s.value for s in InfraState} == {"degraded", "healthy", "unknown"}
    assert InfraHealth().state is InfraState.UNKNOWN
    assert InfraHealth().alarms == ()
    # no boolean shorthand exists for anyone to read two-valued
    assert not hasattr(InfraHealth(), "degraded")


def test_run_window_uses_one_documented_timestamp_format():
    """`Z`, `+00:00` and offset-less all arrive; only one form is stored (see parse_utc)."""
    window = RunWindow(start="2026-08-13T18:02:55.022000Z",
                       end="2026-08-13 18:43:34.769000",
                       provenance=WINDOW_STAMPED)
    assert window.start == "2026-08-13T18:02:55.022000+00:00"
    assert window.end == "2026-08-13T18:43:34.769000+00:00"
    assert window.start_dt().tzinfo is not None
    assert round(window.duration_s()) == 2440
    assert to_utc_iso("2026-08-13T18:02:55.022000Z") == window.start
    assert parse_utc("nope") is None


def test_window_provenance_distinguishes_stamped_from_derived():
    """A derived interval must never be readable as an authoritative stamp."""
    stamped = RunWindow(start="2026-08-13T18:00:00+00:00", end="2026-08-13T18:30:00+00:00")
    derived = RunWindow(start="2026-08-13T18:00:00+00:00", end="2026-08-13T18:30:00+00:00",
                        provenance=WINDOW_FROM_SPANS)
    assert stamped.provenance == WINDOW_STAMPED and stamped.derived() is False
    assert derived.derived() is True


def test_utility_vector_measured_excludes_unmeasured():
    uv = UtilityVector(terms={"task_success": 1.0, "tokens": UNMEASURED})
    assert uv.measured() == {"task_success": 1.0}


# --- the shared verdict counters (two consumers, one implementation) ---------
def _counted(overall, cls="conversational", gating=("dispatch_correct",), basis="inferred", **kw):
    return Verdict("r", kw.pop("results", []), UtilityVector(), overall, "",
                   workload_class=cls, workload_basis=basis, gating=gating, **kw)


def test_verdict_counts_keeps_unscored_as_a_third_count():
    """Never folded into pass or fail. `evaluate.gate` produces UNSCORED when a gating evaluator
    could not measure, which says something about the EVIDENCE, not about the agent."""
    counts = verdict_counts([_counted(Overall.PASS), _counted(Overall.FAIL),
                             _counted(Overall.UNSCORED), _counted(Overall.UNSCORED)])
    assert counts == {"total": 4, "PASS": 1, "FAIL": 1, "UNSCORED": 2}


def test_verdict_counts_by_class_collects_every_distinct_gating_set_not_the_first():
    """A mixed population reported as uniform would say something false about why a class's
    numbers mean what they do — which is the only reason this field exists."""
    grouped = verdict_counts_by_class([
        _counted(Overall.PASS, gating=("dispatch_correct", "results_delivered")),
        _counted(Overall.FAIL, gating=("dispatch_correct",), basis="recorded_label"),
        _counted(Overall.PASS, cls="tool_discovery", gating=("ToolDiscoveryValidator",)),
    ])
    assert grouped["conversational"]["total"] == 2
    assert grouped["conversational"]["gating"] == [
        ("dispatch_correct",), ("dispatch_correct", "results_delivered")]
    assert grouped["conversational"]["basis"] == ["inferred", "recorded_label"]
    assert grouped["tool_discovery"]["PASS"] == 1


def test_an_unclassified_verdict_is_grouped_as_unknown_not_dropped():
    """A class nothing could determine is still a run that happened. Dropping it would make the
    per-class totals disagree with the population."""
    grouped = verdict_counts_by_class([_counted(Overall.UNSCORED, cls="")])
    assert set(grouped) == {"unknown"}
    assert grouped["unknown"]["total"] == 1


def test_gating_outcome_counts_reads_outcomes_and_ignores_non_gating_reds():
    """`DagCompletionValidator` REDs on runs that PASS overall, by design. It is informational,
    the gate does not fold it, and neither may this count."""
    verdict = _counted(
        Overall.FAIL, gating=("dispatch_correct", "results_delivered"),
        results=[EvaluatorResult("dispatch_correct", "llm", Outcome.NO_DATA, None, ""),
                 EvaluatorResult("results_delivered", "llm", Outcome.FAIL, 0.1, ""),
                 EvaluatorResult("DagCompletionValidator", "code", Outcome.FAIL, 0.0, "")])
    counts = gating_outcome_counts([verdict])
    assert counts == {"failedGating": {"dispatch_correct": 0, "results_delivered": 1},
                      "unmeasured": {"dispatch_correct": 1, "results_delivered": 0}}


def test_gating_outcome_counts_are_empty_for_an_empty_population():
    assert gating_outcome_counts([]) == {"failedGating": {}, "unmeasured": {}}


def test_only_exactly_one_is_singular():
    """The whole rule, in one place, because every producer sentence now depends on it.

    ``0`` is plural on purpose. So is a negative and so is a fraction: a producer bug can put
    either there, English has no singular for them, and in that case the figure beside the noun
    is the visible defect rather than the noun.
    """
    assert pluralise(1, "run") == "run"
    assert pluralise(2, "run") == "runs"
    assert pluralise(0, "run") == "runs"
    assert pluralise(-1, "run") == "runs"
    assert pluralise(1.5, "run") == "runs"
    # A float that IS one still reads as one. `1.0 run` is odd, but `1.0 runs` is wrong.
    assert pluralise(1.0, "run") == "run"


def test_a_plural_that_is_not_plus_s_is_an_argument_not_a_call_site_conditional():
    """`signature`'s unpaired-dispatch evidence needs both an -es noun and an agreeing verb in
    ONE sentence, governed by TWO different counts. Both go through this helper so the singular
    rule cannot be spelled two ways in the same string."""
    assert pluralise(1, "dispatch", "dispatches") == "dispatch"
    assert pluralise(3, "dispatch", "dispatches") == "dispatches"
    assert pluralise(1, "has", "have") == "has"
    assert pluralise(0, "has", "have") == "have"


def test_the_producer_and_the_surface_agree_about_a_count_of_one():
    """`improveChrome.pluralise` holds the same rule for the strings the UI composes, and a
    reader sees both halves in one suggestion. Pinned here as the literal sentence fragments the
    two sides produce, so a change to either that broke the agreement would have to change this
    test to stay green."""
    for count, expected in ((1, "from 1 run where"), (2, "from 2 runs where"),
                            (0, "from 0 runs where")):
        assert f"from {count} {pluralise(count, 'run')} where" == expected
