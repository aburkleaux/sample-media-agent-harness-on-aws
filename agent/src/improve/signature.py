"""Turn a (Run, Verdict) pair into facets — the raw material for mining.

A facet is one boolean observation with its evidence attached. Facets are split by
``FacetKind``: SHAPE describes the run's observable structure and is the only thing
allowed to predict; SYMPTOM records what went wrong and is what gets predicted. Keeping
them apart is what stops the REQ-018 discriminative gate from being trivially satisfied
(see ``FacetKind``'s docstring).

No facet parses the goal text. An earlier draft counted "deliverables" out of the request
prose, and a lexical heuristic in the discriminative core would have made the mining result
an artifact of the tokenizer.

That is not the same as "every facet is a direct measurement", which would be false. The
three async-lifecycle facets classify SPAN NAMES through ``_is_dispatch`` / ``_is_retrieve``
(``evaluators_improve``), and those are lexical: ``_is_retrieve`` is an unanchored
``"result" in name`` substring test and ``_is_dispatch`` guesses on a ``start_`` prefix —
the same class of bug that mis-attributed a DAG failure to a container build because
``"depend"`` occurs inside ``"independently"``. Those three are listed in
``HEURISTIC_FACETS`` so any signature resting on one is marked, rather than the set being
empty and the whole marking mechanism inert.

Two further disciplines:

* **Observed vs false.** A facet whose evidence is absent carries ``observed=False``. A
  plan that was never captured must not yield ``flat_plan=True`` — "no edges seen" is not
  "no edges exist" — and a passing run in that state must not be counted as a control a
  signature successfully excluded. Absent includes PARTIAL: a trajectory the harvest is known to
  have clipped (``Run.evidence_complete is False``) makes every ``_NEEDS_TRAJECTORY`` facet
  unobservable, because a span-order claim over a partial span list is not a weaker measurement,
  it is a different one.
* **Evidence tracks value.** Each ``evidence`` string is derived from the same predicate as
  its ``value``, so a False facet can never carry prose asserting the thing happened. Two
  facets previously did exactly that.

Retired rather than fixed: ``dispatch_without_retrieve``. It read "was there a retrieve after
the FIRST dispatch", and measurement on real sessions killed it on two counts. It fired on
``36fafeb7`` (FAIL) and ``23a1fa24`` (PASS) alike — both dispatched with ``sync_mode: true``
and got their results back INLINE, emitting no retrieve span because none was needed — so it
split runs by *"did you use the async retrieve API?"*, not by *"did you leak a result?"* And by
reading only ``dispatches[0]`` it scored ``74b0a063`` False, although that run retrieved early
and then left a fresh dispatch unfetched at its end. Registry record NS-5 settles why the
obvious fix is unavailable: tool arguments and results reach ``aws/spans`` only on
``mcp tools/call`` spans, and ``dispatch_tool`` is a harness builtin, so it emits
``execute_tool dispatch_tool`` and carries neither. "The result came back inline" is therefore
NOT observable (D-02), and the replacement facets are named for what span name, order, status
and duration can actually establish: ``session_ended_on_unretrieved_dispatch`` is a claim, and
``dispatch_unpaired_result_unobserved`` records the ambiguity instead of resolving it.

Removed rather than fixed: ``no_retry_after_failed_node``. It read "was there a dispatch
after the last ERROR span", and measurement killed it on three counts. Every ERROR span in
the golden harvest is an ``S3.GetObject`` — a recovered internal retry, not a tool failure —
so the signal is noise with respect to node failure; this project has already measured that
``had_error`` is ANTI-correlated with run failure (precision 0.276 against a 0.333 base rate,
DF-8/DF-10); and spans carry no per-node attribution, so "retried THIS node" is not
answerable from the evidence we have. A facet that cannot be computed from available
evidence should not exist, rather than exist and be quietly wrong.
"""
from __future__ import annotations

import os
from typing import Optional

try:
    from .contract import (
        NO_DATA, Facet, FacetKind, Outcome, Plan, Run, Verdict, pluralise, tool_name_sequence,
    )
    from .evaluators_improve import (
        CODE_EVALUATORS, _edge_order_violations, pair_dispatches,
    )
except ImportError:  # pragma: no cover
    from improve.contract import (  # type: ignore
        NO_DATA, Facet, FacetKind, Outcome, Plan, Run, Verdict, pluralise, tool_name_sequence,
    )
    from improve.evaluators_improve import (  # type: ignore
        CODE_EVALUATORS, _edge_order_violations, pair_dispatches,
    )


