"""Signature extraction and mining, exercised on the REAL golden plans (REQ-017).

The load-bearing assertion is the negative one: `has_failed_node` must be REJECTED,
because 6309f154 passes while carrying a failed node. A miner that admitted it would
emit a recommendation contradicted by its own evidence.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import (  # noqa: E402
    NO_DATA, UNKNOWN, WINDOW_FROM_SPANS, WINDOW_STAMPED, EvaluatorResult, Facet, FacetKind,
    FailureSignature, InfraHealth, InfraState, Outcome, Overall, Plan, PlanNode, Run, RunWindow,
    UtilityVector, Verdict, tool_name_sequence,
)
from improve.evaluate import DEFAULT_GATING  # noqa: E402
from improve.evaluators_improve import (  # noqa: E402
    _edge_order_violations, _is_dispatch, _is_retrieve, _parse_ts,
)
from improve.judge import DEFAULT_PASS_THRESHOLD  # noqa: E402
from improve.loop import load_golden  # noqa: E402
from improve.mine import (  # noqa: E402
    _FACET_PRECEDENCE, MIN_CONTROLS, _classify, _refine, group_signatures, mine,
)
from improve.mine_local import verdict_from_recorded  # noqa: E402
from improve.signature import HEURISTIC_FACETS, facets_for, shape_facets  # noqa: E402

from improve_fixtures import (  # noqa: E402
    A3A8FAC0, ABC839CA, E2CB300B, FFAB73F8, ONEB42D744, SIX309F154,
    async_lifecycle_trajectory, real_plan, real_trajectory,
)

# Tier scores come from golden.jsonl, never from literals here. An earlier version of the
# sibling fixture module hardcoded session ids and three of six were wrong; hardcoding the
# SCORES is the same hazard with a worse failure mode — a relabel would change which runs act
# as passing controls (and therefore which signatures survive REQ-018) while this file kept
# asserting the old split and stayed green, certifying behaviour that no longer occurs.
SCENARIOS = {s.source_session or s.scenario_id: s for s in load_golden()}
THRESHOLD = DEFAULT_PASS_THRESHOLD
GATING = DEFAULT_GATING


def _run(session: str) -> Run:
    run = Run(run_id=session, plan=real_plan(session), trajectory=real_trajectory(session))
    run.scenario = SCENARIOS[session]
    return run


def _verdict(session: str) -> Verdict:
    """Built by the PRODUCTION path, so the tests cannot disagree with the pipeline about
    what a verdict means — and so `symptom_facets`' NO_DATA branch is reachable."""
    return verdict_from_recorded(_run(session), THRESHOLD, GATING)


def _population():
    sessions = list(SCENARIOS)
    return [_run(s) for s in sessions], [_verdict(s) for s in sessions]


def _facet(session: str, name: str):
    return {f.name: f for f in shape_facets(_run(session))}[name]


# --- facets -----------------------------------------------------------------
def test_recorded_split_matches_the_labels_recorded_in_the_dataset():
    """Not a tautology: `_verdict` runs the production `verdict_from_recorded`, and this
    compares its recomputed Overall against the `expected.overall` string the dataset
    recorded independently. If the two ever diverge, the offline pipeline has stopped
    reproducing the judgement its labels came from."""
    _, verdicts = _population()
    for verdict in verdicts:
        recorded = (SCENARIOS[verdict.run_id].expected or {}).get("overall")
        assert recorded is not None, "golden.jsonl must record an expected overall"
        assert verdict.overall.value == recorded, verdict.run_id
    assert sum(v.overall is Overall.FAIL for v in verdicts) == 2
    assert sum(v.overall is Overall.PASS for v in verdicts) == 4


def test_fanout_parent_ran_early_is_true_only_for_the_delivery_failure():
    """The facet that carries the whole result. It must separate e2cb300b from the two
    runs that also violate ordering but delivered anyway — otherwise it is just a
    restatement of `edge_order_violation`."""
    assert _facet(E2CB300B, "fanout_parent_ran_early").value is True
    # Both of these DO violate ordering; neither has a fan-out parent doing it.
    assert _facet(FFAB73F8, "edge_order_violation").value is True
    assert _facet(FFAB73F8, "fanout_parent_ran_early").value is False
    assert _facet(SIX309F154, "edge_order_violation").value is True
    assert _facet(SIX309F154, "fanout_parent_ran_early").value is False


def test_fanout_facet_evidence_names_the_node_and_its_consumers():
    evidence = _facet(E2CB300B, "fanout_parent_ran_early").evidence
    assert "sg2_diarize_speech" in evidence
    assert "translate_es" in evidence and "translate_fr" in evidence


def test_edgeless_plans_are_flat_and_have_no_ordering_verdict():
    assert _facet(ONEB42D744, "flat_plan").value is True
    assert _facet(ONEB42D744, "chained_plan").value is False
    assert _facet(ONEB42D744, "edge_order_violation").value is False
    assert _facet(ONEB42D744, "has_fanout_node").value is False


def test_failed_node_facet_is_true_for_both_two_node_runs():
    """The premise of the rejection test below: the facet genuinely fires on a passing
    run, so it is not discriminative."""
    assert _facet(A3A8FAC0, "has_failed_node").value is True
    assert _facet(SIX309F154, "has_failed_node").value is True


def test_symptom_facets_are_marked_symptom_and_shape_facets_shape():
    facets = facets_for(_run(E2CB300B), _verdict(E2CB300B))
    assert facets["tier_failed:results_delivered"].kind is FacetKind.SYMPTOM
    assert facets["tier_failed:results_delivered"].value is True
    assert facets["tier_failed:dispatch_correct"].value is False
    assert facets["fanout_parent_ran_early"].kind is FacetKind.SHAPE


def test_absent_plan_and_trajectory_are_facets_not_silence():
    facets = {f.name: f for f in shape_facets(Run(run_id="x"))}
    assert facets["plan_absent"].value is True
    assert facets["trajectory_absent"].value is True


def test_longest_chain_is_cycle_safe():
    """A malformed plan must yield a finite depth rather than recursing forever."""
    cyclic = Plan(nodes=[PlanNode(id="a", depends_on=["b"]), PlanNode(id="b", depends_on=["a"])])
    facets = {f.name: f for f in shape_facets(Run(run_id="c", plan=cyclic))}
    assert facets["deep_plan"].value is False


# --- the timestamp fix ------------------------------------------------------
def test_same_instant_in_two_formats_is_not_an_ordering_violation():
    """The writer emits both `...+00:00` and a bare `...Z`, and both appear in one
    harvest. String comparison called them unequal because 'Z' > '+', manufacturing a
    violation out of two identical timestamps."""
    nodes = [
        PlanNode(id="dep", status="completed", updated_at="2026-08-13T17:22:19.5+00:00"),
        PlanNode(id="child", depends_on=["dep"], status="completed",
                 updated_at="2026-08-13T17:22:19.5Z"),
    ]
    assert _edge_order_violations(nodes) == []


