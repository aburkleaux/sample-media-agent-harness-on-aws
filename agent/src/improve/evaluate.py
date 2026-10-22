"""Score a Run into a universal Verdict.

The run's WORKLOAD CLASS decides which evaluators run and which gate (`classify.py`),
because the harness serves several kinds of invocation and most have no user goal. The goal
judge returns two tiers (dispatch_correct, results_delivered) and both gate a conversational
run, so overall FAILs if the agent misrouted OR the usable result never arrived. Per-tier
scores stay visible so it's unambiguous which failed. Config still overrides the class for
the conversational case, which is what its flat keys were written for.

can-show-RED is evidence-intrinsic: an evaluator that needs evidence the run
should have but doesn't returns NO_DATA (RED) from the run's own shape, never a
scenario-label-gated pass. UNSCORED is never PASS.

**Could-not-measure is not a failure.** `gate` distinguishes three states, and the
distinction is load-bearing rather than cosmetic. NO_DATA on a gating evaluator used to land
in the same bucket as FAIL while also counting as "a scorable outcome was produced", which
made `Overall.UNSCORED` unreachable in practice: measured on the 50-session harvest, 0 runs
were UNSCORED and 30 of 35 FAILs were the judge reporting that it had no conversation to
read. Those 30 then entered mining as failures. So an unmeasurable run is now UNSCORED, it
is never a negative control, and it surfaces as an evidence gap.
"""
from __future__ import annotations

from typing import Optional

try:
    from .classify import Workload, WorkloadClass, classify, evaluators_for, gating_for
    from .contract import (
        NO_DATA, UNMEASURED, EvaluatorResult, Outcome, Overall, Run, UtilityVector, Verdict,
        tool_name_sequence,
    )
    from .evaluators_improve import (
        CODE_EVALUATORS, _is_dispatch, async_job_flow, control_plane_op, dag_completion,
        discovery_elapsed_ms, tool_discovery,
    )
    from .judge import DEFAULT_GOAL_SOURCE, DEFAULT_INPUT_SET, DEFAULT_PASS_THRESHOLD, GoalSuccessJudge, JudgeConfig
except ImportError:
    from improve.classify import Workload, WorkloadClass, classify, evaluators_for, gating_for  # type: ignore
    from improve.contract import (  # type: ignore
        NO_DATA, UNMEASURED, EvaluatorResult, Outcome, Overall, Run, UtilityVector, Verdict,
        tool_name_sequence,
    )
    from improve.evaluators_improve import (  # type: ignore
        CODE_EVALUATORS, _is_dispatch, async_job_flow, control_plane_op, dag_completion,
        discovery_elapsed_ms, tool_discovery,
    )
    from improve.judge import DEFAULT_GOAL_SOURCE, DEFAULT_INPUT_SET, DEFAULT_PASS_THRESHOLD, GoalSuccessJudge, JudgeConfig  # type: ignore


# Both tiers gate: overall FAILs if the agent misrouted (dispatch) OR the usable
# result never arrived (delivery). Per-tier scores stay visible in the verdict.
DEFAULT_GATING = ("dispatch_correct", "results_delivered")
DEFAULT_EVALUATORS = ("goal_judge", "AsyncJobFlowValidator", "DagCompletionValidator")


def _run_dispatched_async(run: Run) -> bool:
    """Does the trajectory show the run kicked off async/DAG work? Used to tell
    'missing plan evidence' (RED) from 'genuinely no DAG' (NOT_APPLICABLE)."""
    if run.trajectory is NO_DATA:
        return False
    return any(_is_dispatch(n) for n in tool_name_sequence(run.trajectory))