TERMINAL_OK = ("completed", "succeeded")

# Facets built on lexical span-name classification rather than on a recorded field. A
# signature resting on one of these is marked `heuristic_facets` so a reviewer can discount
# it. Keeping this accurate is the whole point — an empty set made the marking inert.
HEURISTIC_FACETS: frozenset = frozenset({
    "async_dispatched",
    "dispatch_unpaired_result_unobserved",
    "session_ended_on_unretrieved_dispatch",
})

# Facets requiring a captured plan, and facets requiring captured spans. Used to decide
# `observed`, and by `mine` to tell "excluded" from "could not judge".
_NEEDS_PLAN: frozenset = frozenset({
    "multi_node_plan", "wide_plan", "has_failed_node", "has_non_terminal_node",
    "all_task_nodes_terminal", "chained_plan", "flat_plan", "has_fanout_node",
    "deep_plan", "edge_order_violation", "fanout_parent_ran_early",
    "gateway_reported_failure", "terminal_node_without_gateway_status",
})
# Every facet here is unobservable on a run whose harvest was CLIPPED, not just on one with no
# spans — `shape_facets` folds `Run.evidence_complete` into the same `spans_seen` flag. That is
# correct for all three: each reads span ORDER, and on a partial read neither their True nor their
# False can be trusted. `session_ended_on_unretrieved_dispatch` in particular flips on whether a
# retrieve exists LATER in the sequence, which is precisely the tail the row cap drops.
_NEEDS_TRAJECTORY: frozenset = frozenset({
    "async_dispatched", "dispatch_unpaired_result_unobserved",
    "session_ended_on_unretrieved_dispatch",
})

# The dispatch-pairing rule, including the tail threshold `_TAIL_TOLERANCE` and its measurement,
# lives in `evaluators_improve` as `pair_dispatches` — `async_job_flow` needs the identical rule
# and this module imports from that one, not the reverse. Read the constant's evidence there.

# How many unpaired-dispatch span indices the evidence string names before truncating with
# "+N more". Bounded because this rides to the SNAPSHOT, which is ONE DynamoDB item
# (`aws_lambda._ITEM_BUDGET_BYTES` = 350_000, shared with every suggestion), and a session with
# hundreds of unpaired dispatches would otherwise put an unbounded list into a fixed budget.
#
# An OPERATIONAL LIMIT, not a threshold: it changes how much we PRINT, never what the facet
# claims, so it is env-overridable in the `_ALARM_PAGE_CAP` idiom for the day an item comes back
# too large. Deliberately unrelated to `evaluators_improve._TAIL_TOLERANCE`, which happens to
# share its value today — re-measuring that one must NOT drag this one along. They now live in
# different modules, which helps, but the coincidence of the literal is the trap, not the
# proximity.
_MAX_EVIDENCE_SPAN_IDS = int(os.environ.get("IMPROVE_MAX_EVIDENCE_SPAN_IDS", "12") or "12")


def _task_nodes(run: Run) -> list:
    if not isinstance(run.plan, Plan):
        return []
    return [n for n in run.plan.nodes if n.id != "PLAN"]


def _tier(verdict: Verdict, name: str):
    return next((r for r in verdict.evaluator_results if r.name == name), None)


def _edges_observed(run: Run) -> bool:
    """Whether this plan's dependency edges were actually readable.

    A plan whose node list was offloaded to S3 and not retrieved comes back EDGELESS, which
    is indistinguishable from a genuinely flat plan unless the reader says so. `HarvestedPlanSource`
    records that with `edges_available=False`; absent the attribute we assume the edges are
    real, which is correct for every source that does not offload.
    """
    return bool(getattr(run.plan, "edges_available", True))