def test_a_real_violation_across_formats_is_still_caught():
    nodes = [
        PlanNode(id="dep", status="completed", updated_at="2026-08-13T17:30:00+00:00"),
        PlanNode(id="child", depends_on=["dep"], status="completed",
                 updated_at="2026-08-13T17:22:19.935Z"),
    ]
    assert [v[0] for v in _edge_order_violations(nodes)] == ["child"]


def test_unparseable_timestamp_yields_no_verdict():
    nodes = [
        PlanNode(id="dep", status="completed", updated_at="not-a-timestamp"),
        PlanNode(id="child", depends_on=["dep"], status="completed",
                 updated_at="2026-08-13T17:22:19+00:00"),
    ]
    assert _edge_order_violations(nodes) == []


# --- mining -----------------------------------------------------------------
def test_mines_one_signature_and_one_residue_from_the_golden_set():
    runs, verdicts = _population()
    result = mine(runs, verdicts)
    assert len(result.signatures) == 1
    assert len(result.residue) == 1
    assert result.signatures[0].run_id == E2CB300B
    assert result.residue[0].run_id == A3A8FAC0


def test_admitted_signature_is_the_fanout_facet_and_gated_to_zero():
    runs, verdicts = _population()
    signature = mine(runs, verdicts).signatures[0]
    assert signature.shape == ("fanout_parent_ran_early",)
    assert signature.pass_hits == 0
    assert signature.pass_hit_runs == ()
    assert "tier_failed:results_delivered" in signature.symptom
    assert signature.heuristic_facets == ()


def test_has_failed_node_is_rejected_because_a_passing_run_carries_one():
    """REQ-018, and the reason mining is a stage rather than a formality."""
    runs, verdicts = _population()
    entry = mine(runs, verdicts).residue[0]
    assert entry.pass_hits == 1
    assert entry.pass_hit_runs == (SIX309F154,)
    assert "REQ-018" in entry.reason
    # ONE pass hit, so the reason is the singular case — and this reason reaches a reader
    # verbatim, because `residue_to_wire` deliberately supplies no `plainReason` for it and the
    # Inbox falls back to `reason`. It read "1 passing run(s)" here.
    assert "separates this failure from 1 passing run — the runs are structurally alike" \
        in entry.reason
    # No admitted signature may rest on the non-discriminative facet.
    for signature in mine(runs, verdicts).signatures:
        assert "has_failed_node" not in signature.shape


def test_refinement_log_records_the_argument():
    runs, verdicts = _population()
    log = mine(runs, verdicts).signatures[0].refinement_log
    assert any("seed:" in line for line in log)
    assert any("still-matching 4 => 0" in line for line in log)


def test_precedence_prefers_the_violation_over_the_topology_it_needs():
    """Both `fanout_parent_ran_early` and `has_fanout_node` reach pass_hits 0 on this
    population. Reporting the topology would describe a normal DAG as the cause."""
    assert (_FACET_PRECEDENCE.index("fanout_parent_ran_early")
            < _FACET_PRECEDENCE.index("has_fanout_node"))
    runs, verdicts = _population()
    assert mine(runs, verdicts).signatures[0].shape == ("fanout_parent_ran_early",)


def test_evidence_gap_routes_away_from_the_proposer():
    """A failure we could not observe is an observability defect, not a behaviour to
    change — proposing a fix from absent evidence is exactly the invention can-show-RED
    exists to prevent."""
    blind = Run(run_id="blind")
    verdict = Verdict("blind", [EvaluatorResult("results_delivered", "llm", Outcome.FAIL, 0.1)],
                      UtilityVector(), Overall.FAIL, "")
    result = mine([blind], [verdict])
    assert result.signatures == []
    assert len(result.evidence_gaps) == 1
    assert "defect register" in result.evidence_gaps[0].reason


def test_unscored_runs_are_neither_evidence_nor_control():
    """An UNSCORED run must not vouch for a pattern by sitting in the passing bucket."""
    runs, verdicts = _population()
    extra = Run(run_id="unscored", plan=real_plan(E2CB300B))
    verdicts = verdicts + [Verdict("unscored", [], UtilityVector(), Overall.UNSCORED, "")]
    result = mine(runs + [extra], verdicts)
    assert len(result.signatures) == 1
    assert any(g.run_id == "unscored" for g in result.evidence_gaps)


def test_no_passing_population_cannot_admit_a_signature():
    """With nothing to exclude, the gate never ran — admitting here would be vacuous."""
    run = _run(E2CB300B)
    result = mine([run], [_verdict(E2CB300B)])
    assert result.signatures == []
    assert len(result.residue) == 1
    assert "could not be applied" in result.residue[0].reason


def test_verdict_without_a_matching_run_is_skipped_not_guessed():
    runs, verdicts = _population()
    verdicts = verdicts + [Verdict("ghost", [], UtilityVector(), Overall.FAIL, "")]
    result = mine(runs, verdicts)
    assert all(s.run_id != "ghost" for s in result.signatures)
    assert all(r.run_id != "ghost" for r in result.residue)


def test_grouping_collapses_identical_patterns_and_counts_members():
    runs, verdicts = _population()
    signatures = mine(runs, verdicts).signatures
    patterns = group_signatures(signatures + signatures)
    assert len(patterns) == 1
    assert len(patterns[0]["member_run_ids"]) == 2


# --- the crash paths the docstrings promised could not happen -----------------
def test_naive_timestamp_does_not_crash_the_comparison():
    """`_parse_ts` promised an AWARE datetime and returned a naive one for an offset-less
    value, so comparing it with an aware sibling raised TypeError — which propagated out of
    dag_completion and evaluate and killed the whole scoring run for that session."""
    nodes = [
        PlanNode(id="dep", status="completed", updated_at="2026-08-13T18:10:24.602910"),
        PlanNode(id="child", depends_on=["dep"], status="completed",
                 updated_at="2026-08-13T18:04:33.783333+00:00"),
    ]
    assert [v[0] for v in _edge_order_violations(nodes)] == ["child"]


def test_non_string_timestamp_yields_no_verdict_rather_than_attribute_error():
    """`updated_at` is typed str but sourced from JSON and DynamoDB, where an epoch number
    is plausible."""
    assert _parse_ts(1786644166838) is None
    nodes = [
        PlanNode(id="dep", status="completed", updated_at=1786644166838),  # type: ignore[arg-type]
        PlanNode(id="child", depends_on=["dep"], status="completed",
                 updated_at="2026-08-13T18:04:33+00:00"),
    ]
    assert _edge_order_violations(nodes) == []


