"""Offline tests for the trajectory-shape-aware evaluators — no AWS, no LLM.

REQ-017: every case below is driven by a REAL harvested fixture — the six golden
sessions (`fixtures/plan_state_golden.json`) and the three async-lifecycle ground-truth
sessions (`fixtures/async_lifecycle_sessions.json`) — not hand-invented shapes. Each test
names the session it pins and what that session actually did, so a failure points at
a concrete regression rather than an abstract one.

TWO cases are hand-built, and each says why in its own docstring: the retrieve-without-dispatch
branch fires on zero of 541 real sessions, so no fixture can drive it, and no-async-work needs no
fixture because every golden session dispatches.

These encode the measured terminality findings: recovered errors are not failures, a
failed node is a real DAG failure, PLAN-row status is unreliable and ignored, and
terminality alone cannot see dependency ORDER (DF-11).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve_fixtures import (  # noqa: E402
    A3A8FAC0, ABC839CA, E2CB300B, FFAB73F8, ONEB42D744, SIX309F154,
    async_lifecycle_trajectory, real_plan, real_tool_names, real_trajectory,
)
from improve.contract import NO_DATA, Outcome, Plan, PlanNode, tool_name_sequence  # noqa: E402
from improve.evaluators_improve import (  # noqa: E402
    _edge_order_violations, async_job_flow, dag_completion, pair_dispatches,
)


# --------------------------------------------------------------------------
# async_job_flow — driven by real ordered span names
#
# EVERY span, via `tool_name_sequence(real_trajectory(...))`, because that is exactly what
# `evaluate._code_evaluator` passes. The TOOL-ONLY slice (`real_tool_names`) is not a smaller
# version of that input, it is a different one: the rule reads the span COUNT to decide whether
# the session ended on a dispatch, and for ffab73f8 the slice's last unpaired dispatch is its
# final span (claim TRUE) while on the real 541-span trajectory 17 spans follow it (claim FALSE).
# Use `real_tool_names` only for predicates that genuinely read the slice.
# --------------------------------------------------------------------------

def _names(session: str) -> list:
    """The production input: every span, prefixes stripped, exactly as `_code_evaluator` does."""
    return tool_name_sequence(real_trajectory(session))


def _async_names(prefix: str) -> list:
    """Same, for the three async-lifecycle ground-truth sessions."""
    return tool_name_sequence(async_lifecycle_trajectory(prefix))


def test_async_pass_when_every_dispatch_on_a_real_trajectory_was_collected():
    """abc839ca: 5 dispatches, 28 retrieves, none left unpaired — the healthy shape."""
    r = async_job_flow(_names(ABC839CA))
    assert r.outcome is Outcome.PASS
    assert "All 5 dispatch(es)" in r.explanation


def test_async_no_verdict_when_trajectory_evidence_is_absent():
    """Absent evidence must be NO_DATA (RED), never a silent pass — the N/A-trap."""
    r = async_job_flow([], trajectory_present=False)
    assert r.outcome is Outcome.NO_DATA


def test_async_fails_the_session_that_really_did_lose_a_result_74b0a063():
    """A session that collected earlier results and then abandoned its last dispatch must FAIL.

    `74b0a063` retrieved five results through its 179-233 region, then dispatched again near the
    end and nothing ever fetched it — real lost work, in a run the judge PASSED. Testing only the
    first dispatch lets the early retrieves make the whole session read as healthy, which passed
    all 23 sessions in this state across a 336 h harvest.
    """
    r = async_job_flow(_async_names("74b0a063"))
    assert r.outcome is Outcome.FAIL
    assert "did retrieve 5 result(s)" in r.explanation


def test_async_does_not_fail_the_two_sessions_that_returned_their_results_inline():
    """A session that never retrieved anything must not be accused of losing a result.

    `36fafeb7` and `23a1fa24` both dispatched with `sync_mode: true` and got their payloads back
    INLINE, so neither emitted a retrieve span — `_is_retrieve` is a lexical `"result" in name`
    test and there is nothing to match. Reading that as "no results retrieved after the dispatch"
    FAILs 31 sessions across a 336 h harvest, none of which lost work.

    Asserted as "not FAIL" rather than "is PASS" on purpose: which non-FAIL outcome the ambiguous
    case gets is an open decision (see `async_job_flow`), and this test pins only the half the
    evidence settles.
    """
    for prefix in ("36fafeb7", "23a1fa24"):
        assert async_job_flow(_async_names(prefix)).outcome is not Outcome.FAIL, prefix


def test_async_does_not_accuse_a_dispatch_the_session_kept_working_past():
    """ffab73f8 (golden, label PASS) leaves 2 of 7 dispatches unpaired, the last at span 523
    of 541 — but 17 spans follow it, including six S3.GetObject reads after the closing model
    turn, so the session did not end there. Ambiguous, never accused.

    This is also the session that pins the UPPER bound of `_TAIL_TOLERANCE`'s honest range;
    see `test_improve_mine`'s
    `test_a_dispatch_the_session_kept_working_past_is_not_called_the_session_end`.
    """
    assert async_job_flow(_names(FFAB73F8)).outcome is not Outcome.FAIL


def test_the_ambiguous_case_is_a_PLACEHOLDER_and_this_test_exists_to_make_moving_it_loud():
    """The outcome for an unpaired dispatch the session did not end on is UNDECIDED. Rewrite this
    test when it is decided; do not "fix" it.

    That state cannot be told apart from an inline return (D-02 / NS-5: tool arguments and results
    reach `aws/spans` only on `mcp tools/call` spans, and `dispatch_tool` is a harness builtin
    emitting `execute_tool`). Each available outcome is wrong in a different direction — FAIL
    over-claims, PASS lets absence read as a pass, and NO_DATA is folded together with FAIL by
    `signature.symptom_facets`' `bad = outcome in (FAIL, NO_DATA)` — so the choice is policy, not
    measurement, and it covers 47 of the 70 sessions carrying an unpaired dispatch over a 336 h
    harvest. PASS is the placeholder, pinned here so changing it is a visible decision rather than
    a one-token edit.
    """
    r = async_job_flow(_names(FFAB73F8))
    assert r.outcome is Outcome.PASS
    assert "spans cannot distinguish from a lost result" in r.explanation
    assert "no failure is claimed" in r.explanation


def test_async_fail_when_retrieve_without_dispatch():
    """Hand-built, and it has to be: this branch fires ZERO times across 541 real sessions, so no
    fixture can drive it (REQ-017 asks for real data where real data exists).

    It assumes one session is the whole window, and a retrieve whose dispatch happened in an
    EARLIER session is plausible here since plan sub-sessions and polling both do it. Nothing has
    measured that, so the branch stands.
    """
    assert async_job_flow(["get_plan_results"]).outcome is Outcome.FAIL


def test_async_not_applicable_when_run_did_no_async_work():
    """1b42d744 dispatches, so a real no-dispatch conversational session is not in the golden
    six; this shape is the trivial one and needs no fixture. NOT_APPLICABLE, never PASS: the
    dimension is absent, so it must not vouch for the run (contract.Outcome)."""
    assert async_job_flow(["chat", "file_read"]).outcome is Outcome.NOT_APPLICABLE


def test_the_evaluator_and_the_facet_share_ONE_pairing_rule():
    """`async_job_flow` and `session_ended_on_unretrieved_dispatch` must read ONE pairing rule.

    Asserted as a MECHANISM, not as agreeing outputs: moving the tail threshold in its single home
    must move BOTH readers, which is only true if neither re-derives it. A hand copy would keep
    answering at 12 while this one answers at 100. When each did own a copy they contradicted each
    other — one reported `74b0a063` as ending on an uncollected dispatch and the other passed it.
    """
    from improve import evaluators_improve as ev
    from improve.contract import Run
    from improve.signature import shape_facets

    run = Run(run_id=FFAB73F8, trajectory=real_trajectory(FFAB73F8))
    names = _names(FFAB73F8)

    def facet_claims() -> bool:
        return next(f.value for f in shape_facets(run)
                    if f.name == "session_ended_on_unretrieved_dispatch")

    # At the shipped tolerance ffab73f8 is ambiguous: 17 spans follow its last unpaired
    # dispatch. Neither reader accuses it.
    assert facet_claims() is False
    assert async_job_flow(names).outcome is not Outcome.FAIL

    # Widen the ONE threshold past 17 and BOTH must flip together.
    original = ev._TAIL_TOLERANCE
    try:
        ev._TAIL_TOLERANCE = 100
        assert facet_claims() is True
        assert async_job_flow(names).outcome is Outcome.FAIL
    finally:
        ev._TAIL_TOLERANCE = original
    assert facet_claims() is False


def test_pair_dispatches_reports_the_unpaired_set_as_a_suffix_of_the_dispatches():
    """`unpaired` is provably a SUFFIX of `dispatches`: if no retrieve exceeds `d`, none
    exceeds any later dispatch either. Checked on real sessions rather than argued, because
    `ended_on_unretrieved_dispatch` reads `unpaired[-1]` and would name the wrong span if the
    set were not ordered and trailing.

    No span index is asserted — indices are not stable across harvests
    (`trajectory_for_session` stable-sorts on the parsed timestamp, so spans sharing a
    millisecond keep harvest order and adjacent ones can swap).
    """
    for session in (FFAB73F8, E2CB300B, SIX309F154, A3A8FAC0, ONEB42D744, ABC839CA):
        pairing = pair_dispatches(_names(session))
        n = len(pairing.unpaired)
        assert pairing.unpaired == pairing.dispatches[len(pairing.dispatches) - n:], session
        if pairing.unpaired:
            assert pairing.unpaired[-1] == max(pairing.dispatches), session


def test_the_in_session_retrieve_precondition_is_what_separates_claim_from_ambiguity():
    """The precondition is load-bearing and was established by measurement, not design.

    `36fafeb7` and `74b0a063` are the pair that shows it. Both END on an unpaired dispatch —
    so the tail test alone cannot separate them — and the only difference the spans record is
    that one retrieved earlier in the session and the other never retrieved at all. That
    in-session retrieve is session-local proof that dispatches HERE hand back handles needing
    a fetch, which is what turns "unpaired" into a claim the evidence can carry.

    Measured on the 336 h harvest: the tail test alone fires on 47 sessions and this
    precondition blocks 24 of them.
    """
    inline = pair_dispatches(_async_names("36fafeb7"))
    genuine = pair_dispatches(_async_names("74b0a063"))
    # Same tail state...
    for pairing in (inline, genuine):
        assert pairing.unpaired
        assert pairing.unpaired[-1] >= pairing.span_count - 12
    # ...opposite claims, decided only by whether the session ever retrieved.
    assert inline.retrieved_in_session is False
    assert genuine.retrieved_in_session is True
    assert inline.ended_on_unretrieved_dispatch is False
    assert genuine.ended_on_unretrieved_dispatch is True
    # Neither is dropped: the one that is not claimed is reported as the ambiguity.
    assert inline.unpaired_result_unobserved is True
    assert genuine.unpaired_result_unobserved is False


def test_the_symptom_is_now_a_STRICT_subterm_of_the_claim_facet():
    """`check_failed:AsyncJobFlowValidator` is a pure function of the claim facet, and
    `mine._SUBTERM_OF` refuses that facet on exactly that ground.

    Because the evaluator and the facets read one `pair_dispatches` rule, the symptom is identical
    to `session_ended_on_unretrieved_dispatch` (checked over 541 sessions of a 336 h harvest), and
    if the evaluator's ambiguous case is ever assigned FAIL or NO_DATA it becomes exactly
    `claim OR ambiguity`. So the refusal is load-bearing rather than precautionary; it is harmless
    today only because `classify.GATING_BY_CLASS` gates no class on this evaluator. Pinned so the
    entailment cannot break silently while the refusal keeps assuming it.
    """
    from improve.contract import Run
    from improve.signature import shape_facets

    sessions = [(s, real_trajectory(s)) for s in
                (FFAB73F8, E2CB300B, SIX309F154, A3A8FAC0, ONEB42D744, ABC839CA)]
    sessions += [(p, async_lifecycle_trajectory(p))
                 for p in ("36fafeb7", "74b0a063", "23a1fa24")]
    saw_true = saw_false = False
    for session, trajectory in sessions:
        facets = {f.name: f.value for f in shape_facets(Run(run_id=session,
                                                            trajectory=trajectory))}
        # `symptom_facets` folds FAIL and NO_DATA into one `bad` flag; mirror that fold here
        # rather than checking `is Outcome.FAIL`, because it is the fold mining actually sees.
        bad = async_job_flow(tool_name_sequence(trajectory)).outcome in (Outcome.FAIL,
                                                                        Outcome.NO_DATA)
        assert bad is facets["session_ended_on_unretrieved_dispatch"], session
        saw_true |= bad
        saw_false |= not bad
    # Both sides exercised, or the assertion above passed vacuously on a constant.
    assert saw_true and saw_false


def test_the_rewrite_does_not_change_what_dag_completion_is_told_about_async_work():
    """`evaluate._run_dispatched_async` also reads `_is_dispatch`, and `dag_completion` takes
    `dispatched_async` from it to tell "missing plan evidence" (RED) from "genuinely no DAG"
    (NOT_APPLICABLE). It asks only WHICH spans are dispatches, never how they pair, so it must
    stay insensitive to the pairing rule.
    """
    from improve.contract import Run
    from improve.evaluate import _run_dispatched_async

    for session in (FFAB73F8, E2CB300B, SIX309F154, A3A8FAC0, ONEB42D744, ABC839CA):
        run = Run(run_id=session, trajectory=real_trajectory(session))
        assert _run_dispatched_async(run) is bool(pair_dispatches(_names(session)).dispatches)
        # Every golden session dispatches, so none of them may report "no async work" and let
        # an absent plan read as NOT_APPLICABLE.
        assert _run_dispatched_async(run) is True, session
    assert _run_dispatched_async(Run(run_id="spanless", trajectory=NO_DATA)) is False


# --------------------------------------------------------------------------
# dag_completion — terminality
# --------------------------------------------------------------------------

def test_dag_ignores_unreliable_plan_parent_row():
    """Every real golden session leaves the PLAN row stuck 'pending' even on success,
    so the evaluator must read task-node terminality instead."""
    plan = real_plan(ONEB42D744)
    assert any(n.id == "PLAN" and n.status == "pending" for n in plan.nodes)
    assert dag_completion(plan, dispatched_async=True).outcome is Outcome.PASS


def test_dag_fail_on_real_failed_node_a3a8fac0():
    """a3a8fac0's recognize_faces node genuinely failed (gateway_status=FAILED)."""
    r = dag_completion(real_plan(A3A8FAC0), dispatched_async=True)
    assert r.outcome is Outcome.FAIL
    assert "failed" in r.explanation.lower()