def shape_facets(run: Run) -> list[Facet]:
    """Structural observations about how the run was decomposed and executed."""
    nodes = _task_nodes(run)
    names = tool_name_sequence(run.trajectory)
    plan_seen = bool(nodes)
    # TWO facts, deliberately not one. `spans_present` is "were any spans captured", which is what
    # `trajectory_absent` reports. `spans_seen` is "is the captured trajectory USABLE as evidence",
    # which is what decides `observed` for every `_NEEDS_TRAJECTORY` facet — and a trajectory the
    # harvest is KNOWN to have clipped is not.
    #
    # Collapsing them would make a clipped run report `trajectory_absent=True`, asserting we saw no
    # spans when we saw most of them; keeping only the first would let a run whose retrieves were
    # dropped report `session_ended_on_unretrieved_dispatch=False, observed=True` and count as a
    # control a signature survived. That is the measured defect: `74b0a063`'s dispatch was harvested
    # and its three retrieves were not, so a healthy run entered the control pool looking like
    # "dispatched, never retrieved" and blocked REQ-018 for four real failures.
    #
    # `is not False`, never truthiness: UNKNOWN is a `_Sentinel` whose `__bool__` is False
    # (`contract.py`), and "no harvester recorded either way" must not read as "known incomplete" —
    # that would mark every harvest written before this field existed unobservable, including the
    # whole golden set.
    spans_present = bool(names)
    spans_seen = spans_present and run.evidence_complete is not False
    edges_seen = plan_seen and _edges_observed(run)

    facets: list[Facet] = []

    def add(name: str, value: bool, evidence: str = "", observed: Optional[bool] = None) -> None:
        if observed is None:
            observed = True
            if name in _NEEDS_PLAN:
                observed = plan_seen
            if name in _NEEDS_TRAJECTORY:
                observed = spans_seen
        facets.append(Facet(name, FacetKind.SHAPE, bool(value), evidence, observed))

    # --- evidence presence. Always observed: these ARE the observation. ---
    add("plan_absent", not plan_seen,
        "no task nodes captured for this session" if not plan_seen else "", observed=True)
    # NO spans at all — not "spans were clipped", which is `Run.evidence_complete` and is
    # deliberately kept out of this facet. Two different facts about the evidence, two different
    # remedies (the collector vs the harvest window), so they must not collapse into one name.
    add("trajectory_absent", not spans_present,
        "no spans captured for this session" if not spans_present else "", observed=True)
    # An edgeless-because-unreadable plan is its own defect, and must not be mined as a
    # flat plan. Reported so it can be routed to the defect register.
    add("plan_edges_unreadable", plan_seen and not edges_seen,
        "node list was offloaded and could not be read, so this plan's dependency edges "
        "are unknown" if plan_seen and not edges_seen else "", observed=True)

    # --- plan size ---
    add("multi_node_plan", len(nodes) > 1,
        f"{len(nodes)} task nodes" if len(nodes) > 1 else "")
    add("wide_plan", len(nodes) >= 4, f"{len(nodes)} task nodes" if len(nodes) >= 4 else "")

    # --- terminality ---
    failed = [n.id for n in nodes if n.status == "failed"]
    non_terminal = [n.id for n in nodes if n.status not in TERMINAL_OK and n.status != "failed"]
    all_terminal = plan_seen and not failed and not non_terminal
    add("has_failed_node", bool(failed), f"failed: {sorted(failed)}" if failed else "")
    add("has_non_terminal_node", bool(non_terminal),
        f"non-terminal: {sorted(non_terminal)}" if non_terminal else "")
    add("all_task_nodes_terminal", all_terminal,
        f"all {len(nodes)} task nodes reached terminal success" if all_terminal else "")

    # --- edge topology. Unobservable when the edge list could not be read. ---
    edges = {n.id: list(n.depends_on or []) for n in nodes}
    has_edges = any(edges.values())
    dependents: dict = {}
    for node_id, deps in edges.items():
        for dep in deps:
            # Only count a dependency that is itself a node in this plan. A dangling id
            # referenced by two nodes would otherwise create a phantom fan-out.
            if dep in edges:
                dependents.setdefault(dep, []).append(node_id)
    fanout = {dep: kids for dep, kids in dependents.items() if len(kids) > 1}

    add("chained_plan", has_edges, "at least one dependency edge" if has_edges else "",
        observed=edges_seen)
    add("flat_plan", plan_seen and not has_edges,
        "every node independent" if plan_seen and not has_edges else "", observed=edges_seen)
    # A fan-out node's output is consumed by two or more siblings — the topology where a
    # premature completion does the most damage, because every sibling reads the same
    # unfinished upstream artifact.
    add("has_fanout_node", bool(fanout),
        "; ".join(f"{dep} -> {sorted(kids)}" for dep, kids in sorted(fanout.items()))
        if fanout else "", observed=edges_seen)
    depth = _longest_chain(edges)
    add("deep_plan", depth >= 3, f"longest dependency chain = {depth}" if depth >= 3 else "",
        observed=edges_seen)

    # --- ordering (DF-11) ---
    violations = _edge_order_violations(nodes)
    add("edge_order_violation", bool(violations),
        "; ".join(f"{n} completed at {nt} BEFORE dependency {d} at {dt}"
                  for n, nt, d, dt in violations) if violations else "",
        observed=edges_seen)
    # The damaging special case, and what separates a costly ordering violation from a
    # harmless one: the node that ran early is ITSELF feeding two or more later steps, so
    # its unfinished output propagates to all of them. Measured on the golden set this is
    # what distinguishes e2cb300b (early node has 2 dependents; delivery failed) from
    # ffab73f8 and 6309f154 (1 and 0 dependents; both passed) — plain
    # `edge_order_violation` fires on all three.
    early_fanout = [v for v in violations if len(dependents.get(v[0], [])) > 1]
    add("fanout_parent_ran_early", bool(early_fanout),
        "; ".join(f"{n} was recorded finished at {nt} before its dependency {d} ({dt}), "
                  f"and {len(dependents[n])} later steps consume {n}: {sorted(dependents[n])}"
                  for n, nt, d, dt in early_fanout) if early_fanout else "",
        observed=edges_seen)

    # --- async lifecycle (lexical — see HEURISTIC_FACETS; D-02: spans record no sync/async
    # field for `dispatch_tool`, so "the result was returned inline" is NOT observable) ---
    #
    # The rule, the tail threshold and the in-session-retrieve precondition are ALL
    # `evaluators_improve.pair_dispatches`, not re-derived here — `async_job_flow` decides the same
    # four cases from the same three predicates, and two copies of that rule once answered opposite
    # ways about the same session. Read `pair_dispatches` and `AsyncPairing` for the measurement
    # behind each predicate; the notes below are only what is specific to the FACETS.
    #
    # Span indices are HARVEST-DEPENDENT (`trajectory_for_session` stable-sorts on the parsed
    # timestamp, so spans sharing a millisecond keep harvest order and adjacent indices can swap),
    # yet the evidence strings below print them, because a reader needs somewhere to look. Read
    # them as a REGION, never as a claim, and never pin one in a test: `74b0a063`'s retrieves sit
    # through the 179-233 region, and 233 in one harvest of it is `execute_event_loop_cycle`.
    pairing = pair_dispatches(names)
    dispatches, retrieves, unpaired = (pairing.dispatches, pairing.retrieves, pairing.unpaired)
    add("async_dispatched", bool(dispatches),
        f"{len(dispatches)} dispatch {pluralise(len(dispatches), 'call')}" if dispatches else "")

    leaked_at_end = pairing.ended_on_unretrieved_dispatch
    # Bound here rather than inline so the figure and the noun after it cannot be computed from
    # two different expressions. `unpaired` is non-empty whenever `leaked_at_end` holds; the
    # guard is what makes this safe to evaluate on the runs where it does not.
    spans_after = (len(names) - 1 - unpaired[-1]) if unpaired else 0
    add("session_ended_on_unretrieved_dispatch", leaked_at_end,
        f"this session retrieved {len(retrieves)} {pluralise(len(retrieves), 'result')} for "
        f"earlier dispatches, so its dispatches do return handles that need fetching — yet its "
        f"last dispatch (span {unpaired[-1]} of {len(names)}) was never followed by a retrieval "
        f"and the session ended {spans_after} {pluralise(spans_after, 'span')} later"
        if leaked_at_end else "")
    # The residue: every unpaired dispatch the facet above does not claim. Deliberately does
    # NOT accuse — an unpaired dispatch may simply have returned its result inline, and spans
    # cannot tell the two apart (D-02). Reported rather than dropped so the ambiguity is
    # visible instead of being resolved in the accusatory direction.
    #
    # `_result_unobserved` in the NAME is about the dispatched RESULT's fate, which is a
    # different axis from `Facet.observed`, which is about whether the evidence for the facet
    # was present at all. This facet is normally `observed=True` (spans were captured) while
    # its value says the result's fate was not among the things those spans recorded. It was
    # called `dispatch_unpaired_mid_session` until the `bool(retrieves)` precondition landed
    # above and made position stop being the discriminator: 36fafeb7's last unpaired dispatch
    # is span 167 of 177 and 23a1fa24's is 1263 of 1296, so both sessions ENDED on one, and the
    # old name asserted the opposite. Facet names travel — `bestShape`, `residue[].bestShape`,
    # `equally_discriminative`, `_FACET_PRECEDENCE` — so a positional claim in the identifier
    # is read as evidence even where the prose is careful.
    unclaimed = pairing.unpaired_result_unobserved
    # Span indices are bounded before they go in the evidence string — see
    # `_MAX_EVIDENCE_SPAN_IDS`. The COUNT is always exact; only the index list truncates, so a
    # reader never has to infer how many there were from how many were printed.
    hidden = max(0, len(unpaired) - _MAX_EVIDENCE_SPAN_IDS)
    shown = ", ".join(str(i) for i in unpaired[:_MAX_EVIDENCE_SPAN_IDS])
    # Three words in this sentence agree with two DIFFERENT counts, which is why each is derived
    # rather than written: `dispatch(es)` belongs to the denominator (`1 of 3 dispatches`), while
    # `span(s)` and the verb belong to the numerator — the unpaired ones are what is listed and
    # what has no retrieval. A single unpaired dispatch is the ordinary case on this facet.
    add("dispatch_unpaired_result_unobserved", unclaimed,
        f"{len(unpaired)} of {len(dispatches)} "
        f"{pluralise(len(dispatches), 'dispatch', 'dispatches')}, at "
        f"{pluralise(len(unpaired), 'span')} [{shown}"
        f"{f', +{hidden} more' if hidden else ''}], "
        f"{pluralise(len(unpaired), 'has', 'have')} no later "
        f"retrieval, and this session retrieved {len(retrieves)} "
        f"{pluralise(len(retrieves), 'result')} in total; the results "
        f"may have been returned inline, which spans cannot distinguish from a leak"
        if unclaimed else "")
    # `dispatch_without_retrieve` is RETIRED — it fired on any run that did not use the async
    # retrieve API, which is a different question from whether a result was leaked (F-02).
    # --- gateway (async job) outcome, distinct from the node's own status row ---
    gw_failed = [n.id for n in nodes if (n.gateway_status or "").upper() == "FAILED"]
    add("gateway_reported_failure", bool(gw_failed),
        f"gateway reported FAILED for {sorted(gw_failed)}" if gw_failed else "")
    # A node marked complete while carrying no gateway outcome at all — the row that
    # `chainer-settle-rule` cannot represent as partial success.
    silent = [n.id for n in nodes if n.status in TERMINAL_OK and not (n.gateway_status or "")]
    add("terminal_node_without_gateway_status", bool(silent),
        f"terminal but no gateway outcome recorded: {sorted(silent)}" if silent else "")

    return facets