# --- gate integrity ----------------------------------------------------------
def test_unobservable_passing_runs_are_not_counted_as_excluded_controls():
    """The gate's worst failure mode. A passing run whose plan was never captured has every
    plan-derived facet False, so ANY facet excludes it for free — missing data masquerading
    as discrimination. Such runs must be inconclusive, and COUNTED.

    Counted rather than named: an inconclusive control is another run in the population, and this
    reason is published on the globally-readable Improve bundle. The count is what a reader weighs
    the claim by; the ids would be one tenant's sessions shown to every other.
    """
    fail = _run(E2CB300B)
    fail_verdict = _verdict(E2CB300B)
    blind = [Run(run_id=f"blind{i}") for i in range(4)]
    blind_verdicts = [
        Verdict(f"blind{i}",
                [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, 0.9),
                 EvaluatorResult("results_delivered", "llm", Outcome.PASS, 0.9)],
                UtilityVector(), Overall.PASS, "")
        for i in range(4)
    ]
    result = mine([fail] + blind, [fail_verdict] + blind_verdicts)
    assert result.signatures == []
    assert len(result.residue) == 1
    entry = result.residue[0]
    assert "4 could not be judged because their own evidence was missing" in entry.reason
    for i in range(4):
        assert f"blind{i}" not in entry.reason


def test_admitted_signature_records_how_many_controls_it_survived():
    """`pass_hits == 0` against one control is nearly free; against four it is a claim. The
    number has to be on the record or the two are indistinguishable."""
    runs, verdicts = _population()
    signature = mine(runs, verdicts).signatures[0]
    assert signature.controls_excluded == 4
    assert signature.controls_inconclusive == ()
    assert signature.controls_excluded >= MIN_CONTROLS


def test_shape_facets_that_are_subterms_of_a_gating_evaluator_are_refused():
    """`DagCompletionValidator` FAILs iff has_failed_node OR has_non_terminal_node OR
    edge_order_violation — computed by the same code the facets read. Gate on it and those
    facets entail their own symptom, so the gate becomes free: 1 admitted / 1 residue
    silently became 4 admitted / 0 residue."""
    runs, verdicts = _population()
    gating = (*DEFAULT_GATING, "DagCompletionValidator")
    verdicts = [verdict_from_recorded(r, THRESHOLD, gating) for r in runs]
    result = mine(runs, verdicts, gating_evaluators=gating)
    banned = {"has_failed_node", "has_non_terminal_node", "edge_order_violation",
              "fanout_parent_ran_early"}
    for signature in result.signatures:
        assert not (set(signature.shape) & banned), signature.shape
    assert any("refused as subterms" in line
               for s in result.signatures for line in s.refinement_log)


def test_tie_is_recorded_so_precedence_cannot_hide_a_choice():
    """`has_fanout_node` is equally discriminative on this population — no passing run has
    any fan-out topology at all — so the reported facet is chosen by precedence, not by
    evidence. That has to be visible."""
    runs, verdicts = _population()
    signature = mine(runs, verdicts).signatures[0]
    assert "has_fanout_node" in signature.equally_discriminative
    assert any("tie:" in line for line in signature.refinement_log)


def test_heuristic_facets_are_declared_and_propagate_when_used():
    """The marking mechanism was inert: the set was empty while three facets were built on
    unanchored substring matching of span names."""
    assert "async_dispatched" in HEURISTIC_FACETS
    assert "dispatch_unpaired_result_unobserved" in HEURISTIC_FACETS
    assert "session_ended_on_unretrieved_dispatch" in HEURISTIC_FACETS
    run = Run(run_id="h", plan=real_plan(E2CB300B))
    facets = {f.name: f for f in shape_facets(run)}
    assert facets["dispatch_unpaired_result_unobserved"].observed is False, \
        "a span-derived facet must be unobserved when there are no spans"


def test_unreadable_plan_edges_are_a_gap_not_a_flat_plan():
    """An offloaded node list comes back edgeless. Treating that as a flat, well-ordered plan
    is absence reading as a clean bill of health."""
    plan = real_plan(E2CB300B)
    plan.edges_available = False
    facets = {f.name: f for f in shape_facets(Run(run_id="offloaded", plan=plan))}
    assert facets["plan_edges_unreadable"].value is True
    assert facets["flat_plan"].observed is False
    assert facets["has_fanout_node"].observed is False
    assert facets["edge_order_violation"].observed is False


def test_dangling_dependency_does_not_create_a_phantom_fanout():
    plan = Plan(nodes=[
        PlanNode(id="a", depends_on=["ghost"], status="completed"),
        PlanNode(id="b", depends_on=["ghost"], status="completed"),
    ])
    facets = {f.name: f for f in shape_facets(Run(run_id="d", plan=plan))}
    assert facets["has_fanout_node"].value is False


def test_every_facet_is_emitted_for_every_run_so_the_population_is_comparable():
    """Conditionally-added facets forced consumers to treat a missing key as False."""
    rich = {f.name for f in shape_facets(_run(E2CB300B))}
    empty = {f.name for f in shape_facets(Run(run_id="empty"))}
    assert rich == empty


# --- async lifecycle: the two facets that replaced `dispatch_without_retrieve` --------------
# Every session below is REAL (REQ-017), pulled read-only from staging `aws/spans`. None of the
# three is a golden-set member, so they live in their own committed slice — see
# `improve_fixtures.async_lifecycle_trajectory` and
# `scripts/refresh_async_lifecycle_fixture.py`.
def _async_facets(prefix: str) -> dict:
    run = Run(run_id=prefix, trajectory=async_lifecycle_trajectory(prefix))
    return {f.name: f for f in shape_facets(run)}


@pytest.mark.parametrize("prefix", ["36fafeb7", "23a1fa24"])
def test_a_run_returning_results_inline_is_not_claimed_to_have_leaked(prefix):
    """V-02a. Both sessions dispatched with `sync_mode: true` and got their results back
    INLINE, so neither emitted a retrieve span — and the retired facet fired on both, on a
    FAIL and a PASS alike. Spans record no sync/async field for `dispatch_tool` (D-02 / NS-5),
    so "the result came back inline" is unobservable and MUST NOT be resolved as a leak.

    36fafeb7's two dispatches blocked 49.3 s and 49.6 s; its last one sits 9 spans from the
    end, i.e. it satisfies the tail test on its own. What spares it is the absence of any
    retrieve anywhere in the session: nothing shows these dispatches ever handed back a
    handle that needed fetching.
    """
    facets = _async_facets(prefix)
    assert facets["async_dispatched"].value is True
    assert facets["session_ended_on_unretrieved_dispatch"].value is False
    assert facets["session_ended_on_unretrieved_dispatch"].evidence == "", \
        "evidence must track value — a False facet may not assert the thing happened"
    # Not silently dropped either: the ambiguity is reported, without accusing.
    assert facets["dispatch_unpaired_result_unobserved"].value is True
    assert "cannot distinguish" in facets["dispatch_unpaired_result_unobserved"].evidence


