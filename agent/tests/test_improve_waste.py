"""The efficiency path, exercised on REAL harvested fixtures (REQ-017).

The load-bearing assertion is ``test_waste_is_found_in_a_run_whose_production_verdict_is_PASS``:
it is what makes this a different path from mining rather than a second copy of it. ``mine``
iterates failing runs only and REQ-018 then requires a shape to be FALSE across every passing
control, so a waste-derived facet lands ``pass_hits > 0`` and dies as residue. Nothing here may
branch on pass/fail, and one test asserts that from the function signature outwards.

Every session named here is real and committed: the six golden sessions
(``fixtures/plan_state_golden.json``) and the three async-lifecycle ground-truth sessions
(``fixtures/async_lifecycle_sessions.json``). No trajectory is fabricated, and no test pins a span
INDEX — ``trajectory_for_session`` sorts on the parsed timestamp with a stable sort, so spans
sharing a millisecond keep harvest order and adjacent indices can swap between harvests.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import UNMEASURED, Overall, Plan, Run, tool_name_sequence  # noqa: E402
from improve.evaluate import DEFAULT_GATING  # noqa: E402
from improve.judge import DEFAULT_PASS_THRESHOLD  # noqa: E402
from improve.loop import load_golden  # noqa: E402
from improve.mine_local import verdict_from_recorded  # noqa: E402
from improve.signature import shape_facets  # noqa: E402
from improve.waste import (  # noqa: E402
    WASTE_KINDS, _MIN_REPEAT_LEN, find_waste, is_tool_span, logical_calls,
)

from improve_fixtures import (  # noqa: E402
    A3A8FAC0, ABC839CA, E2CB300B, FFAB73F8, ONEB42D744, SIX309F154,
    async_lifecycle_session_id, async_lifecycle_trajectory, real_plan, real_tool_trajectory,
    real_trajectory,
)

SCENARIOS = {s.source_session or s.scenario_id: s for s in load_golden()}


def _run(session: str) -> Run:
    run = Run(run_id=session, plan=real_plan(session), trajectory=real_trajectory(session))
    run.scenario = SCENARIOS[session]
    return run


def _async_run(prefix: str) -> Run:
    """One of the three async-lifecycle ground-truth sessions. No plan: they are not golden-set
    members, so `plan_state_golden.json` does not carry their nodes — and this path reads spans."""
    return Run(run_id=async_lifecycle_session_id(prefix),
               trajectory=async_lifecycle_trajectory(prefix))


def _findings(run: Run, kind: str = "") -> list:
    found = find_waste([run]).findings
    return [f for f in found if not kind or f.kind == kind]


def _only(run: Run, kind: str):
    found = _findings(run, kind)
    assert len(found) == 1, f"expected exactly one {kind} for {run.run_id[:8]}, got {found}"
    return found[0]


# --- the unit of work: one logical call, not one span -----------------------
def test_the_two_instrumentation_layers_of_one_mcp_call_collapse_into_one_call():
    """A gateway tool call reaches `aws/spans` twice and normalises to the same name both times.

    `abc839ca` really records `mcp tools/call media-processing___process_media` immediately
    followed by `execute_tool media-processing___process_media` — one call, two spans, and
    `tool_name_sequence` strips both down to `process_media`. Counting spans would report every
    gateway call as a duplicate of itself, which is why the collapse exists before any signal runs.
    """
    trajectory = real_trajectory(ABC839CA)
    spans = [n for n in tool_name_sequence(trajectory)
             if n == "process_media"]
    calls = [c for c in logical_calls(trajectory) if c.name == "process_media"]
    assert len(spans) == 4, "the fixture should still carry two spans per process_media call"
    assert len(calls) == 2
    assert all(len(c.raw_names) == 2 for c in calls), "both layers must be recorded on the call"
    assert all(len({*c.raw_names}) == 2 for c in calls), "and they must be DIFFERENT raw names"


def test_a_repeated_identical_raw_name_is_two_calls_not_one():
    """The collapse keys on the raw name, so a genuine second call is not swallowed.

    `abc839ca` fetches `mcp tools/call media-outputs___get_download_url` many times in a row with
    no `execute_tool` partner — different files, one span each. If the rule collapsed on the
    normalised name alone, a batch of N downloads would read as one call and every cost below it
    would be understated.
    """
    calls = logical_calls(real_trajectory(ABC839CA))
    downloads = [c for c in calls if c.name == "get_download_url"]
    assert len(downloads) > 5
    assert any(len(c.raw_names) == 1 for c in downloads)


def test_non_tool_spans_never_reach_a_signal():
    """The trajectory is ~90% runtime and AWS-SDK instrumentation, and it repeats by construction.

    `evaluators_improve._TAIL_TOLERANCE`'s measurement records the fixed nine-span close-out
    sequence every session ends with. If those spans participated, every session would carry a
    repeat of the event loop and the signal would measure the runtime rather than the agent.
    """
    trajectory = real_trajectory(FFAB73F8)
    assert len(trajectory) > 500, "the fixture must be the production-faithful ALL-spans slice"
    calls = logical_calls(trajectory)
    assert all(is_tool_span(raw) for call in calls for raw in call.raw_names)
    # The close-out spans are in the trajectory and out of the call list.
    assert "chat" in {s.name for s in trajectory}
    assert "chat" not in {call.name for call in calls}
    assert len(calls) < len(trajectory) / 5


# --- repeated_sequence ------------------------------------------------------
def test_a_repeated_sequence_is_reported_once_not_once_per_subsequence():
    """One event, one finding. A seven-call block repeated once contains six two-call repeats and
    five three-call repeats; reporting every sub-repeat would turn one observation into twenty."""
    finding = _only(_async_run("36fafeb7"), "repeated_sequence")
    assert finding.occurrences == 2
    assert finding.detail == "run_model x6 -> dispatch_tool"


def test_the_storyboard_case_is_found_and_priced_without_reading_a_tool_argument():
    """A repeated block is found and priced from span NAMES, STATUS and DURATION alone.

    On the session that motivated this signal: `36fafeb7` generated six images, dispatched, then
    generated six images again and dispatched again. No tool arguments are read, which NS-5 records
    are unavailable for harness builtins anyway.
    """
    finding = _only(_async_run("36fafeb7"), "repeated_sequence")
    assert finding.calls == 7, "six generations plus the dispatch that followed them"
    assert finding.wall_ms == 83925
    assert "NOT ESTABLISHED" in finding.confidence


def test_the_earlier_occurrence_is_the_one_charged():
    """The cost is the discarded work, so it must be every occurrence BUT the last.

    Checked against the arithmetic rather than a literal: the charged calls are the block's own
    size times one fewer than the number of occurrences.
    """
    run = _async_run("36fafeb7")
    finding = _only(run, "repeated_sequence")
    calls = logical_calls(run.trajectory)
    block_calls = finding.calls // (finding.occurrences - 1)
    assert finding.calls == block_calls * (finding.occurrences - 1)
    # ...and the charged calls are all EARLIER than the last occurrence's first call.
    assert max(finding.charged) < len(calls) - 1


def test_a_run_of_one_repeated_tool_is_not_reported_as_a_repeated_sequence():
    """The measurement behind `_MIN_REPEAT_LEN`, in test form.

    `ffab73f8` polls `get_job_status` 21 times in a row and fetches `get_download_url` six times
    in a row. Polling is how a synchronous agent WAITS and a burst of downloads is N different
    files; neither is redone work, and the longest-repeated-substring search would otherwise split
    a homogeneous run down the middle and report each half as an occurrence of the other.
    """
    names = tool_name_sequence(real_trajectory(FFAB73F8))
    assert names.count("get_job_status") > 15, "the fixture must still carry the poll loop"
    finding = _only(_run(FFAB73F8), "repeated_sequence")
    assert "get_job_status" not in finding.detail
    assert _MIN_REPEAT_LEN == 2
    # Every reported block spans at least two DIFFERENT tools, which is what counting steps buys.
    tools = {part.split(" x")[0] for part in finding.detail.split(" -> ")}
    assert len(tools) >= 2


def test_wall_ms_is_unmeasured_when_a_contributing_duration_is_absent():
    """Invariant 1: a partial sum presented as a total is the same lie as an unpriced cost chart.

    `real_tool_trajectory` is the same real spans with durations left off, which is exactly the
    shape a pre-duration harvest has. The finding must still be produced — the repetition is
    observable — with `wall_ms` the UNMEASURED sentinel rather than a truncated number or 0.
    """
    run = Run(run_id=ABC839CA, plan=Plan(), trajectory=real_tool_trajectory(ABC839CA))
    finding = _only(run, "repeated_sequence")
    assert finding.calls > 0
    assert finding.wall_ms is UNMEASURED
    assert finding.wall_ms != 0


def test_the_detail_string_is_bounded_and_says_how_much_it_hid():
    """`detail` rides to a DynamoDB item and a real block can be ten steps of a hundred calls.

    Exercised by SHRINKING the bound against a real six-step block (`23a1fa24`) rather than by
    inventing a longer trajectory: no committed fixture repeats more than six steps, and REQ-017
    forbids fabricating one. The COUNTS stay exact when the text truncates — that is the whole
    point of an operational display bound.
    """
    import improve.waste as waste_module

    run = _async_run("23a1fa24")
    full = _only(run, "repeated_sequence")
    assert full.detail.count(" -> ") == 5, "the fixture must still carry a six-step block"

    original = waste_module._MAX_DETAIL_NAMES
    try:
        waste_module._MAX_DETAIL_NAMES = 3
        truncated = _only(run, "repeated_sequence")
    finally:
        waste_module._MAX_DETAIL_NAMES = original

    assert truncated.detail.endswith("+3 more")
    assert truncated.detail.count(" -> ") == 3
    assert truncated.calls == full.calls, "truncating the text must not change the cost"
    assert truncated.wall_ms == full.wall_ms


# --- abandoned_dispatch -----------------------------------------------------
def test_abandoned_dispatch_reuses_the_facets_claim_rather_than_recomputing_it():
    """`74b0a063` retrieved early and then left a fresh dispatch unfetched at its end.

    The condition belongs to `signature.session_ended_on_unretrieved_dispatch` — including
    `_TAIL_TOLERANCE` and the in-session-retrieve precondition — so this asserts the two agree
    rather than asserting a copy of the rule.
    """
    run = _async_run("74b0a063")
    facet = next(f for f in shape_facets(run)
                 if f.name == "session_ended_on_unretrieved_dispatch")
    assert facet.value and facet.observed
    finding = _only(run, "abandoned_dispatch")
    assert finding.calls == 1
    assert finding.detail == "dispatch_tool"
    assert isinstance(finding.wall_ms, int) and finding.wall_ms > 0


def test_abandoned_dispatch_says_the_async_jobs_own_cost_is_invisible():
    """The charged duration is a FLOOR. The job runs behind the gateway and emits no span in this
    session, so a reader must not read the number as the total the abandonment cost."""
    finding = _only(_async_run("74b0a063"), "abandoned_dispatch")
    assert "FLOOR" in finding.confidence
    assert "invisible" in finding.confidence


def test_an_inline_dispatch_is_not_called_abandoned():
    """`36fafeb7` dispatched twice with `sync_mode: true` and got its images back INLINE, emitting
    no retrieve span because none was needed. Accusing it is the exact bug `88ee151` retired a
    facet for, and the precondition that prevents it lives in `signature`, not here."""
    assert _findings(_async_run("36fafeb7"), "abandoned_dispatch") == []


def test_a_clipped_harvest_yields_no_abandoned_dispatch_finding():
    """`Run.evidence_complete is False` makes the facet unobservable, because the row cap drops
    the NEWEST rows — precisely the tail where the missing retrieve would be. An unobserved facet
    must not be priced: that would charge a run for evidence the harvest lost."""
    run = _async_run("74b0a063")
    run.evidence_complete = False
    assert _findings(run, "abandoned_dispatch") == []
    # ...and the repetition signals are unaffected: a repeat we CAN see happened, whatever else
    # was clipped. Clipping can only make this path under-count.
    assert _findings(run) == []  # 74b0a063's only finding is the dispatch one


# --- error_then_retry -------------------------------------------------------
def test_error_then_retry_finds_a_failed_tool_call_the_same_tool_later_completed():
    """`23a1fa24` records seven ERROR `execute_tool code_interpreter` spans and goes on to run
    `code_interpreter` successfully. One finding per failed attempt, because two failures are two
    attempts paid for."""
    found = _findings(_async_run("23a1fa24"), "error_then_retry")
    assert len(found) == 7
    assert {f.detail for f in found} == {"code_interpreter"}
    assert all(f.calls == 1 for f in found)


def test_error_then_retry_ignores_aws_sdk_internal_retries():
    """The DF-8/DF-10 filter, and why it is the tool-span restriction rather than a name list.

    `signature.py` records that every ERROR span in the golden harvest is an `S3.GetObject` — a
    recovered internal retry, not a tool failure — and that `had_error` is ANTI-correlated with run
    failure (precision 0.276 against a 0.333 base rate). `ffab73f8` carries three of them and must
    produce nothing; `23a1fa24` carries fourteen `S3.HeadObject` and five
    `Bedrock AgentCore.InvokeCodeInterpreter` ERRORs alongside its seven real tool failures, and
    must count only the tool ones.
    """
    errors = [s.name for s in real_trajectory(FFAB73F8) if s.status == "ERROR"]
    assert errors and all(name == "S3.GetObject" for name in errors)
    assert _findings(_run(FFAB73F8), "error_then_retry") == []

    noisy = [s.name for s in async_lifecycle_trajectory("23a1fa24") if s.status == "ERROR"]
    assert sum(1 for n in noisy if not is_tool_span(n)) == 19
    assert len(_findings(_async_run("23a1fa24"), "error_then_retry")) == 7


# --- the assertion that this is not the failure path ------------------------
def test_waste_is_found_in_a_run_whose_production_verdict_is_PASS():
    """THE load-bearing test. `abc839ca`'s recorded label is PASS and it still repeated work.

    The verdict is recomputed by the production path (`verdict_from_recorded`) rather than read
    from a literal, so this cannot pass against a stale label. `abc839ca` dispatched
    `process_media` twice with the same shape — 2 calls, ~16 s — and mining will never look at it,
    because `mine` iterates failing runs only and REQ-018 would then find this shape TRUE in a
    passing run and route it to residue.
    """
    run = _run(ABC839CA)
    verdict = verdict_from_recorded(run, DEFAULT_PASS_THRESHOLD, DEFAULT_GATING)
    assert verdict.overall is Overall.PASS

    finding = _only(run, "repeated_sequence")
    assert finding.detail == "process_media -> dispatch_tool"
    assert finding.occurrences == 2
    assert finding.calls == 2
    assert isinstance(finding.wall_ms, int) and finding.wall_ms > 0


def test_find_waste_takes_no_verdicts():
    """Structural, not behavioural, and deliberately so.

    Efficiency is orthogonal to correctness. The moment this function can see a verdict, someone
    will filter on it, and the path becomes the failure path with extra steps.
    """
    import inspect

    parameters = list(inspect.signature(find_waste).parameters)
    assert parameters == ["runs"]


def test_no_discriminative_gate_is_applied():
    """A finding survives even when EVERY other run in the population shows the same thing.

    This is the property REQ-018 deliberately does not have. All four PASSING golden sessions
    repeat work; under a discriminative gate each would be a control against the others and every
    one of them would be suppressed.
    """
    passing = [ONEB42D744, ABC839CA, SIX309F154, FFAB73F8]
    for session in passing:
        assert verdict_from_recorded(
            _run(session), DEFAULT_PASS_THRESHOLD, DEFAULT_GATING).overall is Overall.PASS

    report = find_waste([_run(s) for s in passing])
    with_repeats = {f.run_id for f in report.findings if f.kind == "repeated_sequence"}
    assert with_repeats == set(passing)
    assert report.runs_examined == 4


# --- the shape of the report ------------------------------------------------
def test_every_kind_this_module_can_emit_is_declared():
    """`WASTE_KINDS` is what lets a consumer tell "this signal found nothing" from "this signal
    does not exist" (absent != empty). `duplicate_adjacent_call` is deliberately absent — see the
    module docstring for the population measurement that retired it."""
    report = find_waste([_run(s) for s in SCENARIOS])
    assert set(f.kind for f in report.findings) <= set(WASTE_KINDS)
    assert "duplicate_adjacent_call" not in WASTE_KINDS


def test_the_report_carries_the_denominator_and_names_what_it_did_not_scan():
    """`runsWithWaste: 12` means nothing without the population it was taken over, and a run the
    repeated-block search skipped must be COUNTED rather than read as clean (invariant 1)."""
    runs = [_run(s) for s in SCENARIOS]
    report = find_waste(runs)
    assert report.runs_examined == len(runs)
    assert report.runs_not_scanned == []

    import improve.waste as waste_module

    original = waste_module._MAX_CALLS_SCANNED
    try:
        waste_module._MAX_CALLS_SCANNED = 5
        report = find_waste(runs)
        assert set(report.runs_not_scanned) == {r.run_id for r in runs}
        assert [f for f in report.findings if f.kind == "repeated_sequence"] == []
    finally:
        waste_module._MAX_CALLS_SCANNED = original


def test_a_run_with_no_spans_produces_nothing_and_does_not_raise():
    """The trajectory is NO_DATA for a run whose spans were never captured. Nothing to observe is
    not the same as nothing wasted, and this path says nothing rather than guessing."""
    report = find_waste([Run(run_id="no-spans")])
    assert report.findings == []
    assert report.runs_examined == 1


def test_a_session_that_repeated_nothing_produces_nothing():
    """`e2cb300b` runs eleven distinct tool calls and repeats none of them. A signal that fired on
    every session would carry no information."""
    assert _findings(_run(E2CB300B)) == []


@pytest.mark.parametrize("session", [FFAB73F8, E2CB300B, SIX309F154, A3A8FAC0, ONEB42D744,
                                     ABC839CA])
def test_a_detail_string_names_only_this_runs_own_tools_and_multiplicities(session):
    """Span indices are harvest-dependent, so nothing user-visible may name one.

    Stated as a whitelist rather than "no digits": every step is a tool name from this run's own
    normalised sequence, optionally followed by ` xN`. That also catches a name leaking through
    unnormalised (an `execute_tool ` prefix or an MCP `target___tool` form).
    """
    run = _run(session)
    tools = set(tool_name_sequence(run.trajectory))
    for finding in _findings(run):
        for part in finding.detail.split(" -> "):
            name, _, multiplicity = part.partition(" x")
            assert name in tools, f"{part!r} is not a tool name of {session[:8]}"
            assert multiplicity == "" or multiplicity.isdigit()