def test_dag_no_data_when_plan_absent_but_run_dispatched_async():
    """Missing evidence for a run that DID dispatch is RED, not 'not applicable'."""
    r = dag_completion(NO_DATA, dispatched_async=True)
    assert r.outcome is Outcome.NO_DATA


def test_dag_not_applicable_when_no_plan_and_no_async_dispatch():
    assert dag_completion(None, dispatched_async=False).outcome is Outcome.NOT_APPLICABLE


# --------------------------------------------------------------------------
# dag_completion — dependency ORDER (DF-11). Terminality is not sufficiency.
# --------------------------------------------------------------------------

def test_ordering_violation_on_real_ffab73f8():
    """DF-11, the session that motivated this check: every node was marked completed
    within 96s while the diarization t2 was told to read was still running.
    t2_read_transcript @18:55:55 precedes its dependency t1_diarize_start @18:58:22.
    Terminality-only scored this PASS; so did both LLM judges."""
    r = dag_completion(real_plan(FFAB73F8), dispatched_async=True)
    assert r.outcome is Outcome.FAIL
    assert "ORDER violated" in r.explanation
    assert "t2_read_transcript" in r.explanation and "t1_diarize_start" in r.explanation


def test_ordering_violation_on_real_e2cb300b():
    """Found by the check, not by hand: sg2_diarize_speech precedes sg1_separate_stems.
    This gives e2cb300b's delivery failure a NON-CIRCULAR explanation, which is what
    dissolved DF-9's 'all-terminal-yet-RED' shape."""
    r = dag_completion(real_plan(E2CB300B), dispatched_async=True)
    assert r.outcome is Outcome.FAIL
    assert "ORDER violated" in r.explanation


