"""Trajectory-shape-aware domain evaluators (code-based).

Separate from the legacy tests/evaluation/evaluators.py, which stays untouched.
These handle real multi-node DAG traces where dispatch/poll/retrieve tool names
recur across nodes — the legacy first-match/poll-between logic false-fails those.

Contract: an async lifecycle is honored iff every dispatch is eventually followed
by a retrieve; a DAG is complete iff every task node reached terminal-success.
Both read terminality, not error-count (a recovered error is not a failure), and
both ignore the unreliable PLAN parent-row status.

The async half of that contract is only half OBSERVABLE, and `async_job_flow` says so rather than
rounding it off: an unpaired dispatch may have returned its result inline, which spans cannot
record (D-02 / NS-5). `pair_dispatches` is the single implementation of the pairing rule and is
shared with `signature.shape_facets`; keep it that way, because two copies of it once answered
opposite ways about the same session.

`tool_discovery` and `control_plane_op` are the objectives for the invocation
classes that have no user request (see `classify.py`). They are here rather than in
a new module because they are code evaluators of the same kind: they read a run's
own shape and return the same result type.

can-show-RED is evidence-intrinsic: when a run's shape shows a dimension SHOULD
have evidence but it is absent (e.g. the trajectory dispatched async work but no
plan was captured), the result is NO_DATA (RED), not a "not applicable" pass.
NOT_APPLICABLE is reserved for runs that genuinely lack the dimension.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

try:
    from .contract import NO_DATA, EvaluatorResult, Outcome, Plan, parse_utc
except ImportError:
    from improve.contract import NO_DATA, EvaluatorResult, Outcome, Plan, parse_utc  # type: ignore


# Every code evaluator, in one place. `evaluate._code_evaluator` dispatches on these names and
# `signature.symptom_facets` emits a `check_failed:` facet for each, so two copies of the list
# would silently disagree: adding an evaluator without adding its facet means a failure that
# evaluator alone detects has NO symptom, and mining then cannot describe what went wrong.
# That is exactly what happened when `ControlPlaneOpValidator` and `ToolDiscoveryValidator`
# were added against a hardcoded two-name list.
CODE_EVALUATORS = (
    "DagCompletionValidator",
    "AsyncJobFlowValidator",
    "ToolDiscoveryValidator",
    "ControlPlaneOpValidator",
)


def _is_dispatch(name: str) -> bool:
    n = name.lower()
    return n == "dispatch_tool" or n.startswith("start_") or n == "plan_and_execute_tool"


def _is_retrieve(name: str) -> bool:
    n = name.lower()
    return "result" in n or n == "get_job" or n == "get_download_url"


# How many trailing spans still count as "the session ended here", counting the dispatch itself.
# MEASURED, not chosen, and it lives beside `_is_dispatch` / `_is_retrieve` because
# `pair_dispatches` is the one place the whole rule is implemented.
#
# THE EVIDENCE. Measured over a 336 h staging span harvest — 78,612 spans, verified against an
# uncapped `stats count()` over the same window, zero clipped windows, 541 sessions with >=5 spans,
# 163 of them dispatching. Across the 70 sessions whose last dispatch is unpaired, the span count
# AFTER that dispatch is
#
#     4, 9x35, 10x9, 18, 27, 29, 30, 32, 32, 35, 38, 59, 60, 61, 64, 66, 73, 82, 87,
#     105, 106, 108, 109, 112, 130, 141, 171, 200, 238, 249, 254
#
# plus 17 for `ffab73f8`, which comes from `tests/fixtures/plan_state_golden.json` and NOT from the
# harvest: it ran 2026-08-13, past the `aws/spans` 14-day retention, so that value cannot be
# re-derived from the log group and never will be again. It matters because the fixture, not the
# live data, sets the UPPER bound of the honest range.
#
# The cluster at 9-10 is one fixed close-out sequence: `CreateEvent, CreateEvent,
# execute_event_loop_cycle, chat, chat <model>, CreateEvent, execute_event_loop_cycle,
# invoke_agent Strands Agents, POST /invocations` — two memory writes, the loop cycle closing, the
# model's final summarising turn, then the two outermost request spans; 10 adds one memory/S3 span.
# There is no 11. Every session at 18 or above did further WORK after its dispatch (artifact
# copies, more S3 reads, further model turns), so it did not end there. The 4 is real and is not a
# floor to tune around: `52a4f062` dispatches at span 316 of 321 and its trace then stops MID
# close-out, with no outer `invoke_agent` / `POST /invocations` pair — a truncated close-out is
# still a session that ended on that dispatch.
#
# In THIS constant's units the test is `unpaired[-1] >= span_count - T`, one MORE than the counts
# above, so the cluster is 10-11 and the next values are 18 (fixture) and 19 (live). The honest
# range is T = 11..17: 11 is the tightest value covering the cluster, and 18 would accuse
# `ffab73f8`, which `test_a_dispatch_the_session_kept_working_past_is_not_called_the_session_end`
# forbids. The firing set is identical across all of 11..18 on live data — 23 of the 163
# dispatching sessions — so the choice is not knife-edge. 12 is not the tightest covering value; it
# carries three spans of slack over the 9-span close-out. Do not re-read it as tight.
#
# Wall TIME does not separate the cases (4.8 s, 7.1 s, 11.6 s on the three sessions with known
# ground truth) and dispatch DURATION was rejected as a discriminator (117 ms to 605,808 ms, no
# bimodality), so the span COUNT is what is used. That couples the constant to the runtime's own
# close-out instrumentation: if that sequence changes length, re-measure rather than nudge.
#
# Deliberately NOT env-overridable, unlike `signature._MAX_EVIDENCE_SPAN_IDS`. This is a measured
# semantic threshold — it decides what a facet claims and what this module's evaluator calls a
# failure — so the right response to it being wrong is a recorded re-measurement, not an ops tweak
# that silently redefines the claim.
_TAIL_TOLERANCE = 12


@dataclass(frozen=True)
class AsyncPairing:
    """Every dispatch in one trajectory, paired against a later retrieve.

    `async_job_flow` and `signature.shape_facets` both need "which dispatches were never
    collected, and did the session end on one of them". They must share this, not re-derive it:
    when each had its own copy they disagreed on the same session — one reported that it ended on
    an uncollected dispatch while the other passed it.

    Field vs property is deliberate. The four fields are what the span sequence RECORDS; the three
    properties are the CLAIMS derived from them, each named for the facet it feeds, so a reader
    grepping a facet name lands on the predicate that produces it.

    The three index fields hold positions in `tool_name_sequence(run.trajectory)`, i.e. over ALL
    spans and not just tool spans — that is what both callers pass. Indices are not stable across
    harvests (`trajectory_for_session` stable-sorts on the parsed timestamp, so spans sharing a
    millisecond keep harvest order), so never pin one in a test.
    """

    span_count: int
    dispatches: tuple[int, ...]
    retrieves: tuple[int, ...]
    #: Dispatches with no retrieve anywhere after them. A SUFFIX of `dispatches`, provably: if no
    #: retrieve exceeds `d`, none exceeds any later dispatch either.
    unpaired: tuple[int, ...]

    @property
    def retrieved_in_session(self) -> bool:
        """Did this session retrieve at all? The precondition that turns "unpaired" into a claim.

        Session-local proof that dispatches HERE hand back handles needing a fetch. Without it the
        claim fires on `36fafeb7`, whose two `dispatch_tool` calls each blocked ~49 s and returned
        their images INLINE (`sync_mode: true`, as its skill instructs). Load-bearing by
        measurement: over the 336 h harvest the tail test alone fires on 44 sessions and this
        precondition blocks 21 of them, so without it the claim would over-claim by 91%.
        """
        return bool(self.retrieves)

    @property
    def ended_on_unretrieved_dispatch(self) -> bool:
        """The CLAIM — `signature.session_ended_on_unretrieved_dispatch`, and the evaluator's FAIL.

        The session retrieved at least once, and yet its last dispatch was never followed by a
        retrieval and the session ended within `_TAIL_TOLERANCE` spans of it.
        """
        return (bool(self.unpaired)
                and self.unpaired[-1] >= self.span_count - _TAIL_TOLERANCE
                and self.retrieved_in_session)

    @property
    def unpaired_result_unobserved(self) -> bool:
        """The AMBIGUITY — `signature.dispatch_unpaired_result_unobserved`.

        An unpaired dispatch the claim above does not cover. Asserts nothing: the result may
        simply have been returned inline, and spans cannot tell the two apart (D-02 / NS-5 — tool
        arguments and results reach `aws/spans` only on `mcp tools/call` spans, and
        `dispatch_tool` is a harness builtin emitting `execute_tool`).
        """
        return bool(self.unpaired) and not self.ended_on_unretrieved_dispatch


def pair_dispatches(tool_names) -> AsyncPairing:
    """Pair every dispatch in `tool_names` against a later retrieve.

    EVERY dispatch, not just the first. Testing only the first makes a late unpaired dispatch after
    an early successful retrieve read as healthy — `74b0a063` is exactly that shape:
    get_job/get_job_result through the 179-233 region, then a fresh dispatch near the end that
    nothing ever fetched.
    """
    names = list(tool_names or [])
    dispatches = tuple(i for i, n in enumerate(names) if _is_dispatch(n))
    retrieves = tuple(i for i, n in enumerate(names) if _is_retrieve(n))
    unpaired = tuple(d for d in dispatches if not any(r > d for r in retrieves))
    return AsyncPairing(span_count=len(names), dispatches=dispatches, retrieves=retrieves,
                        unpaired=unpaired)


def async_job_flow(tool_names, trajectory_present: bool = True) -> EvaluatorResult:
    """Async lifecycle check: was every dispatched job's result collected?

    trajectory_present=False means the trajectory evidence itself is absent — a
    run we cannot judge for async flow → NO_DATA (RED), never a silent pass.

    The rule is `pair_dispatches`, shared with `signature.shape_facets` and not re-derived here.
    The trap it avoids: testing only whether a retrieve followed the FIRST dispatch answers "did
    this session use the async retrieve API at all", which is a different question from "did it
    lose a result", and on real data the two are not merely different but opposed. Measured over
    541 sessions of a 336 h harvest, that test FAILed 31 sessions which never retrieved anything
    (the inline-return shape, none of them lost work) and PASSed all 23 which ended on an
    uncollected dispatch (all of the real lost work) — precision 0/31, recall 0/23.

    Four outcomes over the pairing, three of which the evidence decides:

      no dispatches                          NOT_APPLICABLE — the dimension is absent
      every dispatch collected               PASS
      ended on an uncollected dispatch,      FAIL — work was dispatched and its result was
        having collected earlier ones               demonstrably never fetched
      unpaired, but not that                 see below — genuinely undecidable

    The fourth row is UNDECIDED, and choosing it is policy rather than measurement: an unpaired
    dispatch may simply have returned its result inline, and spans cannot distinguish that from a
    lost result (D-02). FAIL over-claims on evidence that establishes nothing; PASS lets absence
    read as a pass; NO_DATA fits the semantics but `signature.symptom_facets` computes
    `bad = outcome in (FAIL, NO_DATA)`, so mining sees "check failed" either way and the split
    between a claim and an ambiguity collapses.

    PASS is a PLACEHOLDER pending that decision. It is the only one of the three that ships no
    unestablished claim, and it does not silence the ambiguity:
    `signature.dispatch_unpaired_result_unobserved` is a separate `observed=True` facet reporting
    exactly this state. Note it is the largest of the four buckets — 47 of the 70 sessions carrying
    an unpaired dispatch — so it is this evaluator's dominant behaviour, not an edge case.
    """
    name = "AsyncJobFlowValidator"
    if not trajectory_present:
        return EvaluatorResult(name, "code", Outcome.NO_DATA, 0.0,
                               "Trajectory evidence absent — async flow unjudgeable.")
    pairing = pair_dispatches(tool_names)
    if not pairing.dispatches:
        # Fires on 0 of 541 sessions in the 336 h harvest, so it is dead rather than wrong today.
        # It does assume one session is the whole window, and a retrieve whose dispatch happened
        # in an EARLIER session is plausible here (plan sub-sessions and polling both do it) — but
        # nothing has measured that, so it stays until something does.
        if pairing.retrieves:
            return EvaluatorResult(name, "code", Outcome.FAIL, 0.0,
                                   "Results retrieved with no preceding dispatch.")
        return EvaluatorResult(name, "code", Outcome.NOT_APPLICABLE, 1.0,
                               "No async job dispatched — genuinely not applicable.")
    total = len(pairing.dispatches)
    if not pairing.unpaired:
        return EvaluatorResult(name, "code", Outcome.PASS, 1.0,
                               f"All {total} dispatch(es) were followed by a retrieval.")
    if pairing.ended_on_unretrieved_dispatch:
        return EvaluatorResult(
            name, "code", Outcome.FAIL, 0.0,
            f"{len(pairing.unpaired)} of {total} dispatch(es) were never followed by a "
            f"retrieval, and the session ended "
            f"{pairing.span_count - 1 - pairing.unpaired[-1]} span(s) after the last of them. "
            f"This session did retrieve {len(pairing.retrieves)} result(s) for earlier "
            "dispatches, so its dispatches do return handles that need fetching.")
    # PLACEHOLDER — see the docstring. This outcome is undecided, not measured.
    return EvaluatorResult(
        name, "code", Outcome.PASS, 1.0,
        f"{len(pairing.unpaired)} of {total} dispatch(es) have no later retrieval, and this "
        f"session retrieved {len(pairing.retrieves)} result(s) in total; the results may have "
        "been returned inline, which spans cannot distinguish from a lost result, so no failure "
        "is claimed.")


def dag_completion(plan, dispatched_async: bool = False) -> EvaluatorResult:
    """DAG node-terminality check.

    ``plan`` may be a Plan, None (no plan captured), or the NO_DATA sentinel.
    ``dispatched_async`` = the trajectory shows the run kicked off DAG/async work.
    If the run dispatched async work but the plan is absent, that is missing
    evidence → NO_DATA (RED). If the run did no async work, absent plan is
    genuinely NOT_APPLICABLE.
    """
    name = "DagCompletionValidator"
    plan_present = isinstance(plan, Plan) and bool(getattr(plan, "nodes", None))
    if not plan_present:
        if dispatched_async:
            return EvaluatorResult(name, "code", Outcome.NO_DATA, 0.0,
                                   "Run dispatched async/DAG work but no plan was captured — missing evidence.")
        return EvaluatorResult(name, "code", Outcome.NOT_APPLICABLE, 1.0,
                               "No DAG plan and no async dispatch — genuinely not applicable.")
    task_nodes = [n for n in plan.nodes if n.id != "PLAN"]
    if not task_nodes:
        return EvaluatorResult(name, "code", Outcome.NOT_APPLICABLE, 1.0, "No task nodes.")
    failed = [n.id for n in task_nodes if n.status == "failed"]
    if failed:
        return EvaluatorResult(name, "code", Outcome.FAIL, 0.0, f"Node(s) failed: {failed}")
    non_terminal = [n.id for n in task_nodes if n.status not in ("completed", "succeeded")]
    if non_terminal:
        return EvaluatorResult(name, "code", Outcome.FAIL, 0.0, f"Node(s) non-terminal: {non_terminal}")

    # Terminality is not sufficiency. A node can be marked complete BEFORE the
    # dependency whose output it consumes — see DF-11 / lifecycle-ffab73f8.md, where
    # all six nodes went terminal in 96s while the diarization t2 was told to read was
    # still running. Every other evaluator, and both LLM judges, scored that PASS.
    violations = _edge_order_violations(task_nodes)
    if violations:
        detail = "; ".join(f"{n} completed at {nt} BEFORE its dependency {d} at {dt}"
                           for n, nt, d, dt in violations)
        return EvaluatorResult(
            name, "code", Outcome.FAIL, 0.0,
            f"All nodes terminal but dependency ORDER violated ({len(violations)}): {detail}")

    return EvaluatorResult(name, "code", Outcome.PASS, 1.0, f"All {len(task_nodes)} task nodes complete.")


def tool_discovery(run) -> EvaluatorResult:
    """Did session warm-up resolve the gateway tool catalogs?

    The objective for a `tool_discovery` invocation. There is no user request in one of
    these, so the only thing that can go wrong is the discovery itself: a gateway that
    refuses the handshake, or an error on the way to it. Success here is genuinely a PASS,
    not a consolation for having no goal.

    What this deliberately does NOT judge is how MANY handshakes happened. The harvest shows
    a uniform 5 per warm-up invocation, which looks like redundant re-resolution until you
    notice the harness federates to several gateways and the spans come in
    `tools/list -> POST -> tools/list` pairs, one set per endpoint. From span names alone
    "redundant" and "one per gateway" are indistinguishable, so calling it redundant would
    be inventing a finding. The count and the elapsed time are reported so a reader can see
    them; neither decides the outcome.
    """
    name = "ToolDiscoveryValidator"
    if run.trajectory is NO_DATA or not run.trajectory:
        return EvaluatorResult(name, "code", Outcome.NO_DATA, None,
                               "No trajectory captured, so discovery is unjudgeable.")
    handshakes = [s for s in run.trajectory if getattr(s, "name", "") == "mcp tools/list"]
    if not handshakes:
        # Classified here on some other ground, so the dimension this evaluator measures is
        # absent rather than failed.
        return EvaluatorResult(name, "code", Outcome.NOT_APPLICABLE, None,
                               "No gateway tool-catalog handshake in this session.")
    # Only the HANDSHAKE spans decide. An earlier version failed the run on any ERROR span
    # anywhere in the trajectory, which is the signal this project has already measured as
    # noise: every ERROR span in the harvest is an S3 retry or an existence probe, and
    # `had_error` is ANTI-correlated with run failure (precision 0.276 against a 0.333 base
    # rate, DF-8/DF-10). `no_retry_after_failed_node` was deleted for exactly that reason, so
    # reintroducing an error-count gate here would have contradicted a measured finding.
    failed = [s for s in handshakes if getattr(s, "status", "") not in ("OK", "UNSET")]
    if failed:
        return EvaluatorResult(name, "code", Outcome.FAIL, 0.0,
                               f"{len(failed)} of {len(handshakes)} catalog handshake(s) "
                               "did not succeed.")
    elapsed = discovery_elapsed_ms(run)
    took = f", warm-up took {elapsed} ms" if elapsed is not None else ""
    return EvaluatorResult(name, "code", Outcome.PASS, 1.0,
                           f"All {len(handshakes)} gateway catalog handshake(s) succeeded"
                           f"{took}.")


def control_plane_op(run) -> EvaluatorResult:
    """Did the Lab / control-plane operations in this session succeed? Currently unanswerable.

    Always NO_DATA, and that is the finding rather than a placeholder. A control-plane
    invocation records no outcome anywhere we can read:

      * `POST /invocations`, the root span, is `UNSET` on all 268 occurrences in the harvest.
        The invocation never reports whether it succeeded.
      * Span `status` cannot substitute. All four ERROR spans in the control-plane sessions
        are `S3.HeadObject` 404s, which are a deliberate existence probe (`s3_user_files_tool`
        heads the canonical key and falls back on `ClientError`), so an error-count gate marks
        correct behaviour as a defect. This project already measured `had_error` as
        ANTI-correlated with run failure (precision 0.276 against a 0.333 base rate,
        DF-8/DF-10) and DELETED `no_retry_after_failed_node` over it.

    An earlier version of this function did gate on ERROR spans, and on the 50-session harvest
    it removed 30 fabricated failures and introduced 1. Reporting NO_DATA keeps the class in
    the population, keeps its evidence in the artifact, and states that the harness does not
    yet emit what would be needed to judge it: a recorded per-operation outcome. That is a
    run-record instrumentation gap, and it belongs in the defect register rather than being
    papered over with a verdict that the evidence does not support.
    """
    name = "ControlPlaneOpValidator"
    if run.trajectory is NO_DATA or not run.trajectory:
        return EvaluatorResult(name, "code", Outcome.NO_DATA, None,
                               "No trajectory captured, so the operations are unjudgeable.")
    errors = [s for s in run.trajectory if getattr(s, "status", "") == "ERROR"]
    detail = ""
    if errors:
        # Recorded, never scored: a reader needs to see these, and span status is known to be
        # non-discriminative, so they cannot decide the outcome.
        detail = (" Errors present but NOT treated as failure (span status is measured "
                  "non-discriminative here): "
                  + "; ".join(
                      f"{getattr(s, 'name', '?')}: "
                      f"{getattr(s, 'error_message', None) or 'no message'}"
                      for s in errors[:3]))
    return EvaluatorResult(
        name, "code", Outcome.NO_DATA, None,
        f"No operation outcome is recorded for this invocation ({len(run.trajectory)} spans, "
        "root POST /invocations status UNSET), so success cannot be established."
        + detail)


def discovery_elapsed_ms(run) -> Optional[int]:
    """Duration of the root invocation span, or None when it was not recorded.

    Reported as a utility term rather than gated. Warm-up latency is a real cost (1.8 to
    6.6 s across the harvest) but it is a trend to watch, and a threshold picked here would
    be a number nobody measured.
    """
    if run.trajectory is NO_DATA or not run.trajectory:
        return None
    roots = [s for s in run.trajectory
             if getattr(s, "name", "") == "POST /invocations"
             and getattr(s, "duration_ms", None) is not None]
    return max((s.duration_ms for s in roots), default=None)


def _parse_ts(raw) -> Optional[datetime]:
    """Parse a node's ``updated_at`` to a UTC-aware datetime, or None if unusable.

    The writer emits TWO formats — ``...+00:00`` from the chainer and a bare ``...Z`` on the
    failure path — and both appear in the same harvest. Comparing them as STRINGS is wrong:
    for the same instant ``'…19.5Z' > '…19.5+00:00'`` because ``'Z' > '+'``, which
    manufactures an ordering violation out of two identical timestamps. (No golden session
    exhibits that today — in this harvest ``Z`` appears only on failed leaf nodes, so no
    edge triggers it — but the formats are already mixed, so the next writer change would.)

    A third format is the real hazard: an offset-LESS timestamp parses to a NAIVE datetime,
    and comparing naive with aware raises ``TypeError``, which would propagate out of
    ``dag_completion`` → ``evaluate`` and kill the whole scoring run for that session. So a
    naive value is assumed UTC rather than returned as-is, and every parse failure — wrong
    type included, since ``updated_at`` is typed ``str`` but sourced from JSON and DynamoDB
    where an epoch number is plausible — degrades to None, which yields no verdict.

    The rule now lives in ``contract.parse_utc`` and this delegates to it, because the
    ``Run.window`` bounds need the identical rule and two copies of "one documented format"
    is how the formats got mixed in the first place. Behaviour is unchanged.
    """
    return parse_utc(raw)


def _edge_order_violations(task_nodes) -> list[tuple]:
    """Edges where the dependent's last status write PRECEDES its dependency's.

    Returns [(node_id, node_ts, dep_id, dep_ts)] with the ORIGINAL strings, so the
    explanation quotes what was recorded. Only compares nodes whose timestamps both
    parse — an absent or malformed one yields no verdict rather than a false
    accusation (can-show-RED applies here too: silence is not evidence).
    """
    raw = {n.id: (n.updated_at or "") for n in task_nodes}
    ts = {k: _parse_ts(v) for k, v in raw.items()}
    out = []
    for n in task_nodes:
        own = ts.get(n.id)
        if own is None:
            continue
        for dep in (n.depends_on or []):
            dep_ts = ts.get(dep)
            if dep_ts is not None and own < dep_ts:
                out.append((n.id, raw[n.id], dep, raw[dep]))
    return out