def symptom_facets(verdict: Verdict) -> list[Facet]:
    """What went wrong — the target of prediction, never a predictor.

    A tier that never ran is ``observed=False``: "not scored" is not "no symptom".

    The check list is ``CODE_EVALUATORS``, not a literal, and the FIXED whole set rather than
    the subset this verdict happened to run. Both details matter. Deriving the names
    per-verdict would give different runs different facet keys, breaking the "every facet is
    emitted for every run" invariant the greedy refinement depends on; hardcoding a subset is
    how a ``control_plane`` failure came to have no symptom facet at all, which then made it
    look like an evidence gap instead of the S3 error it was.
    """
    facets: list[Facet] = []
    for tier in ("dispatch_correct", "results_delivered"):
        result = _tier(verdict, tier)
        bad = result is not None and result.outcome in (Outcome.FAIL, Outcome.NO_DATA)
        facets.append(Facet(
            f"tier_failed:{tier}", FacetKind.SYMPTOM, bad,
            f"{tier}={result.score} ({result.outcome.value})" if result else "",
            observed=result is not None,
        ))
    for check in CODE_EVALUATORS:
        result = _tier(verdict, check)
        bad = result is not None and result.outcome in (Outcome.FAIL, Outcome.NO_DATA)
        facets.append(Facet(
            f"check_failed:{check}", FacetKind.SYMPTOM, bad,
            result.explanation if result else "",
            observed=result is not None,
        ))
    return facets


def facets_for(run: Run, verdict: Verdict) -> dict:
    """{facet_name: Facet} for one run — both kinds, keyed for set operations.

    Every facet is emitted for every run, present-and-False rather than absent, so the
    population is directly comparable and no consumer has to treat a missing key as False.
    """
    return {f.name: f for f in (*shape_facets(run), *symptom_facets(verdict))}


def _longest_chain(edges: dict) -> int:
    """Longest dependency path length in nodes. Cycle-safe: a node already on the
    current path is not re-entered, so a malformed plan returns a finite depth instead
    of recursing forever."""
    memo: dict = {}

    def depth(node: str, seen: frozenset) -> int:
        if node in seen:
            return 0
        if node in memo:
            return memo[node]
        deps = edges.get(node) or []
        best = 1 + max((depth(d, seen | {node}) for d in deps if d in edges), default=0)
        memo[node] = best
        return best

    return max((depth(n, frozenset()) for n in edges), default=0)