def _code_evaluator(name: str, run: Run) -> EvaluatorResult:
    # Evidence-intrinsic RED: the evaluator decides applicability + missingness
    # from the run's own shape, not from a scenario label (REQ-002, live path).
    if name == "AsyncJobFlowValidator":
        return async_job_flow(tool_name_sequence(run.trajectory),
                              trajectory_present=(run.trajectory is not NO_DATA))
    if name == "DagCompletionValidator":
        return dag_completion(run.plan, dispatched_async=_run_dispatched_async(run))
    if name == "ToolDiscoveryValidator":
        return tool_discovery(run)
    if name == "ControlPlaneOpValidator":
        return control_plane_op(run)
    # Unreachable for anything in CODE_EVALUATORS; the assertion below is what keeps that
    # true, because a name in the registry with no branch here would otherwise return
    # NO_DATA and read as missing evidence rather than as a wiring mistake.
    assert name not in CODE_EVALUATORS, f"{name} is registered but has no dispatch branch"
    return EvaluatorResult(name, "code", Outcome.NO_DATA, 0.0, "Unknown evaluator.")


def _utility(run: Run, results: list[EvaluatorResult],
             workload: Optional[Workload] = None) -> UtilityVector:
    def score_of(name):
        return next((r.score for r in results if r.name == name and r.score is not None), None)
    dispatch = score_of("dispatch_correct")
    delivered = score_of("results_delivered")
    terms = {
        "dispatch_correct": dispatch if dispatch is not None else UNMEASURED,
        "results_delivered": delivered if delivered is not None else UNMEASURED,
        "latency_ms": run.resources.get("latency_ms", UNMEASURED),
        "tokens": run.resources.get("tokens", UNMEASURED),
        "cost": run.resources.get("cost", UNMEASURED),
    }
    # Warm-up cost, for the class where it IS the whole invocation. A trend term, not a gate:
    # see `evaluators_improve.discovery_elapsed_ms` for why no threshold is asserted.
    #
    # TOOL_DISCOVERY only, deliberately, even though `control_plane` and `conversational`
    # sessions also perform handshakes. The term is the root invocation's duration, and that
    # only MEANS "warm-up" when warm-up is all the invocation did. For a lab session with 25
    # invocations it would be the longest CRUD operation, and for a conversation it would be
    # the whole turn. Emitting it there would put three different quantities under one name.
    if workload is not None and workload.cls is WorkloadClass.TOOL_DISCOVERY:
        elapsed = discovery_elapsed_ms(run)
        terms["discovery_ms"] = elapsed if elapsed is not None else UNMEASURED
    return UtilityVector(terms=terms)


def resolve_plan(run: Run, config: Optional[dict] = None) -> tuple:
    """Which evaluators run for this run, which gate, and the class behind that choice.

    Returns ``(names, gating, workload)``. One function so that the live path and the
    recorded path cannot disagree: they used to source the evaluator list differently, and a
    config-level list applied in one mode and was ignored in the other.

    Precedence, and why:

    1. **The workload class**, for every class except conversational. A `tool_discovery`
       invocation has no request to dispatch, so `dispatch_correct` cannot be measured for
       it and the config's flat keys must not be able to say otherwise.
    2. **Config**, for the conversational class. Its flat `evaluators` /
       `gating_evaluators` keys describe exactly that class, and remain the knob for it.
    3. **The scenario**, then the module defaults, for a conversational run with no config.
    """
    config = config or {}
    workload = classify(run)
    if workload.cls is WorkloadClass.CONVERSATIONAL:
        names = list(config.get("evaluators")
                     or (run.scenario.evaluators if run.scenario else None)
                     or DEFAULT_EVALUATORS)
        gating = tuple(config.get("gating_evaluators") or DEFAULT_GATING)
    else:
        names = list(evaluators_for(workload))
        gating = gating_for(workload)
    return names, gating, workload