def test_an_early_retrieve_does_not_excuse_a_late_unpaired_dispatch():
    """V-02b + V-02c on one real session — 74b0a063 is both shapes at once.

    It retrieved properly (get_plan_results @179, get_job @188/@219, get_job_result @230/@233)
    and then dispatched again at span 239 of 249 and never fetched that one; 9 spans later the
    session ended. The retired rule looked only at `dispatches[0]`, saw retrieves after it, and
    scored the run clean. The in-session retrieves are also what license the claim here: this
    session demonstrably gets handles back from its dispatches.
    """
    facets = _async_facets("74b0a063")
    assert facets["session_ended_on_unretrieved_dispatch"].value is True
    evidence = facets["session_ended_on_unretrieved_dispatch"].evidence
    assert "span 239 of 249" in evidence
    assert "never followed by a retrieval" in evidence
    # The residue facet must not double-report what the claim already covers.
    assert facets["dispatch_unpaired_result_unobserved"].value is False
    assert facets["dispatch_unpaired_result_unobserved"].evidence == ""


def test_a_clipped_trajectory_unobserves_the_async_facets_but_not_trajectory_absent():
    """Two facts about the evidence, kept apart. Real spans, one marked known-incomplete.

    `74b0a063`'s truncated harvest kept its dispatch and dropped all three of its retrieves. On
    that read the run has spans — so `trajectory_absent` is False and must stay False — while
    every span-ORDER claim over it is unusable, because the retrieve that decides the answer is
    exactly what the row cap dropped.

    `is not False`, not truthiness: UNKNOWN is a falsy `_Sentinel`, so a run no harvester checked
    must keep reporting its facets observed. That guard is what stops this change from marking the
    entire golden set unobservable.
    """
    trajectory = async_lifecycle_trajectory("74b0a063")
    complete = {f.name: f for f in shape_facets(Run(run_id="x", trajectory=trajectory))}
    clipped = {f.name: f for f in shape_facets(
        Run(run_id="x", trajectory=trajectory, evidence_complete=False))}

    assert complete["trajectory_absent"].value is False
    assert clipped["trajectory_absent"].value is False, \
        "'no spans at all' and 'spans clipped' are different facts and must not collapse"
    for name in ("async_dispatched", "dispatch_unpaired_result_unobserved",
                 "session_ended_on_unretrieved_dispatch"):
        assert complete[name].observed is True, name
        assert clipped[name].observed is False, name
    # UNKNOWN is the default and must behave like the un-clipped run, not like the clipped one.
    unknown = {f.name: f for f in shape_facets(Run(run_id="x", trajectory=trajectory))}
    assert all(unknown[n].observed is True for n in
               ("async_dispatched", "session_ended_on_unretrieved_dispatch"))


def _async_population(incomplete: tuple = ()):
    """The three REAL async-lifecycle sessions plus two real golden trajectories, no plans.

    Built this way because the golden set's own admitted shape is plan-derived
    (`fanout_parent_ran_early`), so nothing there exercises the trajectory route this change
    takes. Dropping the plans leaves the async facets as the only observable discriminators, which
    is the shape the truncation defect actually occurred in: `74b0a063` FAILS while
    `session_ended_on_unretrieved_dispatch`, and the other four passing runs do not.

    Verdicts are hand-built — as the sibling unobservable-controls test does — because these
    sessions are not golden-set members and carry no recorded labels. `gating` is named
    explicitly: `session_ended_on_unretrieved_dispatch` is refused as a subterm while
    `AsyncJobFlowValidator` gates (`_SUBTERM_OF`), and this population is gated on the two goal
    tiers.
    """
    gating = ("dispatch_correct", "results_delivered")

    def verdict(run_id, overall, delivered):
        return Verdict(
            run_id,
            [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, 0.9),
             EvaluatorResult("results_delivered", "llm",
                             Outcome.PASS if delivered >= 0.7 else Outcome.FAIL, delivered)],
            UtilityVector(), overall, "", workload_class="conversational", gating=gating)

    runs, verdicts = [], []
    for prefix, overall, delivered in (("74b0a063", Overall.FAIL, 0.2),
                                       ("36fafeb7", Overall.PASS, 0.9),
                                       ("23a1fa24", Overall.PASS, 0.9)):
        runs.append(Run(run_id=prefix, trajectory=async_lifecycle_trajectory(prefix),
                        evidence_complete=False if prefix in incomplete else UNKNOWN))
        verdicts.append(verdict(prefix, overall, delivered))
    for session in (ABC839CA, ONEB42D744):
        runs.append(Run(run_id=session, trajectory=real_trajectory(session),
                        evidence_complete=False if session in incomplete else UNKNOWN))
        verdicts.append(verdict(session, Overall.PASS, 0.9))
    return runs, verdicts


def test_an_incomplete_control_is_inconclusive_not_excluded():
    """V-01c: a partially-read control may not vouch for a shape.

    Baseline: `session_ended_on_unretrieved_dispatch` separates `74b0a063`'s failure from four
    passing runs, so it is admitted with `controls_excluded == 4`.

    Mark ONE of those controls known-incomplete and it stops counting. Not because it started
    matching — because its own answer is no longer observable, which is the honest state of a run
    whose retrieves the harvest dropped. `controls_excluded` falls to 3 and the run is NAMED in
    `controls_inconclusive`, which `mine_local._print_report` already prints as "controls NOT
    judgeable (their own evidence was missing)". No new bucket, no new report.

    This is the mechanism that manufactured the false control, closed, and it is why harvest
    completeness is tracked at all: before it, an incomplete run's False read as discrimination, so
    the shape looked better tested than it was.
    """
    runs, verdicts = _async_population()
    baseline = mine(runs, verdicts).signatures
    assert len(baseline) == 1, [s.shape for s in baseline]
    assert baseline[0].shape == ("session_ended_on_unretrieved_dispatch",)
    assert baseline[0].controls_excluded == 4
    assert baseline[0].controls_inconclusive == ()

    runs, verdicts = _async_population(incomplete=("36fafeb7",))
    signature = mine(runs, verdicts).signatures[0]
    assert signature.shape == ("session_ended_on_unretrieved_dispatch",)
    assert signature.controls_excluded == 3, "the clipped control no longer counts"
    assert signature.controls_inconclusive == ("36fafeb7",)
    assert signature.pass_hits == 0, "it is unjudgeable, NOT a counter-example"
    assert signature.controls_excluded >= MIN_CONTROLS


def test_enough_incomplete_controls_turns_an_admitted_signature_into_residue():
    """And the gate stays real: below `MIN_CONTROLS` conclusive controls, nothing is admitted.

    Three of the four controls clipped leaves one conclusive exclusion, which is "it differed from
    the one other run we had". That is residue with the reason spelled out, not a signature with a
    quieter number — the alternative would publish a recommendation whose gate had almost nothing
    to test against.
    """
    runs, verdicts = _async_population(incomplete=("36fafeb7", "23a1fa24", ABC839CA))
    result = mine(runs, verdicts)
    assert result.signatures == []
    assert len(result.residue) == 1
    reason = result.residue[0].reason
    # Singular, and pinned as such: this reason reaches a reader verbatim (`residue_to_wire`
    # refuses to invent a `plainReason`, so the Inbox falls back to it), and one conclusive
    # exclusion is exactly the case the gate exists to report.
    assert "excluded only 1 passing run conclusively" in reason
    assert "could not be judged because their own evidence was missing" in reason


