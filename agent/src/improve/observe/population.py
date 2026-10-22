"""Discover which sessions to mine, from the harvest rather than from the benchmark.

`golden.jsonl` and the mining population are two different things, and using one for both was a
real bug:

  golden.jsonl        a LABELLED BENCHMARK. Carries `expected` tier scores and
                      `label_provenance`; its job is to prove the scorer works and to calibrate
                      the threshold. Its STABILITY is the feature. If membership drifts,
                      "correct" drifts with it and a regression stops being visible.

  the population      every session with usable evidence. Its job is to find patterns, so it
                      must be dynamic and as large as retention allows.

Mining only the benchmark capped the population at a fixed handful of sessions no matter how
much was harvested. It also weakened the discriminative gate, which is only as strong as the
number of PASSING runs a pattern has to survive: a few controls is a plausibility check, many
of them are a test.

A discovered session carries no labels, so it has to be scored live. That is the cost of a
dynamic population and there is no way around it: the pass/fail split comes from the judge, and
without it there is nothing for `pass_hits` to measure against.
"""
from __future__ import annotations

import json
import os
from typing import Optional

try:
    from ..contract import Scenario
    from .span_trajectory import PLAN_SESSION_PREFIX, HarvestedSpanSource
except ImportError:  # pragma: no cover
    from improve.contract import Scenario  # type: ignore
    from improve.observe.span_trajectory import (  # type: ignore
        PLAN_SESSION_PREFIX, HarvestedSpanSource,
    )


# A session needs enough spans to be worth judging. One or two spans is a session that barely
# started; scoring it costs the same as a real one and tells you nothing.
MIN_SPANS = 5

# Every session is processed. There is deliberately no allow/deny list of session kinds: a
# hardcoded prefix filter decides in advance what is worth looking at, and the whole point of a
# discovered population is that the data decides.
#
# One hazard this leaves open, recorded rather than filtered: a `plan-session-*` id is a
# sub-session minted for a plan's own nodes, so mining it alongside its parent counts one user
# request more than once and can make sibling sub-sessions look like independent evidence for
# the same pattern. `session_kind` carries that so a reader (and, later, the miner) can weigh it
# instead of it being silently dropped here.

# Left EMPTY on purpose, so the evaluator choice is made by `improve.classify` from the run's
# own shape rather than declared here.
#
# This used to name all three evaluators, on the reasoning that a discovered session has not
# declared which dimensions apply so the full set is the safe default. That was wrong in the
# expensive direction: `goal_judge` was then run on invocations that had no user request at
# all (session warm-up, Lab operations, plan sub-sessions), and its honest "no conversation is
# available" answer was folded into the verdict as a FAIL. 30 of 35 FAILs in the 50-session
# harvest were that. Running a check whose dimension does not exist is not a safe default; it
# manufactures failures.
DISCOVERED_EVALUATORS: tuple = ()


def discover_sessions(sessions_path: Optional[str] = None,
                      min_spans: int = MIN_SPANS,
                      stats: Optional[dict] = None) -> list[str]:
    """Session ids worth scoring, newest first.

    Reads the MERGED harvest, through the same source the evidence layer uses. Reading a single
    file here while `HarvestedSpanSource` merged several made discovery silently blind to six
    sessions whose spans the rest of the pipeline could see perfectly well: the entire golden
    benchmark. Two components disagreeing about what exists is worse than either being wrong.

    Every session with enough spans is returned. Nothing is excluded by id shape.

    ``stats``, when given, is FILLED with what this read discarded — ``harvested`` (every session
    in the merged harvest), ``below_min_spans`` (those the span floor excluded) and
    ``discoverable`` (what is returned). An out-parameter rather than a wider return type because
    every caller wants the ids and only the reporting path wants the arithmetic, and because the
    count of sessions the floor dropped has to be MEASURED somewhere: without it the population
    that comes out of here is a sample of the harvest whose fraction nobody knows.
    """
    harvest = HarvestedSpanSource(sessions_path)._load()

    scored: list[tuple] = []
    below_min_spans = 0
    for session_id, record in harvest.items():
        spans = record.get("spans") or []
        if len(spans) < min_spans:
            below_min_spans += 1
            continue
        # Newest first, so a truncated run still mines the most recent sessions.
        first_ts = min((s.get("ts", "") for s in spans), default="")
        scored.append((first_ts, session_id))

    if stats is not None:
        stats.update({"harvested": len(harvest), "below_min_spans": below_min_spans,
                      "discoverable": len(scored), "min_spans": min_spans})
    return [session_id for _, session_id in sorted(scored, reverse=True)]


def harvest_population(sessions_path: Optional[str] = None, limit: Optional[int] = None,
                       min_spans: int = MIN_SPANS,
                       stats: Optional[dict] = None) -> list[Scenario]:
    """Unlabelled Scenarios for every minable session in the harvest.

    ``expected`` is deliberately EMPTY. A discovered session has no recorded labels, so it must
    be scored by the judge; leaving the field blank means `verdict_from_recorded` would produce
    NO_DATA tiers for it rather than inventing a pass, which is why the discovered population
    only makes sense in live mode.

    ``stats``, when given, is FILLED with `discover_sessions`' counts plus ``admitted`` (how many
    survived ``limit``) and ``limit`` itself. TWO bounds narrow this list and both used to be
    silent: the span floor above, and the cap below. Everything computed from the survivors — the
    pass rate, the spend, every history row — is then a figure over a sample, and a sample with no
    recorded size is indistinguishable from a whole population. ``admitted`` and ``discoverable``
    are what let a reader tell them apart.
    """
    stats = stats if stats is not None else {}
    session_ids = discover_sessions(sessions_path, min_spans=min_spans, stats=stats)
    if limit is not None:
        session_ids = session_ids[:limit]
    stats.update({"admitted": len(session_ids), "limit": limit})
    return [
        Scenario(
            scenario_id=session_id,
            goal="",                       # read from Memory at build time
            category="discovered",
            evaluators=list(DISCOVERED_EVALUATORS),
            claims={},
            expected={},                   # unlabelled: the judge supplies the verdict
            source_session=session_id,
            label_provenance="discovered from harvest; scored live",
        )
        for session_id in session_ids
    ]


def session_kind(session_id: str) -> str:
    """What kind of session this id names, recorded so it can be weighed rather than filtered.

    `plan_node` is the one that matters for honesty: it is a sub-session of a plan, so it shares
    a user request with its parent and must not be counted as independent evidence for a pattern.
    """
    if session_id.startswith(PLAN_SESSION_PREFIX):
        return "plan_node"
    if session_id.startswith("lab-"):
        return "lab_harness"
    if session_id.startswith("harness-catalog-"):
        return "catalog"
    return "user_run"
