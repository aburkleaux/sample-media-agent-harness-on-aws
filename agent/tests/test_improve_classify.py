"""Workload classification and the per-class objectives, on real harvested sessions.

The defect these tests exist to prevent is measured, not hypothetical: with one objective
applied to every session, 30 of the 35 FAILs in the 50-session harvest were the goal judge
reporting that it had no conversation to read, and those 30 then entered mining as evidence
of failure. So the claims here are about ROUTING and about the absence of a fabricated
verdict, in both directions:

  * a session that had no user request must not be scored as though it failed one
  * a session that had one must still be scored exactly as before
  * a shape we do not recognise must produce UNSCORED, never a verdict assembled from
    whichever checks happened to apply

Every session used is real (REQ-017). The four non-conversational exemplars are committed by
`scripts/refresh_improve_fixtures.py`, chosen by span shape rather than by id.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve_fixtures import (  # noqa: E402
    A3A8FAC0, E2CB300B, ONEB42D744, exemplar, fanout_harvest, real_plan, real_trajectory,
)

from improve.classify import (  # noqa: E402
    EVALUATORS_BY_CLASS, GATING_BY_CLASS, WorkloadClass, classify, evaluators_for, gating_for,
    has_model_span,
)
from improve.contract import NO_DATA, Outcome, Overall, Run, Scenario, Step  # noqa: E402
from improve.evaluate import evaluate, resolve_plan  # noqa: E402
from improve.evaluators_improve import (  # noqa: E402
    control_plane_op, discovery_elapsed_ms, tool_discovery,
)
from improve.observe.span_trajectory import trajectory_for_session  # noqa: E402


class _RefusingJudge:
    """Fails loudly if the goal judge is reached. The whole point of the routing is that a
    class with no user request never gets here, and a judge double that returned a score
    would let a routing regression pass silently."""

    def judge(self, run):  # noqa: D102
        raise AssertionError(
            f"goal_judge was invoked for {classify(run).cls.value}, which has no user request")


def _conversation(session: str) -> Run:
    return Run(run_id=session, trajectory=real_trajectory(session), plan=real_plan(session),
               turns=[{"role": "USER", "text": "do the thing"},
                      {"role": "ASSISTANT", "text": "done"}],
               response="done")


def _exemplar_run(kind: str) -> Run:
    session, steps = exemplar(kind)
    return Run(run_id=session, trajectory=steps)


def _sub_session_run(run_id: str, span_source) -> Run:
    """A real sub-session's Run, with both evidence fields read through the real accessors.

    Trajectory and parent come from the SAME source, because they come from the same spans — the
    parent link is a top-level span field, so a source that could not read the trajectory could
    not have read the parent either. `build_run` wires exactly this pair; the seam test covers
    that wiring, and this keeps the classification tests clear of plan-state and Memory sources
    they do not need.
    """
    return Run(run_id=run_id, trajectory=trajectory_for_session(run_id, source=span_source),
               parent_run_id=span_source.parent_for(run_id))


# --- classification ---------------------------------------------------------
@pytest.mark.parametrize("session", [E2CB300B, ONEB42D744, A3A8FAC0])
def test_a_real_conversation_classifies_conversational(session):
    workload = classify(_conversation(session))
    assert workload.cls is WorkloadClass.CONVERSATIONAL
    assert has_model_span(_conversation(session))


def test_a_real_warm_up_session_is_tool_discovery_not_a_failed_conversation():
    workload = classify(_exemplar_run("tool_discovery"))
    assert workload.cls is WorkloadClass.TOOL_DISCOVERY
    assert "tool-catalog handshake" in workload.evidence


def test_a_real_lab_session_is_control_plane():
    assert classify(_exemplar_run("control_plane")).cls is WorkloadClass.CONTROL_PLANE


def test_a_real_plan_sub_session_is_plan_node():
    workload = classify(_exemplar_run("plan_node"))
    assert workload.cls is WorkloadClass.PLAN_NODE


def test_a_sub_session_with_no_trace_evidence_reports_no_parent_rather_than_its_own_suffix():
    """The defect this pair of tests replaces, and why it survived a passing suite for months.

    ``parent_hint`` used to be ``run_id[len("plan-session-"):]`` — the sub-session's OWN suffix.
    It is unique per row, so it named no parent and no two siblings ever shared one: measured over
    141 real sub-session ids, 141 distinct hints and 0 siblings agreeing. Every check a test would
    naturally make still passed (the field is a non-empty string that varies per run), which is
    exactly why the assertion here is the NEGATIVE one — that a run whose evidence names no parent
    reports none, and in particular does not report a value derived from its own id.

    The exemplar carries spans but no trace, which is what every harvest taken before the
    harvester projected ``traceId`` looks like. ``test_improve_harvest_live_seam.py`` covers the
    positive case on a real request that fanned out.
    """
    session, _ = exemplar("plan_node")
    workload = classify(_exemplar_run("plan_node"))
    assert workload.parent_hint is None
    assert session[len("plan-session-"):] not in (workload.parent_hint or "")
    # Evidence tracks value: the prose must not claim a link the field does not carry.
    assert "does not identify" in workload.evidence


def test_no_trajectory_is_unclassified_rather_than_guessed():
    workload = classify(Run(run_id="r", trajectory=NO_DATA))
    assert workload.cls is WorkloadClass.UNCLASSIFIED
    assert evaluators_for(workload) == ()


def test_a_model_bearing_session_with_no_transcript_is_still_conversational():
    """The distinction that was wrong in the first version of the classifier.

    "This was not a conversation" and "this was a conversation whose transcript we failed to
    read" are different facts with different owners. Classifying the second as
    `unclassified` would file a Memory-read failure as an unknown shape; keeping it
    conversational makes the goal judge report NO_DATA, which surfaces it as an evidence gap.
    """
    run = Run(run_id="r", trajectory=[Step(name="chat"), Step(name="execute_tool skills")])
    workload = classify(run)
    assert workload.cls is WorkloadClass.CONVERSATIONAL
    assert "no transcript found" in workload.evidence


def test_recorded_goal_labels_outrank_span_shape():
    """The golden benchmark must not be reclassified by a heuristic a harvest change moves."""
    run = Run(run_id="r", trajectory=NO_DATA)
    run.scenario = Scenario(scenario_id="r", expected={"dispatch_correct": 0.9})
    workload = classify(run)
    assert workload.cls is WorkloadClass.CONVERSATIONAL
    assert workload.basis == "recorded_label"


def test_inferred_classes_say_they_were_inferred():
    """Nothing in the telemetry records the invocation kind, so every span-derived class has
    to be visibly weaker than a recorded one (run-record field 3 is the durable fix)."""
    for kind in ("tool_discovery", "control_plane", "plan_node"):
        assert classify(_exemplar_run(kind)).basis == "inferred_from_spans"


# --- the routing tables -----------------------------------------------------
def test_every_class_appears_in_both_tables():
    """A class missing from `GATING_BY_CLASS` would run evaluators and then gate on nothing,
    which reads as UNSCORED for every member and looks like a data problem."""
    assert set(EVALUATORS_BY_CLASS) == set(WorkloadClass)
    assert set(GATING_BY_CLASS) == set(WorkloadClass)


def test_no_class_gates_on_an_evaluator_it_does_not_run():
    """The failure mode is silent: gate on `dispatch_correct` without running `goal_judge`
    and every verdict in that class is UNSCORED forever."""
    produced = {
        "goal_judge": {"dispatch_correct", "results_delivered"},
    }
    for cls, evaluators in EVALUATORS_BY_CLASS.items():
        available = set()
        for name in evaluators:
            available |= produced.get(name, {name})
        missing = set(GATING_BY_CLASS[cls]) - available
        assert not missing, f"{cls.value} gates on {missing} but never runs it"


def test_only_the_conversational_class_uses_the_goal_judge():
    for cls, evaluators in EVALUATORS_BY_CLASS.items():
        if cls is WorkloadClass.CONVERSATIONAL:
            assert "goal_judge" in evaluators
        else:
            assert "goal_judge" not in evaluators


# --- routing through evaluate() ---------------------------------------------
def test_config_cannot_force_the_goal_judge_onto_a_warm_up_session():
    """`loop.config.yaml` names `goal_judge` for the whole population, and applying that
    standing list to a discovered harvest is exactly how the 30 fabricated FAILs happened.
    The class wins for every class except conversational."""
    run = _exemplar_run("tool_discovery")
    names, gating, workload = resolve_plan(
        run, {"evaluators": ["goal_judge"],
              "gating_evaluators": ["dispatch_correct", "results_delivered"]})
    assert "goal_judge" not in names
    assert gating == ("ToolDiscoveryValidator",)
    # And it really does not get called.
    evaluate(run, config={"evaluators": ["goal_judge"]}, judge=_RefusingJudge())


def test_a_conversational_run_still_honours_the_config_list():
    """The knob keeps working for the class its keys describe, so calibration is unaffected."""
    names, gating, workload = resolve_plan(
        _conversation(E2CB300B), {"evaluators": ["goal_judge"],
                                  "gating_evaluators": ["dispatch_correct"]})
    assert workload.cls is WorkloadClass.CONVERSATIONAL
    assert names == ["goal_judge"] and gating == ("dispatch_correct",)


def test_an_unclassified_run_is_unscored_and_never_fail():
    verdict = evaluate(Run(run_id="r", trajectory=NO_DATA), judge=_RefusingJudge())
    assert verdict.overall is Overall.UNSCORED
    assert verdict.overall is not Overall.FAIL
    assert verdict.evaluator_results == []
    assert verdict.workload_class == "unclassified"


def test_the_verdict_records_the_class_and_what_gated_it():
    """Without these a reader cannot tell why `dispatch_correct` is absent from a run that
    never had a request to dispatch, and mining cannot refuse self-satisfying facets."""
    verdict = evaluate(_exemplar_run("tool_discovery"), judge=_RefusingJudge())
    assert verdict.workload_class == "tool_discovery"
    assert verdict.workload_basis == "inferred_from_spans"
    assert verdict.workload_evidence
    assert verdict.gating == ("ToolDiscoveryValidator",)


# --- the new objectives -----------------------------------------------------
def test_a_successful_warm_up_passes_its_own_objective():
    """The correction that matters most. These 17 sessions were FAILs; they are ordinary
    successful harness work, and the loop has to be able to say so."""
    verdict = evaluate(_exemplar_run("tool_discovery"), judge=_RefusingJudge())
    assert verdict.overall is Overall.PASS
    result = next(r for r in verdict.evaluator_results
                  if r.name == "ToolDiscoveryValidator")
    assert result.outcome is Outcome.PASS
    assert "handshake" in result.explanation


def test_warm_up_latency_is_reported_as_a_utility_term_not_a_gate():
    """Warm-up cost is real (1.8 to 6.6 s across the harvest) but no threshold has been
    measured, so it is a trend term. A gate here would be a number nobody established."""
    run = _exemplar_run("tool_discovery")
    verdict = evaluate(run, judge=_RefusingJudge())
    assert verdict.utility.terms["discovery_ms"] == discovery_elapsed_ms(run)
    assert isinstance(verdict.utility.terms["discovery_ms"], int)
    assert verdict.overall is Overall.PASS


def test_the_discovery_objective_does_not_judge_the_handshake_COUNT():
    """From span names, "redundant re-resolution" and "one handshake per federated gateway"
    are indistinguishable, and the harness does federate to several. Calling the count a
    defect would be inventing a finding, so the count is reported and never gated."""
    run = _exemplar_run("tool_discovery")
    handshakes = sum(1 for s in run.trajectory if s.name == "mcp tools/list")
    assert handshakes > 1
    assert tool_discovery(run).outcome is Outcome.PASS


def test_error_spans_do_not_make_a_control_plane_session_fail():
    """The correction that cost the most to get right.

    An earlier version of `control_plane_op` FAILed on any ERROR span, and on this exemplar
    that produced a confident failure from four `S3.HeadObject` 404s that are a deliberate
    existence probe (`s3_user_files_tool` heads the canonical key, then falls back on
    `ClientError`). It also contradicted a measurement this repo already made: `had_error` is
    ANTI-correlated with run failure (precision 0.276 against a 0.333 base rate, DF-8/DF-10),
    which is why `no_retry_after_failed_node` was deleted rather than fixed. Net effect on the
    harvest was 30 fabricated failures removed and 1 introduced.
    """
    run = _exemplar_run("control_plane_error")
    errors = [s for s in run.trajectory if s.status == "ERROR"]
    assert errors, "this exemplar is chosen for its ERROR spans; without them it proves nothing"
    result = control_plane_op(run)
    assert result.outcome is not Outcome.FAIL
    # The evidence is still reported, because a human has to be able to look at it.
    assert "not treated as failure" in result.explanation.lower()
    assert "ClientError" in result.explanation or "404" in result.explanation


def test_a_control_plane_session_is_unscorable_because_no_outcome_is_recorded():
    """NO_DATA is the finding, not a placeholder: `POST /invocations` is UNSET on all 268
    occurrences in the harvest, so nothing records whether the invocation succeeded. Saying so
    routes it to the defect register as an instrumentation gap. Claiming PASS would assert a
    success the evidence does not support."""
    for kind in ("control_plane", "control_plane_error"):
        result = control_plane_op(_exemplar_run(kind))
        assert result.outcome is Outcome.NO_DATA
        assert result.score is None
        assert "no operation outcome is recorded" in result.explanation.lower()
    verdict = evaluate(_exemplar_run("control_plane"), judge=_RefusingJudge())
    assert verdict.overall is Overall.UNSCORED
    assert verdict.overall is not Overall.PASS


def test_the_discovery_objective_reads_handshake_status_not_any_error_span():
    """Same rule for the other new objective: only the operation the class exists to perform
    can decide its outcome."""
    from improve.contract import Step
    run = Run(run_id="r", trajectory=[
        Step(name="S3.GetObject", status="ERROR", error_message="transient, retried"),
        Step(name="mcp tools/list", status="OK"),
        Step(name="POST /invocations", status="UNSET", duration_ms=2000)])
    assert tool_discovery(run).outcome is Outcome.PASS
    broken = Run(run_id="r", trajectory=[Step(name="mcp tools/list", status="ERROR")])
    assert tool_discovery(broken).outcome is Outcome.FAIL


def test_both_new_objectives_are_no_data_without_a_trajectory():
    """can-show-RED: absent evidence is never a pass, in the new objectives too."""
    blind = Run(run_id="r", trajectory=NO_DATA)
    assert tool_discovery(blind).outcome is Outcome.NO_DATA
    assert control_plane_op(blind).outcome is Outcome.NO_DATA
    assert discovery_elapsed_ms(blind) is None


# --- what mining does with a non-conversational failure ---------------------
def test_a_control_plane_run_gets_a_symptom_facet_and_is_not_a_plan_gap():
    """Both halves of the hole this change opened, pinned together.

    A `control_plane` run has no DAG by design, so `plan_absent` is unconditionally true for
    it, and the symptom facets used to be a hardcoded two-tier-plus-two-check list that could
    not see `ControlPlaneOpValidator`. Together those routed the run into `evidence_gaps` as
    "plan_absent (no task nodes captured)", which names the wrong defect and hands it to the
    wrong owner.
    """
    from improve.mine import mine
    from improve.signature import facets_for

    run = _exemplar_run("control_plane_error")
    verdict = evaluate(run, judge=_RefusingJudge())

    facets = facets_for(run, verdict)
    symptom = facets["check_failed:ControlPlaneOpValidator"]
    assert symptom.value is True and symptom.observed is True

    # The class is UNSCORED today (no recorded outcome), so it is not a failure and not
    # mineable. What must hold is that the symptom facet EXISTS, so that the moment the
    # harness records an operation outcome, a real failure has something to be described by.
    assert verdict.overall is Overall.UNSCORED
    result = mine([run], [verdict])
    assert result.signatures == [], "an unscorable run is never evidence of a pattern"
    assert run.run_id not in [entry.run_id for entry in result.residue]


def test_a_wholly_blind_run_is_still_an_evidence_gap():
    """The conditional must not swallow the unconditional case. No spans at all means the run
    could not be observed by anyone, whatever its class would have been."""
    from improve.contract import EvaluatorResult, UtilityVector, Verdict
    from improve.mine import mine

    blind = Run(run_id="blind")
    verdict = Verdict("blind",
                      [EvaluatorResult("results_delivered", "llm", Outcome.FAIL, 0.1)],
                      UtilityVector(), Overall.FAIL, "")
    result = mine([blind], [verdict])
    assert [e.run_id for e in result.evidence_gaps] == ["blind"]
    assert "trajectory_absent" in result.evidence_gaps[0].best_shape


def test_every_code_evaluator_has_a_symptom_facet():
    """The drift that caused this: a new evaluator without a `check_failed:` facet means a
    failure only it detects has NO symptom, and mining cannot say what went wrong."""
    from improve.contract import UtilityVector, Verdict
    from improve.evaluators_improve import CODE_EVALUATORS
    from improve.signature import symptom_facets

    names = {f.name for f in symptom_facets(Verdict("r", [], UtilityVector(), Overall.UNSCORED))}
    for evaluator in CODE_EVALUATORS:
        assert f"check_failed:{evaluator}" in names


def test_only_same_class_passing_runs_are_negative_controls():
    """The REQ-018 gate is only as strong as its control pool is honest.

    A passing `tool_discovery` run was gated on whether gateway handshakes completed. It was
    never asked whether the agent achieved a goal, so it cannot vouch for a shape that predicts
    goal failure. Admitting the whole harvest's non-conversational passes raised
    `controls_excluded` for the `e2cb300b` signature from 4 to 12, which is the number a reader
    weighs the claim by.
    """
    from improve.contract import EvaluatorResult, UtilityVector, Verdict
    from improve.mine import mine
    from improve_fixtures import SIX309F154

    def conversational_pass(session):
        run = Run(run_id=session, trajectory=real_trajectory(session), plan=real_plan(session))
        return run, Verdict(
            session, [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, 0.9),
                      EvaluatorResult("results_delivered", "llm", Outcome.PASS, 0.9)],
            UtilityVector(), Overall.PASS, "", workload_class="conversational",
            gating=("dispatch_correct", "results_delivered"))

    fail_run = Run(run_id=E2CB300B, trajectory=real_trajectory(E2CB300B),
                   plan=real_plan(E2CB300B))
    fail_verdict = Verdict(
        E2CB300B, [EvaluatorResult("results_delivered", "llm", Outcome.FAIL, 0.4)],
        UtilityVector(), Overall.FAIL, "", workload_class="conversational",
        gating=("dispatch_correct", "results_delivered"))

    control_run, control_verdict = conversational_pass(SIX309F154)
    discovery_run = _exemplar_run("tool_discovery")
    discovery_verdict = evaluate(discovery_run, judge=_RefusingJudge())
    assert discovery_verdict.overall is Overall.PASS, "the contaminating run must be a PASS"

    without = mine([fail_run, control_run], [fail_verdict, control_verdict])
    with_other_class = mine([fail_run, control_run, discovery_run],
                            [fail_verdict, control_verdict, discovery_verdict])

    def excluded(result):
        entries = result.signatures + result.residue
        return getattr(entries[0], "controls_excluded", None) if entries else None

    assert excluded(without) == excluded(with_other_class), (
        "a passing run from another class changed the gate's control count")
    log = " ".join((with_other_class.signatures + with_other_class.residue)[0].refinement_log)
    assert "conversational" in log and "of 2 passing run(s)" in log


def test_sibling_plan_sub_sessions_count_as_one_control():
    """Sub-sessions of one plan share a user request, so they are one piece of evidence.
    `population.py` records this hazard; `classify.parent_hint` is what lets mining honour it.

    The KEY is real (REQ-017): the sub-session ids and the parent they resolve to come from a
    harvested request that genuinely fanned out, classified by the production `classify`. It was
    hand-written before, and that is what hid the defect — a fabricated shared key proved `mine`
    collapses siblings *given* one, while nothing proved `classify` ever produced one. It never
    did.

    The verdict OUTCOMES stay hand-built, and that part is not a fixture gap: whether these
    particular runs passed is irrelevant to the arithmetic under test, and forcing them through the
    scorer would make the test depend on their DAG evidence instead of on the de-duplication.
    """
    from improve.contract import EvaluatorResult, UtilityVector, Verdict
    from improve.mine import mine

    parent, kids, span_source = fanout_harvest()
    hints = {run_id: classify(_sub_session_run(run_id, span_source)).parent_hint
             for run_id in kids}
    # The premise, asserted rather than assumed: every sibling resolved to the ONE parent.
    assert set(hints.values()) == {parent}, hints

    def sibling(run_id):
        run = Run(run_id=run_id,
                  trajectory=[Step(name="chat"), Step(name="execute_tool skills")])
        return run, Verdict(
            run.run_id, [EvaluatorResult("DagCompletionValidator", "code", Outcome.PASS, 1.0)],
            UtilityVector(), Overall.PASS, "", workload_class="plan_node",
            gating=("DagCompletionValidator",), workload_parent=hints[run_id])

    controls = kids[1:]
    fail_run = Run(run_id=kids[0], trajectory=[Step(name="chat")])
    fail_verdict = Verdict(
        fail_run.run_id,
        [EvaluatorResult("DagCompletionValidator", "code", Outcome.FAIL, 0.0)],
        UtilityVector(), Overall.FAIL, "", workload_class="plan_node",
        gating=("DagCompletionValidator",), workload_parent=hints[fail_run.run_id])

    runs, verdicts = [fail_run], [fail_verdict]
    for run_id in controls:
        run, verdict = sibling(run_id)
        runs.append(run)
        verdicts.append(verdict)

    result = mine(runs, verdicts)
    entry = (result.signatures + result.residue + result.evidence_gaps)[0]
    log = " ".join(getattr(entry, "refinement_log", ()) or ())
    # Siblings of ONE parent, so one control however many of them there are. Written from
    # `len(controls)` rather than a literal because the fixture is shape-selected: the number of
    # sub-sessions the busiest request fanned out into moves with the harvest.
    assert f"1 of {len(controls)} passing run(s)" in log, log


def test_two_different_requests_do_not_collapse_into_one_control():
    """The other way a de-duplication is wrong. Merging siblings is the point; merging two
    unrelated requests would silently discard a real, independent control.

    Both committed fan-outs are used, so this reads across two parents and two id FORMATS —
    `fanout_1`'s sub-sessions are the old `plan-session-<epoch>` shape and `fanout_2`'s the current
    `plan-session-<epoch>-<32 hex>`. The lengthened suffix is what removed the last chance the old
    id-derived hint had of grouping anything, so a rule that works on only one format is not fixed.
    """
    keys = {}
    for group in ("fanout_1", "fanout_2"):
        parent, kids, span_source = fanout_harvest(group)
        resolved = {classify(_sub_session_run(run_id, span_source)).parent_hint for run_id in kids}
        assert resolved == {parent}, (group, resolved)
        keys[group] = parent
    assert keys["fanout_1"] != keys["fanout_2"], keys


# --- goal_source and the gradability guard ----------------------------------
def test_the_gradability_guard_honours_the_configured_goal_source():
    """`plan_state` and `plan_or_first_user` exist for transcript-less runs, so a guard that
    reads only `run.turns` refuses exactly the runs those sources were added to grade.
    `judge_sweep.py` sweeps all five, so a disagreement empties an arm of the calibration."""
    from improve.contract import Plan, PlanNode
    from improve.judge import GOAL_SOURCES, _no_gradable_evidence

    plan_only = Run(run_id="r", plan=Plan(goal="translate the audio to Spanish",
                                          nodes=[PlanNode(id="t1", status="completed")]))
    assert _no_gradable_evidence(plan_only, "plan_state") is None
    assert _no_gradable_evidence(plan_only, "plan_or_first_user") is None
    # No transcript, so the conversation-derived sources genuinely have nothing.
    for source in ("full_conversation", "first_user", "all_user"):
        refusal = _no_gradable_evidence(plan_only, source)
        assert refusal and source in refusal

    # A transcript makes every source gradable.
    talking = Run(run_id="r", turns=[{"role": "USER", "text": "translate this"}])
    assert _no_gradable_evidence(talking, "full_conversation") is None
    # And the guard has an answer for every supported source, rather than a default that
    # silently means "gradable".
    for source in GOAL_SOURCES:
        _no_gradable_evidence(talking, source)


def test_a_response_alone_is_gradable_because_the_delivery_tier_can_read_it():
    """The two tiers are independent; `results_delivered` needs the response, not the request."""
    from improve.judge import _no_gradable_evidence
    assert _no_gradable_evidence(
        Run(run_id="r", response="here are your four translations"), "plan_state") is None


# --- a control-plane session that also warmed up a gateway ------------------
def test_a_lab_session_still_gets_its_handshakes_checked():
    """Measured: 2 of the 3 lab sessions carry `mcp tools/list`, one of them 8 times. The `lab-`
    prefix is checked before the discovery test, so without running BOTH objectives a gateway
    refusing the handshake inside a lab session would be invisible."""
    run = _exemplar_run("control_plane")
    handshakes = sum(1 for s in run.trajectory if s.name == "mcp tools/list")
    assert handshakes, "this exemplar is chosen for its handshakes"
    names, gating, workload = resolve_plan(run)
    assert workload.cls is WorkloadClass.CONTROL_PLANE
    assert "ToolDiscoveryValidator" in names and "ToolDiscoveryValidator" in gating
    assert f"{handshakes} gateway handshake(s)" in workload.evidence


def test_a_failed_handshake_in_a_lab_session_is_the_one_thing_that_can_fail_it():
    """`ControlPlaneOpValidator` is always NO_DATA, so a lab session is UNSCORED by default.
    A measured handshake failure must still win, because that IS establishable."""
    from improve.contract import Step
    run = Run(run_id="lab-abc", trajectory=[
        Step(name="DynamoDB.Query", status="OK"),
        Step(name="mcp tools/list", status="ERROR", error_message="gateway refused"),
        Step(name="POST /invocations", status="UNSET", duration_ms=300)])
    verdict = evaluate(run, judge=_RefusingJudge())
    assert verdict.workload_class == "control_plane"
    assert verdict.overall is Overall.FAIL
    assert "ToolDiscoveryValidator:FAIL" in verdict.reason
    # And the unmeasurable sibling is named, so the failure is not read as fully evidenced.
    assert "on partial evidence" in verdict.reason
    assert "ControlPlaneOpValidator:NO_DATA" in verdict.reason


def test_a_lab_session_without_handshakes_gets_not_applicable_not_a_failure():
    """The NOT_APPLICABLE branch, which the `lab-` fix made reachable: 1 of the 3 lab sessions
    performs no discovery at all."""
    from improve.contract import Step
    run = Run(run_id="lab-abc", trajectory=[Step(name="DynamoDB.Query", status="OK")])
    result = tool_discovery(run)
    assert result.outcome is Outcome.NOT_APPLICABLE
    assert evaluate(run, judge=_RefusingJudge()).overall is Overall.UNSCORED


def test_a_control_plane_session_without_a_lab_id_is_still_recognised():
    """The `CONTROL_PLANE_SPAN_PREFIXES` fallback. No whole session in the current harvest takes
    it, so it is exercised here on real span NAMES from the harvest rather than left untested:
    a data-plane-only invocation under an ordinary uuid must not read as `unclassified`."""
    from improve.contract import Step
    # Deliberately NOT uuid-shaped. A fabricated uuid with a run of zeros both trips the
    # 12-digit account-id guard and can be mistaken for a real harvested session; that has
    # happened here before. The test only needs an id that is neither `lab-` nor
    # `plan-session-` prefixed.
    run = Run(run_id="synthetic-data-plane-only", trajectory=[
        Step(name="S3.ListObjectsV2", status="OK"),
        Step(name="DynamoDB.Query", status="OK"),
        Step(name="DynamoDB.PutItem", status="OK")])
    workload = classify(run)
    assert workload.cls is WorkloadClass.CONTROL_PLANE
    assert "only data-plane operations" in workload.evidence