def test_the_retired_facet_reported_the_opposite_answer_on_all_three_sessions():
    """The regression this task exists to prevent, stated as the old predicate.

    `dispatch_without_retrieve` was `bool(dispatches) and not any(r > dispatches[0])`. On real
    data it is TRUE for the two inline runs and FALSE for the one that actually left a dispatch
    unfetched — inverted on every one of the three. Recomputed here rather than trusted, so the
    test still means something if `_is_dispatch`/`_is_retrieve` change.
    """
    old, new = {}, {}
    for prefix in ("36fafeb7", "23a1fa24", "74b0a063"):
        names = tool_name_sequence(async_lifecycle_trajectory(prefix))
        dispatches = [i for i, n in enumerate(names) if _is_dispatch(n)]
        retrieves = [i for i, n in enumerate(names) if _is_retrieve(n)]
        old[prefix] = bool(dispatches) and not any(r > dispatches[0] for r in retrieves)
        new[prefix] = _async_facets(prefix)["session_ended_on_unretrieved_dispatch"].value
    assert old == {"36fafeb7": True, "23a1fa24": True, "74b0a063": False}
    assert new == {"36fafeb7": False, "23a1fa24": False, "74b0a063": True}


def test_the_evidence_span_id_list_is_bounded_by_its_own_constant(monkeypatch):
    """`_MAX_EVIDENCE_SPAN_IDS` is a display bound, and it has to be honoured exactly.

    The evidence string rides to the SNAPSHOT, which is ONE DynamoDB item on a shared 350 KB
    budget, so an unbounded index list is a real hazard. Asserted against the CONSTANT and
    against the fixture's own real unpaired count — never against a literal 12 — or the test
    re-introduces the unnamed-limit problem it guards. It is also deliberately independent of
    the default: patching the bound both AT and BELOW the real count means lowering the default
    (or setting `IMPROVE_MAX_EVIDENCE_SPAN_IDS`) cannot make this pass or fail by accident.
    """
    from improve import signature as signature_mod

    names = tool_name_sequence(async_lifecycle_trajectory("23a1fa24"))
    dispatches = [i for i, n in enumerate(names) if _is_dispatch(n)]
    retrieves = [i for i, n in enumerate(names) if _is_retrieve(n)]
    unpaired = [d for d in dispatches if not any(r > d for r in retrieves)]
    assert len(unpaired) > 2, "this fixture must carry several unpaired dispatches to prove anything"

    def listed(evidence: str) -> str:
        return evidence.split("[", 1)[1].split("]", 1)[0]

    # AT the bound: every index named, and NO "+N more" suffix invented.
    monkeypatch.setattr(signature_mod, "_MAX_EVIDENCE_SPAN_IDS", len(unpaired))
    evidence = _async_facets("23a1fa24")["dispatch_unpaired_result_unobserved"].evidence
    assert [int(x) for x in listed(evidence).split(", ")] == unpaired
    assert "more" not in evidence

    # BELOW the bound: exactly `cap` indices, then the count of those withheld.
    cap = len(unpaired) - 2
    monkeypatch.setattr(signature_mod, "_MAX_EVIDENCE_SPAN_IDS", cap)
    evidence = _async_facets("23a1fa24")["dispatch_unpaired_result_unobserved"].evidence
    shown, _, suffix = listed(evidence).partition(", +")
    assert [int(x) for x in shown.split(", ")] == unpaired[:cap]
    assert suffix == f"{len(unpaired) - cap} more"
    # The COUNT is never truncated: a reader must not have to infer how many there were from
    # how many were printed.
    # `dispatches` spelled out rather than derived from the helper, which would be tautological.
    # Safe to fix here because `len(dispatches) >= len(unpaired) > 2` is asserted above, so this
    # fixture cannot reach the singular; the singular rule itself is
    # `test_improve_contract.test_only_exactly_one_is_singular`.
    assert evidence.startswith(f"{len(unpaired)} of {len(dispatches)} dispatches")


def test_the_async_facet_evidence_reads_as_english_at_its_real_counts():
    """These strings reach a reader verbatim — `dto._pattern_evidence` joins a signature's facet
    evidence into `rootCauseHypo` on the owning actor's bundle — so they are pinned whole here,
    not by prefix. They used to say `dispatch(es)`, `span(s)` and `result(s)`.

    Three words in the second sentence agree with TWO different counts: `dispatches` with the
    denominator, `spans` and the verb with the numerator. Whole-string equality is what catches a
    rewrite that agreed one of them with the wrong number, or lost a space between two f-strings.

    NONE of the three committed sessions reaches a count of 1 on these facets (36fafeb7 and
    23a1fa24 carry 2 and 11 unpaired dispatches, 74b0a063 retrieves 5 results and ends 9 spans
    after its last dispatch), and no span sequence is invented here to manufacture one — REQ-017.
    The singular branch is pinned by `test_improve_contract.test_only_exactly_one_is_singular`
    and, end to end on a real producer path, by
    `test_improve_propose.test_the_evidence_summary_agrees_with_a_single_run`.
    """
    inline = _async_facets("36fafeb7")
    assert inline["async_dispatched"].evidence == "2 dispatch calls"
    assert inline["dispatch_unpaired_result_unobserved"].evidence == (
        "2 of 2 dispatches, at spans [100, 167], have no later retrieval, and this session "
        "retrieved 0 results in total; the results may have been returned inline, which spans "
        "cannot distinguish from a leak")

    leaked = _async_facets("74b0a063")
    assert leaked["async_dispatched"].evidence == "3 dispatch calls"
    assert leaked["session_ended_on_unretrieved_dispatch"].evidence == (
        "this session retrieved 5 results for earlier dispatches, so its dispatches do return "
        "handles that need fetching — yet its last dispatch (span 239 of 249) was never followed "
        "by a retrieval and the session ended 9 spans later")