def test_failed_node_takes_precedence_over_ordering_on_real_6309f154():
    """6309f154 has BOTH a failed node and an ordering violation. The failed-node
    branch short-circuits first — which is why the ordering violation in this session
    went unreported until measured directly."""
    plan = real_plan(SIX309F154)
    assert _edge_order_violations([n for n in plan.nodes if n.id != "PLAN"])
    r = dag_completion(plan, dispatched_async=True)
    assert r.outcome is Outcome.FAIL
    assert "failed" in r.explanation.lower()


def test_ordering_check_cannot_fire_on_a_legitimately_edgeless_dag():
    """1b42d744's goal was literally 'four independent async analysis jobs', so its
    edgeless DAG is CORRECT. The ordering check is structurally unable to say anything
    about such a plan — documented, not a bug. (abc839ca is the same shape.)"""
    for sid in (ONEB42D744, ABC839CA):
        plan = real_plan(sid)
        task = [n for n in plan.nodes if n.id != "PLAN"]
        assert sum(len(n.depends_on) for n in task) == 0
        assert dag_completion(plan, dispatched_async=True).outcome is Outcome.PASS


def test_ordering_check_is_silent_without_timestamps():
    """can-show-RED applies to this check too: a missing timestamp must yield NO
    verdict rather than a false accusation. Real fixture with the ts fields stripped."""
    plan = real_plan(FFAB73F8)
    stripped = Plan(goal="", nodes=[
        PlanNode(id=n.id, depends_on=n.depends_on, status=n.status)
        for n in plan.nodes
    ])
    assert dag_completion(stripped, dispatched_async=True).outcome is Outcome.PASS