def evaluate(run: Run, config: Optional[dict] = None,
             judge: Optional[GoalSuccessJudge] = None) -> Verdict:
    config = config or {}
    names, gating_tuple, workload = resolve_plan(run, config)
    gating = set(gating_tuple)
    judge = judge or GoalSuccessJudge(JudgeConfig(
        goal_source=config.get("goal_source", DEFAULT_GOAL_SOURCE),
        input_set=tuple(config.get("judge_input_set", DEFAULT_INPUT_SET)),
        pass_threshold=config.get("goal_pass_threshold", DEFAULT_PASS_THRESHOLD),
    ))

    results: list[EvaluatorResult] = []
    for name in names:
        if name == "goal_judge":
            two_tier = judge.judge(run)   # the judge itself scores NO_DATA on absent evidence
            results.append(two_tier.dispatch_correct)
            results.append(two_tier.results_delivered)
        else:
            results.append(_code_evaluator(name, run))   # evidence-intrinsic RED (REQ-002)

    overall, reason = gate(results, gating)
    return Verdict(run.run_id, results, _utility(run, results, workload), overall, reason,
                   workload_class=workload.cls.value, workload_basis=workload.basis,
                   workload_evidence=workload.evidence, gating=tuple(gating_tuple),
                   workload_parent=workload.parent_hint or "")


def gate(results: list[EvaluatorResult], gating) -> tuple:
    """Fold evaluator results into ``(Overall, reason)``.

    Extracted so there is exactly ONE implementation. ``mine_local``'s recorded-label path
    had a hand copy, and it had already drifted: it omitted the ``(informational: …)``
    suffix, so the artifact's human-readable reason concealed that both of the passing runs
    the ordering claim rests on carry a RED ``DagCompletionValidator``.

    Three outcomes, and the three input states that produce them:

    ``FAIL``      a gating evaluator MEASURED a failure. Any unmeasured sibling is named in
                  the reason, because a failure established on partial evidence is weaker
                  than one established on all of it, and the reader has to be able to see
                  that.
    ``UNSCORED``  no gating evaluator measured a failure, and at least one could not measure
                  at all (NO_DATA). Could-not-measure is not could-not-pass. This is also
                  where an empty gating set and an all-NOT_APPLICABLE set land.
    ``PASS``      every gating evaluator that has an applicable dimension measured a pass.

    NO_DATA and NOT_APPLICABLE are kept apart deliberately, per ``contract.Outcome``: the
    first means the dimension applies but its evidence is missing, which blocks a pass; the
    second means the dimension does not exist for this run, which cannot block anything. A
    run whose only gating evaluator is NOT_APPLICABLE has nothing to stand on either way, so
    it is UNSCORED rather than a free pass.
    """
    gating = set(gating)
    gating_results = [r for r in results if r.name in gating]
    failed = [r for r in gating_results if r.outcome is Outcome.FAIL]
    no_data = [r for r in gating_results if r.outcome is Outcome.NO_DATA]
    measured_pass = [r for r in gating_results if r.outcome is Outcome.PASS]

    if failed:
        reason = "gating: " + "; ".join(f"{r.name}:FAIL" for r in failed)
        if no_data:
            reason += (" (on partial evidence: "
                       + ", ".join(f"{r.name}:NO_DATA" for r in no_data) + ")")
        return Overall.FAIL, reason

    if no_data:
        return (Overall.UNSCORED,
                "could not measure: " + "; ".join(f"{r.name}:NO_DATA" for r in no_data)
                + ". Absent evidence is not a failure; this run is neither a defect nor a "
                  "negative control")

    if not measured_pass:
        detail = ("no gating evaluator was configured" if not gating_results
                  else "every gating evaluator reported NOT_APPLICABLE: "
                       + ", ".join(r.name for r in gating_results))
        return Overall.UNSCORED, f"nothing scorable ({detail})"

    info_reds = [r for r in results
                 if r.name not in gating and r.outcome in (Outcome.FAIL, Outcome.NO_DATA)]
    reason = "gating passed"
    if info_reds:
        reason += (" (informational: "
                   + ", ".join(f"{r.name}:{r.outcome.value}" for r in info_reds) + ")")
    return Overall.PASS, reason