def test_the_display_bound_cannot_change_what_a_facet_claims(monkeypatch):
    """`_TAIL_TOLERANCE` and `_MAX_EVIDENCE_SPAN_IDS` share the value 12 today and mean unrelated
    things — a measured semantic threshold and a display bound. The trap is someone re-measuring
    the first (which its own comment invites) and moving the second with it. This pins the
    property that makes them safely independent: squeezing the display bound to its minimum
    changes evidence PROSE and no facet VALUE anywhere in the population.

    The two live in different modules — `evaluators_improve._TAIL_TOLERANCE`, with the pairing rule
    both of its readers share, and `signature._MAX_EVIDENCE_SPAN_IDS`, with the evidence string it
    truncates. That helps a little and settles nothing: the shared LITERAL is the trap, not the
    proximity.

    That re-measurement has since happened (2026-08-29, verified-complete 336 h harvest). It kept
    `_TAIL_TOLERANCE` at 12 and left this bound untouched, so the shared literal is now a
    known-persistent coincidence rather than an unexamined one — which makes the trap MORE likely,
    not less, because the next reader sees two constants that have agreed across a re-measurement.

    Why this asserts a PROPERTY and not a count: the bound DOES truncate on real traffic. Over a
    336 h harvest the per-session unpaired-dispatch counts run 1x38, 2x8, 3x8, 4x3, 5x4, 6x2, 9x3,
    11x2, 13, 14 — so two sessions exceed the cap of 12. That is the bound doing its job (the exact
    count stays exact and only the index list gets a `+N more` suffix), and it is why "this never
    truncates a real session" is not a safe thing to record: that would be a claim about last
    fortnight's traffic, not about the code.
    """
    from improve import signature as signature_mod

    runs = [_run(s) for s in (FFAB73F8, E2CB300B, SIX309F154, A3A8FAC0, ONEB42D744, ABC839CA)]
    before = [{f.name: f.value for f in shape_facets(r)} for r in runs]
    evidence_before = [f.evidence for f in shape_facets(runs[0])
                       if f.name == "dispatch_unpaired_result_unobserved"]

    monkeypatch.setattr(signature_mod, "_MAX_EVIDENCE_SPAN_IDS", 1)
    after = [{f.name: f.value for f in shape_facets(r)} for r in runs]
    evidence_after = [f.evidence for f in shape_facets(runs[0])
                      if f.name == "dispatch_unpaired_result_unobserved"]

    assert before == after, "a display bound must never move a facet value"
    # ffab73f8 has 2 unpaired dispatches, so a bound of 1 must visibly truncate — otherwise the
    # assertion above passed only because nothing was exercised.
    assert evidence_before != evidence_after
    assert "+1 more" in evidence_after[0]


def test_a_run_with_no_spans_leaves_both_async_facets_unobserved():
    """Invariant 2. Omit either name from `_NEEDS_TRAJECTORY` and a spanless run reports
    `value=False, observed=True` — missing evidence reading as a clean bill of health."""
    facets = {f.name: f for f in shape_facets(Run(run_id="spanless", plan=real_plan(E2CB300B)))}
    for name in ("async_dispatched", "dispatch_unpaired_result_unobserved",
                 "session_ended_on_unretrieved_dispatch"):
        assert facets[name].observed is False, name
        assert facets[name].value is False, name


def test_a_dispatch_the_session_kept_working_past_is_not_called_the_session_end():
    """The tail test earns its constant — and this test is what PINS THE UPPER BOUND of
    `_TAIL_TOLERANCE`'s honest range. Do not delete it without re-deriving that bound.

    ffab73f8 (golden, PASS) leaves two dispatches unpaired, the last at span 523 of 541 — but 17
    spans follow it, including six S3.GetObject reads after the closing model turn, so the session
    did NOT end there. It must land in the ambiguous bucket, not be accused.

    `_TAIL_TOLERANCE` is 12, re-measured 2026-08-29 on a verified-complete 336 h harvest: across
    the 73 real sessions whose last dispatch was unpaired the trailing span count clusters at
    9-10 (one fixed close-out sequence, and there is no 11), and the next value up is 17 — which
    is THIS session — then 18. So ffab73f8 is the gap's upper wall: any T >= 18 accuses it, which
    is why the honest range stops at 17. It ran 2026-08-13, past the `aws/spans` 14-day retention,
    so this fixture is the only surviving evidence for that wall — the live log group cannot
    re-derive it.
    """
    facets = {f.name: f for f in shape_facets(_run(FFAB73F8))}
    assert facets["dispatch_unpaired_result_unobserved"].value is True
    assert facets["session_ended_on_unretrieved_dispatch"].value is False


def test_no_facet_name_still_carries_the_retired_facet():
    """Seven sites keyed on `dispatch_without_retrieve`, and a missed one fails SILENTLY: an
    orphaned key in `_SUBTERM_OF` never refuses anything, an orphaned `_FACET_KEYWORDS` entry
    never attributes, an unlisted facet sorts last in `_FACET_PRECEDENCE` and is treated as
    structural. Asserted over the real containers rather than by grepping source, because the
    modules mention the retired name deliberately, in comments explaining its retirement.
    """
    from improve import mine as mine_mod
    from improve import propose as propose_mod
    from improve.signature import _NEEDS_PLAN, _NEEDS_TRAJECTORY

    emitted = {f.name for f in shape_facets(_run(FFAB73F8))}
    # `dispatch_unpaired_mid_session` was this task's own first name for the ambiguity facet and
    # is retired for the same reason as the original: it made a POSITIONAL claim that is false
    # for the two sessions that land in it (36fafeb7 ends on span 167 of 177, 23a1fa24 on 1263
    # of 1296 — both ENDED on an unpaired dispatch). Pinned so a revert cannot restore it.
    for retired in ("dispatch_without_retrieve", "dispatch_unpaired_mid_session"):
        assert retired not in HEURISTIC_FACETS, retired
        assert retired not in _NEEDS_TRAJECTORY, retired
        assert retired not in _NEEDS_PLAN, retired
        assert retired not in mine_mod._SUBTERM_OF, retired
        assert retired not in mine_mod._FACET_PRECEDENCE, retired
        assert retired not in propose_mod._FACET_KEYWORDS, retired
        assert retired not in propose_mod._FACET_PROSE, retired
        # ...and no run can still emit it.
        assert retired not in emitted, retired
    # Both replacements ARE classified, so neither silently sorts last as "structural".
    for name in ("dispatch_unpaired_result_unobserved", "session_ended_on_unretrieved_dispatch"):
        assert name in mine_mod._FACET_PRECEDENCE, name
        assert name in mine_mod._SUBTERM_OF, name
        assert name in propose_mod._FACET_PROSE, name
    # The remedy template is keyed on the CLAIM only; the ambiguous facet gets no draft edit.
    assert "session_ended_on_unretrieved_dispatch" in propose_mod._FACET_KEYWORDS
    assert "dispatch_unpaired_result_unobserved" not in propose_mod._FACET_KEYWORDS


# --- the platform-was-degraded gate ------------------------------------------
# E2CB300B is normally the ONE admitted signature on the golden set; using it as the failing
# run under test proves the gate diverts a run that WOULD otherwise be mined, not one that
# was already residue.
_DEGRADED = InfraHealth(state=InfraState.DEGRADED, reason="2 alarms firing over the window",
                        alarms=("BedrockRuntimeThrottle", "AgentCore5xx"), source="cloudwatch")
_HEALTHY = InfraHealth(state=InfraState.HEALTHY, reason="no alarms in window")
_UNKNOWN_HEALTH = InfraHealth(state=InfraState.UNKNOWN,
                              reason="window older than alarm-history retention")
_WINDOW_ARGS = dict(start="2026-08-13T17:00:00+00:00", end="2026-08-13T17:30:00+00:00")
_DERIVED_WINDOW = RunWindow(provenance=WINDOW_FROM_SPANS, **_WINDOW_ARGS)
_STAMPED_WINDOW = RunWindow(provenance=WINDOW_STAMPED, **_WINDOW_ARGS)


def _stamp(runs, run_id, *, health=None, window=None):
    for run in runs:
        if run.run_id == run_id:
            if health is not None:
                run.infra_health = health
            if window is not None:
                run.window = window
    return runs


def test_failing_run_under_degraded_platform_goes_to_environment_and_never_a_signature():
    """Acceptance #1. A DEGRADED window diverts a failing run to the environment bucket
    BEFORE any pattern-matching; it produces no signature and no residue entry."""
    runs, verdicts = _population()
    _stamp(runs, E2CB300B, health=_DEGRADED)
    result = mine(runs, verdicts)
    assert all(s.run_id != E2CB300B for s in result.signatures)
    assert all(r.run_id != E2CB300B for r in result.residue)
    diverted = [e for e in result.environment if e.run_id == E2CB300B]
    assert len(diverted) == 1
    assert "DEGRADED" in diverted[0].reason and "ops" in diverted[0].reason
    assert result.summary()["environment"] == 1


def test_same_failing_run_with_healthy_platform_is_mined_exactly_as_before():
    """Acceptance #2. Proves the gate is not merely suppressing findings: with HEALTHY the
    same run yields the same signature it would with no infra_health at all."""
    runs, verdicts = _population()
    baseline = mine(runs, verdicts).signatures[0]
    assert baseline.run_id == E2CB300B  # sanity: this is the mined one

    runs2, verdicts2 = _population()
    _stamp(runs2, E2CB300B, health=_HEALTHY)  # window stays UNKNOWN -> no caveat
    result = mine(runs2, verdicts2)
    assert result.environment == []
    sig = result.signatures[0]
    assert sig.run_id == E2CB300B
    assert sig.shape == baseline.shape == ("fanout_parent_ran_early",)
    assert sig.pass_hits == baseline.pass_hits == 0
    assert sig.controls_excluded == baseline.controls_excluded
    assert sig.refinement_log == baseline.refinement_log


def test_unknown_platform_does_not_gate():
    """Acceptance #3. UNKNOWN must NOT gate — the failure mode that would silently empty the
    mining population the moment alarm history expires."""
    runs, verdicts = _population()
    _stamp(runs, E2CB300B, health=_UNKNOWN_HEALTH)
    result = mine(runs, verdicts)
    assert result.environment == []
    assert len(result.signatures) == 1
    assert result.signatures[0].run_id == E2CB300B
    assert result.signatures[0].shape == ("fanout_parent_ran_early",)


def test_degraded_passing_run_stays_a_control_and_controls_excluded_is_unchanged():
    """Acceptance #4. Only FAILURES are gated. A degraded PASSING run is still a control, so
    the admitted signature's controls_excluded is identical to the un-stamped scenario."""
    runs, verdicts = _population()
    baseline = mine(runs, verdicts).signatures[0]

    runs2, verdicts2 = _population()
    _stamp(runs2, SIX309F154, health=_DEGRADED)  # SIX309F154 PASSES on the golden set
    result = mine(runs2, verdicts2)
    assert result.environment == []           # a passing run is never diverted
    sig = result.signatures[0]
    assert sig.run_id == E2CB300B
    assert sig.controls_excluded == baseline.controls_excluded == 4


def test_non_gated_healthy_run_on_a_derived_window_carries_a_caveat():
    """Judgement #4. A run that PROCEEDS to mining but whose HEALTHY verdict rests on a
    derived (possibly too-narrow) window is annotated, not gated."""
    runs, verdicts = _population()
    _stamp(runs, E2CB300B, health=_HEALTHY, window=_DERIVED_WINDOW)
    sig = mine(runs, verdicts).signatures[0]
    assert sig.run_id == E2CB300B  # NOT gated
    assert any("derived" in line.lower() and "narrow-window" in line.lower()
               for line in sig.refinement_log)


def test_healthy_run_on_a_stamped_window_carries_no_caveat():
    """Judgement #4, the negative. A stamped window cannot be a narrow-window miss, so no
    caveat is attached."""
    runs, verdicts = _population()
    _stamp(runs, E2CB300B, health=_HEALTHY, window=_STAMPED_WINDOW)
    sig = mine(runs, verdicts).signatures[0]
    assert not any("narrow-window" in line.lower() for line in sig.refinement_log)


def test_no_reason_this_engine_writes_names_a_session():
    """The guard that makes `dto.residue_to_wire` able to publish `reason` at all.

    Every residue / evidence-gap / environment record's `reason` reaches the globally-readable
    Improve bundle, so a session id interpolated into one is one tenant's run shown to every other
    actor. Two constructions used to do exactly that — the pass-hit list, which duplicated
    `pass_hit_runs` on the same object, and the unjudgeable-control list, which is now a count.

    Driven over the REAL golden population and over the degenerate shapes that reach the other
    branches, and asserted on `mine`'s OUTPUT rather than by reading the templates — a new branch
    added later is covered without anyone remembering to come back here. The full id and its 8-char
    prefix are both probed, because the two that leaked used the prefix.
    """
    fail = _run(E2CB300B)
    blind = [Run(run_id=f"6309f154-blind-{i}") for i in range(4)]
    blind_verdicts = [
        Verdict(f"6309f154-blind-{i}",
                [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, 0.9),
                 EvaluatorResult("results_delivered", "llm", Outcome.PASS, 0.9)],
                UtilityVector(), Overall.PASS, "")
        for i in range(4)
    ]
    populations = [
        _population(),                                   # admitted + residue with pass hits
        ([fail] + blind, [_verdict(E2CB300B)] + blind_verdicts),   # inconclusive controls
        ([fail], [_verdict(E2CB300B)]),                   # no passing runs at all
    ]

    reasons, ids = [], set()
    for runs, verdicts in populations:
        result = mine(runs, verdicts)
        for bucket in (result.residue, result.evidence_gaps, result.environment):
            reasons.extend(entry.reason for entry in bucket)
        ids.update(run.run_id for run in runs)
    assert reasons, "no reason was produced, so this test would prove nothing"

    leaked = sorted({probe for probe in (ids | {i[:8] for i in ids})
                     for reason in reasons if probe in reason})
    assert not leaked, (
        f"a session id reached a published reason: {leaked}. Put the ids on a structured field "
        f"(as `pass_hit_runs` does) rather than in prose, or the wire cannot carry the sentence.")


# --- the gate's own denominator ----------------------------------------------
def _shape_facet(name: str, value: bool, observed: bool = True) -> Facet:
    return Facet(name=name, kind=FacetKind.SHAPE, value=value, observed=observed,
                 evidence=f"{name}={value}")


def _controls_scenario() -> tuple:
    """A TWO-ROUND refinement in which one control goes inconclusive in the FIRST round.

    Hand-built because the golden set has no such population: every one of its signatures resolves
    in a single round, so the accumulation this exercises cannot be reached from the fixtures.

    `edge_order_violation` is chosen first because it leaves the fewest controls still matching (2
    against 3); it excludes `dep-clean-1`/`dep-clean-2` conclusively and leaves `blind` UNJUDGEABLE,
    because that run's own edge evidence was never captured. `has_fanout_node` then finishes the job
    on the two that remain — and `blind` is never looked at again, because it left the matching set
    when it went inconclusive.
    """
    fail = "failing"
    controls = ["blind", "still-a", "still-b", "dep-clean-1", "dep-clean-2"]
    facets = {
        fail: {"edge_order_violation": _shape_facet("edge_order_violation", True),
               "has_fanout_node": _shape_facet("has_fanout_node", True)},
        # Its plan edges were never read, so its non-match proves nothing.
        "blind": {"edge_order_violation": _shape_facet("edge_order_violation", False,
                                                       observed=False),
                  "has_fanout_node": _shape_facet("has_fanout_node", True)},
        "still-a": {"edge_order_violation": _shape_facet("edge_order_violation", True),
                    "has_fanout_node": _shape_facet("has_fanout_node", False)},
        "still-b": {"edge_order_violation": _shape_facet("edge_order_violation", True),
                    "has_fanout_node": _shape_facet("has_fanout_node", False)},
        "dep-clean-1": {"edge_order_violation": _shape_facet("edge_order_violation", False),
                        "has_fanout_node": _shape_facet("has_fanout_node", True)},
        "dep-clean-2": {"edge_order_violation": _shape_facet("edge_order_violation", False),
                        "has_fanout_node": _shape_facet("has_fanout_node", True)},
    }
    return fail, controls, facets


def test_a_control_that_went_inconclusive_early_is_never_recounted_as_excluded():
    """The gate's central number, over a refinement that takes more than one round.

    `_classify` decides each control against the CURRENT trial shape, and a control it calls
    inconclusive leaves the still-matching set — so a later round never sees it again. Recording only
    the last round's inconclusive set therefore forgot it, and `excluded` (everything neither still
    matching nor inconclusive) then counted it as a control the shape had conclusively excluded. That
    is exactly the missing-data-as-discrimination the module docstring says this rule prevents, and
    it inflated the one number a reader weighs the claim by.

    The check is against `_classify`'s own verdict on the FINAL shape rather than against a literal:
    `blind` is inconclusive under `(edge_order_violation, has_fanout_node)` and cannot be described
    as excluded by any reading of the shape that was actually admitted.
    """
    fail, controls, facets = _controls_scenario()
    signature = _refine(fail, ("tier_failed:results_delivered",),
                        ["edge_order_violation", "has_fanout_node"], facets, controls,
                        max_conjuncts=4, min_controls=MIN_CONTROLS, gap=[], banned=set())

    assert isinstance(signature, FailureSignature), signature
    assert signature.shape == ("edge_order_violation", "has_fanout_node")
    assert _classify("blind", signature.shape, facets) == "inconclusive"
    assert signature.controls_inconclusive == ("blind",)
    assert signature.controls_excluded == 4
    assert signature.pass_hits == 0


def test_an_admitted_signature_records_the_pool_its_control_counts_are_out_of():
    """`controls_excluded: 4` is unreadable without how many controls there were. 4 of 4 is a gate
    that decided everything it was offered; 4 of 5 is one that could not place a fifth.

    Recorded rather than re-derived, because nothing downstream can recover it: `_controls_for`
    narrows the population's passing runs by workload class and by parent request before the gate
    ever sees them, so a population-wide pass count is a different number.

    The triple also CLOSES for an admitted signature — excluded plus inconclusive is the whole pool —
    which is what lets a reader check the three against each other instead of trusting one.
    """
    fail, controls, facets = _controls_scenario()
    signature = _refine(fail, ("tier_failed:results_delivered",),
                        ["edge_order_violation", "has_fanout_node"], facets, controls,
                        max_conjuncts=4, min_controls=MIN_CONTROLS, gap=[], banned=set())
    assert signature.controls_considered == len(controls) == 5
    assert (signature.controls_excluded + len(signature.controls_inconclusive)
            == signature.controls_considered)


def test_the_golden_populations_signature_reports_a_pool_equal_to_its_controls():
    """And the same on the real fixtures, where the refinement is one round: four controls offered,
    four conclusively excluded, none unjudgeable. `4 of 4` is the strongest form the triple can
    take, and it has to be distinguishable from `4` alone."""
    runs, verdicts = _population()
    signature = mine(runs, verdicts).signatures[0]
    assert signature.controls_considered == 4
    assert signature.controls_excluded == 4
    assert signature.controls_inconclusive == ()


def _admitted(run_id: str, excluded: int, inconclusive: tuple, considered: int,
              ties: tuple = ()) -> FailureSignature:
    return FailureSignature(
        run_id=run_id, symptom=("tier_failed:results_delivered",),
        shape=("has_failed_node",), pass_hits=0,
        controls_excluded=excluded, controls_inconclusive=inconclusive,
        controls_considered=considered, equally_discriminative=ties)


def test_a_patterns_gate_triple_is_one_members_and_reconciles():
    """Grouping takes the WEAKEST member's whole triple, not a min of one number and a max of
    another.

    The three only reconcile within a single refinement — `excluded + inconclusive == considered` is
    a fact about one run's gate, not about a set of them — so mixing members would publish three
    numbers describing no run in the population and not adding up. The weakest member is the one with
    the fewest conclusively-excluded controls, which is the rule the previous `min` implemented for
    that one field.
    """
    weak = _admitted("weak", excluded=2, inconclusive=("x", "y"), considered=4)
    strong = _admitted("strong", excluded=9, inconclusive=(), considered=9)
    pattern = group_signatures([strong, weak])[0]

    assert pattern["member_run_ids"] == ["strong", "weak"]
    assert pattern["controls_excluded"] == 2
    assert pattern["controls_inconclusive"] == ("x", "y")
    assert pattern["controls_considered"] == 4
    assert (pattern["controls_excluded"] + len(pattern["controls_inconclusive"])
            == pattern["controls_considered"])


def test_a_patterns_equally_discriminative_set_stays_a_union_across_members():
    """A different kind of fact from the control counts, so a different fold. It says the data failed
    to distinguish this explanation from another, which is a defect of the pattern if it happened to
    ANY member — and a member with a strong control count can still be the one whose data was
    ambiguous."""
    weak = _admitted("weak", excluded=2, inconclusive=(), considered=2, ties=("has_fanout_node",))
    strong = _admitted("strong", excluded=9, inconclusive=(), considered=9, ties=("deep_plan",))
    pattern = group_signatures([weak, strong])[0]
    assert pattern["equally_discriminative"] == ("deep_plan", "has_fanout_node")
    assert pattern["controls_excluded"] == 2
