"""Quantify DISCARDED work across every run — passing runs included.

This is the efficiency path, and it is deliberately a sibling of ``mine`` rather than a facet
inside it. Two independent reasons, and both are load-bearing.

**1. No discriminative gate, because there is no causal claim.** ``mine`` tests *"this shape
explains why the run FAILED"*, which is a causal claim, so REQ-018 makes it earn controls
(``mine.py``'s module docstring is emphatic about why). A waste finding claims nothing about an
outcome: it says *"this run performed work whose output was discarded, costing N calls and T
ms"* — arithmetic over observed evidence. There is nothing to discriminate against because
nothing is being predicted, so controls would be a category error. Worse, they would suppress
every true finding: waste does not correlate with pass/fail, so any waste-derived facet fed to
``mine`` lands ``pass_hits > 0`` and dies as residue. The anchor is measured, not hypothetical —
``74b0a063`` PASSED and ended on a dispatch whose result was never retrieved, and ``23a1fa24``
dispatched 11 times with zero retrieves and PASSED. Nothing in the failure-attribution path will
ever look at either.

Consequences, so nobody has to re-derive them: no controls, no ``MIN_CONTROLS``, no
``pass_hits``, no residue bucket, no attribution, and **no judge** — every number here comes
from spans, so this path spends no Bedrock money, needs no verdict, and is not bounded by
``IMPROVE_LIMIT``. ``find_waste`` takes ``runs``, never ``verdicts``.

**2. The honesty guard is different from mining's.** Mining's is "do not claim causation without
controls". This module's is: **do not claim work was WASTED when the evidence only shows it was
REPEATED.** Repetition is not waste if the second run had different inputs, or produced a
different output that was needed — and tool ARGUMENTS do not reach ``aws/spans`` for harness
builtins at all (NS-5: only ``mcp tools/call`` spans carry
``attributes.gen_ai.tool.call.arguments``, and those are excluded from the fixtures under
OURS-4). So every finding states the observation, charges a cost, and names in ``confidence``
what it cannot establish. Naming follows ``88ee151``'s pattern —
``session_ended_on_unretrieved_dispatch`` (a claim) versus ``dispatch_unpaired_result_unobserved``
(the ambiguity, reported rather than resolved in the accusatory direction).

--------------------------------------------------------------------------------------------
THE UNIT OF WORK: one logical call, not one span. Measured, and it changes every number.
--------------------------------------------------------------------------------------------

A single tool call reaches ``aws/spans`` up to THREE times, under three different names that all
normalise to the same tool (``contract.tool_name_sequence`` strips ``execute_tool ``,
``mcp tools/call `` and everything before the last ``___``). Real, from the 336 h harvest:

    mcp tools/call aws___search_documentation                                 3,447 ms
    mcp tools/call mediaworks-harness-aws-mcp___aws___search_documentation   12,701 ms
    execute_tool   mediaworks-harness-aws-mcp___aws___search_documentation   12,706 ms

— the upstream MCP server's own span, the gateway's, and Strands', for ONE call. Counting spans
would report two thirds of every gateway call as "duplicate work", and the golden fixtures show
the same doubling (``abc839ca``: ``mcp tools/call media-processing___process_media`` immediately
followed by ``execute_tool media-processing___process_media``). Measured population-wide: 2,257
of 6,474 tool spans are the ``mcp`` layer and 1,677 of those overlap a same-named ``execute_tool``
span in wall-clock time.

So spans are collapsed into LOGICAL CALLS first: a maximal run of consecutive tool spans that
share a normalised name and whose RAW names are all distinct is one call. The rule is
deliberately name-based rather than timestamp-based: the committed fixtures
(``improve_fixtures.real_trajectory``) carry ``name``, ``status`` and ``duration_ms`` but **no**
``ts``, so a wall-clock-overlap rule would be untestable on the only data REQ-017 permits, and the
tested behaviour would not be the deployed behaviour.

What that rule does NOT collapse, said out loud: N consecutive calls of the same tool with
IDENTICAL raw names stay N calls. That is right when they are N different files (``98c1afc0``
fetches seven ``get_download_url`` inside 700 ms) and wrong for the residual same-raw-name
double-recording the harvest also contains — 241 adjacent same-raw-name call pairs begin within
5 ms of each other, ~4% of the 6,474 tool spans. Both directions were considered; this one
over-counts CALLS in a case the repetition signal excludes anyway (a single repeated name is one
STEP — see ``_MIN_REPEAT_LEN``), while a wall-clock-overlap rule would under-count a genuine
parallel batch of six generations as one call. Neither is free; this one errs where it is caught.

The collapse could be made EXACT rather than heuristic, and the evidence for that was checked
rather than assumed: the ``aws/spans`` records carry ``spanId`` and ``parentSpanId``, and on live
staging telemetry an ``mcp tools/call X`` span's ``parentSpanId`` IS the ``spanId`` of the
``execute_tool X`` span beside it (verified 2026-08-29, read-only, three consecutive pairs in one
session). ``harvest_live.harvest_spans`` does not request those two fields, so they are not in the
harvest this path reads, and adding them is a harvest change with a data-volume cost — out of scope
here, recorded so the next person does not re-derive it. Until then the raw-name rule is what a
committed fixture can test.

Only TOOL spans participate. The other ~92% of the trajectory is the runtime's own
instrumentation — ``chat``, ``execute_event_loop_cycle``, ``CreateEvent``, ``POST /invocations``,
``S3.GetObject`` — and it repeats by construction: ``evaluators_improve._TAIL_TOLERANCE``'s
measurement records the fixed nine-span close-out sequence every session ends with. Counting that
as repeated work would call the event loop wasteful.

--------------------------------------------------------------------------------------------
WHAT SHIPS, AND WHAT WAS MEASURED AND DROPPED
--------------------------------------------------------------------------------------------

``repeated_sequence``, ``abandoned_dispatch`` and ``error_then_retry`` ship.
``duplicate_adjacent_call`` — "the same tool twice in a row" — does NOT, and it is named here
because it reads as the most obvious of the four. It looks unambiguous; measured, it is the
opposite. Over the 549 sessions of the complete 336 h harvest, 140 carry at least one adjacent
duplicate LOGICAL call (i.e. after the layer collapse above), and
the runs of them are led by ``code_interpreter`` (103 — a REPL, where consecutive calls run
different code), ``get_download_url``/``get_download_urls`` (94 — different files),
``run_model`` (79 — largely concurrent generation of different assets),
``get_job_status``/``check_job``/``get_job`` (76 — polling an async job, i.e. WAITING, not redoing)
and ``skills`` (27). The dominant groups are not discarded work, and span names cannot separate
them from the case that is.
The same measurement is what justifies ``_MIN_REPEAT_LEN`` below, so one rule covers both: a run of
a SINGLE repeated tool name is not evidence of redone work. Three honest signals beat four with one
lying, and the honest content ``duplicate_adjacent_call`` would have had — a failed call repeated —
is exactly what ``error_then_retry`` reports, with the ERROR status as the evidence that the first
attempt produced nothing.

``error_then_retry`` DOES ship, and the DF-8/DF-10 filter it needs is the tool-span restriction
this module already applies for the reason above. ``signature.py`` records that *every* ERROR span
in the golden harvest is an ``S3.GetObject`` — a recovered internal retry — and that ``had_error``
is ANTI-correlated with run failure (precision 0.276 against a 0.333 base rate). That measurement
holds and is the reason the signal is restricted, not the reason to drop it: over the complete
population 775 spans carry ERROR and 300 of them are tool spans (``code_interpreter`` 100 spans,
``run_model`` 75 across its two layers, ``dispatch_tool`` 35), while every AWS-SDK-internal ERROR
(``S3.GetObject`` 222,
``S3.HeadObject`` 110, ``GET`` 69, ``Bedrock AgentCore.*`` 28) is a non-tool span and is dropped
before the signal runs. ``23a1fa24`` is the committed fixture that pins both halves: 7 ERROR
``execute_tool code_interpreter`` spans (which must be found) alongside 14 ``S3.HeadObject`` and 5
``Bedrock AgentCore.InvokeCodeInterpreter`` ERRORs (which must not).

--------------------------------------------------------------------------------------------
TENANT ISOLATION — read before adding anything to the wire
--------------------------------------------------------------------------------------------

``WasteFinding.detail`` names tool names, and a tool name from a USER-REGISTERED MCP server is
user-derived data: ``tool_name_sequence`` strips ``mcp tools/call <target>___<tool>`` down to
exactly that bare name, and nothing cheap distinguishes it from a harness builtin. The served
SNAPSHOT is ONE global item read by every actor, so ``detail`` must NOT reach it. The aggregation
that does (``dto.waste_to_wire``) carries counts and totals only.

Per-finding detail travels on the PER-ACTOR bundle instead, and it already does: ``dto.actor_bundles``
adds a ``findings`` list to that same block and ``_waste_finding_to_wire`` carries the sequence,
because there the reader owns the session it came out of. So the constraint here is about the shared
item's AUDIENCE and not about a carrier that has to be built — this used to say the per-actor bundle
"does not exist yet", which had not been true for months and made the rule read as a temporary
limitation. The same warning is repeated at ``waste_to_wire``, because that is where the field would
get "helpfully" added.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Union

try:
    from .contract import NO_DATA, UNMEASURED, Run, Step, _Sentinel, tool_name_sequence
    from .evaluators_improve import _is_dispatch, _is_retrieve
    from .signature import shape_facets
except ImportError:  # pragma: no cover — direct-script fallback, mirrors the package
    from improve.contract import (  # type: ignore
        NO_DATA, UNMEASURED, Run, Step, _Sentinel, tool_name_sequence,
    )
    from improve.evaluators_improve import _is_dispatch, _is_retrieve  # type: ignore
    from improve.signature import shape_facets  # type: ignore


#: Every kind this module can emit, so a consumer can tell "this signal found nothing" from
#: "this signal does not exist" (absent != empty). ``duplicate_adjacent_call`` is deliberately
#: NOT here — see the module docstring for the measurement that retired it.
WASTE_KINDS: tuple = ("repeated_sequence", "abandoned_dispatch", "error_then_retry")

#: The facet whose CLAIM ``abandoned_dispatch`` rides on. Named once, imported nowhere else, so
#: the two cannot drift: the condition (including `_TAIL_TOLERANCE`, the in-session-retrieve
#: precondition, and `observed`) stays `signature.shape_facets`' single definition and this module
#: only PRICES what that facet already established.
_ABANDONED_FACET = "session_ended_on_unretrieved_dispatch"

# A repeated block must be at least this many STEPS long, where a step is a maximal run of
# consecutive calls of ONE tool (see `_call_steps`). MEASURED SEMANTIC THRESHOLD, and the single
# most consequential line in this module, so its evidence is here rather than in a doc.
#
# Two properties come out of counting steps rather than calls, and the signal is worthless without
# either. First, a long run of ONE tool can no longer masquerade as a repeat: the search is the
# classic longest-repeated-substring DP, which happily splits a homogeneous run down the middle and
# matches each half against the other. Measured before this rule existed, `f0598806`'s longest
# repeat came back as 58 consecutive `check_job` calls "repeated twice" — one poll loop cut in half.
# Second, at >= 2 steps the block is guaranteed to contain >= 2 DISTINCT tool names, because
# adjacent steps differ by construction — one rule instead of two.
#
# Why that matters, measured over the 549 sessions of the complete 336 h harvest: without it the
# top of the distribution is not redone work at all. The longest single-name runs of consecutive
# calls are `check_job` x123 (`f0598806`), `get_download_url` x69 (`df51db2b`), `get_job_status`
# x56 (`db21d0c8`), `check_job` x38 (`8bf565f7`) and `get_job_status` x30 (`98c1afc0`) — polling an
# async job to completion, and fetching N different files. Polling is how a synchronous agent
# WAITS; charging 123 calls of it as "repeated work" would be a confidently wrong accusation on the
# most-repeating session in the population, which is the exact failure mode `88ee151` retired a
# facet for.
#
# Everything the signal exists for survives: the golden fixture `36fafeb7`'s
# `run_model x6 -> dispatch_tool` twice (six generations, then six generations again),
# `abc839ca`'s `process_media -> dispatch_tool` twice, `443eb166`'s eight-step
# `dispatch_tool -> get_plan_results -> get_download_url -> recognize_faces -> ...` pipeline run
# twice, `cd838132`'s `dispatch_tool -> run_model -> dispatch_tool` four times, `a80b8da8`,
# `154b5b07`.
#
# What it does NOT remove, said out loud because a reader of a finding needs to know: a poll loop
# that ENDS in a fetch is two steps (`check_job x111 -> get_job_result`), so a session that ran two
# different async jobs to completion still matches, and its charged cost is dominated by polls.
# Span names cannot distinguish that from a redo — `detail` prints each step's multiplicity for
# exactly this reason, so a large `xN` on a status query is visible as what it is.
#
# Deliberately NOT env-overridable: it decides what a finding CLAIMS. The correct response to it
# being wrong is a re-measurement recorded here, not an ops tweak that silently redefines the
# claim. (`evaluators_improve._TAIL_TOLERANCE` carries the same note for the same reason.)
_MIN_REPEAT_LEN = 2

# How many logical calls the repeated-block search will scan in one run. OPERATIONAL, and
# env-overridable in the `_ALARM_PAGE_CAP` idiom, because it is a compute bound and the right
# value depends on how busy the account's sessions are — exactly when you cannot edit code.
#
# The search is the classic longest-repeated-substring DP, quadratic in the STEPS a run's calls
# collapse to (never more than the calls themselves, which is why the cap is stated in calls), and
# this module runs inside a 900 s Lambda that already spends ~287 s on a 168 h population. Sized
# off headroom rather than off observed need: the busiest run in the complete 336 h harvest holds
# 430 logical calls, so 2,000 is ~4.6x the measured maximum and still only ~4M inner steps. The
# whole 540-run population takes 0.05 s today.
#
# A run over the cap is NOT silently truncated — truncating would report a smaller repeat as
# though it were the longest, which is a wrong answer rather than a missing one. The run is
# skipped for this signal and COUNTED (`WasteReport.runs_not_scanned`, which `dto.waste_to_wire`
# publishes as `runsNotScanned`), because absence must never read as "no waste here" (invariant 1).
_MAX_CALLS_SCANNED = int(os.environ.get("IMPROVE_WASTE_MAX_CALLS", "2000") or "2000")

# How many STEPS a `detail` string spells out before truncating with "+N more". OPERATIONAL, same
# idiom and same reason as `signature._MAX_EVIDENCE_SPAN_IDS`: it changes how much is PRINTED, never
# what the finding claims. Bounded because `detail` rides to the per-actor bundle, which is also one
# DynamoDB item, and a block can be long.
#
# 16 because the longest repeated block on the complete 336 h population is 10 steps (charging 127
# calls), so this never truncates on today's evidence while still capping a pathological run — and
# deliberately NOT 12, which `signature._MAX_EVIDENCE_SPAN_IDS` and
# `evaluators_improve._TAIL_TOLERANCE` already both read. Two unrelated bounds sharing a literal
# reads as a relationship and invites someone to change them together.
_MAX_DETAIL_NAMES = int(os.environ.get("IMPROVE_WASTE_MAX_DETAIL_NAMES", "16") or "16")

#: What each kind can and cannot establish. One constant per kind rather than per-finding prose,
#: so the limitation cannot drift from finding to finding, and so no number appears in it that the
#: structured fields do not already carry (invariant 3: evidence tracks value).
_CONFIDENCE = {
    "repeated_sequence": (
        "OBSERVED: this ordered block of tool calls ran more than once in one session, and the "
        "cost charged is the earlier occurrence(s). NOT ESTABLISHED: that the earlier work was "
        "discarded. Spans record no tool arguments for harness builtins (NS-5), so a second run "
        "over DIFFERENT inputs, or one whose output was also needed, is indistinguishable from a "
        "redo. Read this as repeated, not as wasted."
    ),
    "abandoned_dispatch": (
        "OBSERVED: this session retrieved results for earlier dispatches, so its dispatches do "
        "hand back handles that need fetching, and yet its last dispatch was never followed by a "
        "retrieval before the session ended (the condition is "
        "`signature.session_ended_on_unretrieved_dispatch`). The cost charged is the dispatch "
        "call's OWN duration, which is a FLOOR and not a total: the async job it started runs "
        "behind the gateway and emits no span in this session, so whatever it spent is invisible "
        "here."
    ),
    "error_then_retry": (
        "OBSERVED: a tool call recorded status=ERROR and a later call of the same tool did not, "
        "so the failed attempt produced nothing that was kept. NOT ESTABLISHED: that the retry "
        "was avoidable — spans carry no arguments, so a corrected re-call and a blind repeat look "
        "identical, and an ERROR recovered inside the tool would look the same from outside. "
        "Restricted to TOOL spans: every ERROR in the golden harvest is an AWS-SDK internal "
        "retry, and `had_error` is anti-correlated with run failure (DF-8/DF-10)."
    ),
}


@dataclass
class WasteFinding:
    """One quantified observation of work that was repeated, failed, or left uncollected.

    ``calls`` and ``wall_ms`` are the COST CHARGED, always over logical calls (see the module
    docstring), never over spans. ``wall_ms`` is the ``UNMEASURED`` sentinel — never ``0`` —
    when any contributing call has no recorded duration, because a partial sum presented as a
    total is the same lie the spend block exists to avoid (invariant 1).

    ``detail`` names tool names and therefore may be user-derived. It must not reach the global
    SNAPSHOT; ``dto.waste_to_wire`` aggregates counts and totals only.
    """

    run_id: str
    kind: str
    calls: int
    wall_ms: Union[int, _Sentinel] = UNMEASURED
    occurrences: int = 1
    detail: str = ""
    confidence: str = ""
    #: Positions in THIS run's logical-call sequence that the cost above is charged for. Kept so
    #: the aggregate can union the kinds: an errored call can also sit inside a repeated block, and
    #: summing `calls` across kinds would then charge it twice. Positions in the collapsed call
    #: sequence, NOT span indices, and either way harvest-dependent: spans sharing a millisecond
    #: keep harvest order, so adjacent positions can swap between harvests. Never assert on them
    #: and never put them on the wire.
    charged: tuple = ()


@dataclass(frozen=True)
class LogicalCall:
    """One tool call, after collapsing the instrumentation layers that recorded it.

    ``raw_names`` keeps every span name that contributed, which is what makes the collapse
    auditable: a call recorded three times names all three. ``duration_ms`` is the LONGEST
    contributor (the outermost layer's, i.e. the wall time the agent actually waited) and is
    ``None`` if any contributor lacked one — the same absence-propagation rule as ``wall_ms``.
    """

    name: str
    duration_ms: Optional[int]
    errored: bool
    raw_names: tuple = ()
    #: The span index range this call was assembled from, in the run's FULL trajectory. Nothing in
    #: this module needs it — the dispatch/retrieve pairing below is in CALL space, which agrees
    #: with the facet's span-space one because no NON-tool span name matches `_is_dispatch` or
    #: `_is_retrieve` anywhere in the 549-session harvest (measured). It is kept so the collapse is
    #: auditable and so a future exact mapping has somewhere to land. Harvest-dependent for the
    #: same reason as ``WasteFinding.charged``: never assert on it, never put it on the wire.
    first_span: int = -1
    last_span: int = -1


def is_tool_span(raw_name: str) -> bool:
    """Whether a raw span name is a TOOL call rather than runtime/AWS-SDK instrumentation.

    The same test ``scripts/refresh_improve_fixtures.py`` uses to build the committed
    ``tool_spans`` slice, so the fixture and this module cannot disagree about what a tool is.
    Deliberately substring-based on the two prefixes the runtimes emit (``execute_tool <name>``
    for Strands-level calls including builtins, ``mcp tools/call <target>___<tool>`` for the MCP
    client) — lexical, like ``_is_dispatch``/``_is_retrieve``, and named here so that is visible.
    """
    return "execute_tool" in raw_name or "tools/call" in raw_name


def _layer_distinct(raw_names: tuple, candidate: str) -> bool:
    """Whether ``candidate`` is a NEW instrumentation layer for a call already seen.

    The collapse test, isolated so it is one sentence: two adjacent spans that normalise to the
    same tool are the same call only when their RAW names differ, because that is what
    double-instrumentation looks like (``mcp tools/call X`` beside ``execute_tool X``) and
    repeating the identical raw name is what a genuine second call looks like.
    """
    return candidate not in raw_names


def logical_calls(trajectory) -> list[LogicalCall]:
    """Tool spans of a trajectory, collapsed into the calls they record. NO_DATA/empty -> [].

    Non-tool spans are dropped (the runtime's own instrumentation repeats by construction) and
    the survivors keep their order, so a caller may reason about sequence.
    """
    if trajectory is NO_DATA or not trajectory:
        return []
    steps = list(trajectory)
    normalised = tool_name_sequence(steps)
    calls: list[LogicalCall] = []
    for index, (step, name) in enumerate(zip(steps, normalised)):
        raw = step.name if isinstance(step, Step) else str(step)
        if not is_tool_span(raw):
            continue
        duration = step.duration_ms if isinstance(step, Step) else None
        errored = (step.status if isinstance(step, Step) else "") == "ERROR"
        previous = calls[-1] if calls else None
        if previous is not None and previous.name == name and _layer_distinct(
                previous.raw_names, raw):
            calls[-1] = LogicalCall(
                name=name,
                # Absence propagates: one layer without a duration makes the call's duration
                # unknown rather than "whatever the other layer said".
                duration_ms=(None if previous.duration_ms is None or duration is None
                             else max(previous.duration_ms, duration)),
                # ERROR anywhere in the stack means this call errored, because the question the
                # signal asks is whether the attempt produced anything. The layers DO disagree,
                # though rarely: exactly 1 of the 1,541 multi-layer calls in the complete 336 h
                # harvest is mixed (`cd838132`'s `run_model`, ERROR at the MCP layer and UNSET at
                # the Strands one). Taking the union rather than one layer's word means a failure
                # only one of them recorded is still seen.
                errored=previous.errored or errored,
                raw_names=previous.raw_names + (raw,),
                first_span=previous.first_span,
                last_span=index,
            )
        else:
            calls.append(LogicalCall(name=name, duration_ms=duration, errored=errored,
                                     raw_names=(raw,), first_span=index, last_span=index))
    return calls


def _sum_durations(calls: list[LogicalCall], positions) -> Union[int, _Sentinel]:
    """Total wall time of the named calls, or ``UNMEASURED`` if any one of them lacks a duration.

    Never a partial sum: invariant 1. A caller that wants "at least this much" must say so in
    words, not by silently dropping the unmeasured terms.
    """
    total = 0
    for position in positions:
        duration = calls[position].duration_ms
        if duration is None:
            return UNMEASURED
        total += duration
    return total


def _call_steps(calls: list[LogicalCall]) -> list[tuple]:
    """``[(tool name, positions of the calls in it)]`` — the calls, run-length encoded.

    A STEP is a maximal run of consecutive calls of one tool: ``run_model`` six times in a row is
    one step of six calls. Matching happens over steps and charging happens over the calls inside
    them, which is what lets a finding say "``run_model x6 -> dispatch_tool``, twice, 7 calls".
    """
    steps: list[tuple] = []
    for position, call in enumerate(calls):
        if steps and steps[-1][0] == call.name:
            steps[-1] = (call.name, steps[-1][1] + (position,))
        else:
            steps.append((call.name, (position,)))
    return steps


def _detail_steps(steps: list[tuple]) -> str:
    """``run_model x6 -> dispatch_tool`` for a block of steps, bounded by ``_MAX_DETAIL_NAMES``.

    The multiplicity is printed because it is the reader's only discriminator between a repeated
    unit of work and a poll loop (see ``_MIN_REPEAT_LEN``). The finding's own ``calls`` and
    ``occurrences`` are exact regardless, so truncation loses text and never a number.
    """
    shown = " -> ".join(f"{name} x{len(positions)}" if len(positions) > 1 else name
                        for name, positions in steps[:_MAX_DETAIL_NAMES])
    hidden = len(steps) - _MAX_DETAIL_NAMES
    return f"{shown}{f' -> +{hidden} more' if hidden > 0 else ''}"


def _longest_repeated_block(names: list[str]) -> tuple:
    """``(length, first_start)`` of the longest repeated, NON-OVERLAPPING block of ``names``.

    Longest, not every sub-repeat: a six-step block repeated once contains five two-step repeats,
    ten three-step repeats and so on, so reporting them all would turn one event into twenty
    findings. Ties resolve to the earliest occurrence, which keeps the artifact byte-reproducible
    for a given input.
    """
    total = len(names)
    if total < _MIN_REPEAT_LEN * 2:
        return (0, -1)
    previous = [0] * (total + 1)
    best = (0, -1)
    for i in range(1, total + 1):
        current = [0] * (total + 1)
        for j in range(i + 1, total + 1):
            if names[i - 1] != names[j - 1]:
                continue
            # Longest common suffix of the prefixes ending at i and j, capped at `j - i` so the
            # two occurrences cannot overlap: the same call cannot be both the discarded work and
            # the work that replaced it.
            length = min(previous[j - 1] + 1, j - i)
            current[j] = length
            if length > best[0] and length >= _MIN_REPEAT_LEN:
                best = (length, i - length)
        previous = current
    return best


def _occurrence_starts(names: list[str], block: list[str]) -> list[int]:
    """Start positions of every NON-OVERLAPPING occurrence of ``block``, left to right.

    Greedy and left-anchored, so "the earlier occurrence" stays well defined when a block repeats
    three or more times (``154b5b07`` runs ``process_media -> dispatch_tool -> get_plan_results``
    three times).
    """
    starts: list[int] = []
    size = len(block)
    position = 0
    while position + size <= len(names):
        if names[position:position + size] == block:
            starts.append(position)
            position += size
        else:
            position += 1
    return starts


def _repeated_sequence(run_id: str, calls: list[LogicalCall]) -> Optional[WasteFinding]:
    """The longest repeated block of tool STEPS in one run, charged at its earlier occurrences.

    The cost is every occurrence but the LAST, because the last is the one whose output could
    still be the one that was used. With two occurrences that charges the earlier, discarded one;
    with three it generalises the same way rather than reporting a third of the repetition.

    ``detail`` describes the FIRST charged occurrence. Matching is on the ordered tool names and
    NOT on their multiplicities, and that is the common case rather than an edge one: 56 of the 94
    findings over the complete 336 h population have different multiplicities in different
    occurrences (``fbedf50a`` runs ``run_model`` twice in one occurrence and four times in the
    other). So the claim is "this ordered block of tools ran again", the charge is the earlier
    occurrence's OWN calls, and the printed ``xN`` is the one being charged for.
    """
    steps = _call_steps(calls)
    names = [name for name, _positions in steps]
    length, first = _longest_repeated_block(names)
    if length <= 0:
        return None
    block = names[first:first + length]
    starts = _occurrence_starts(names, block)
    if len(starts) < 2:  # pragma: no cover — the DP found two, so the scan must find >= two
        return None
    charged = tuple(position
                    for start in starts[:-1]
                    for step in steps[start:start + length]
                    for position in step[1])
    return WasteFinding(
        run_id=run_id,
        kind="repeated_sequence",
        calls=len(charged),
        wall_ms=_sum_durations(calls, charged),
        occurrences=len(starts),
        detail=_detail_steps(steps[starts[0]:starts[0] + length]),
        confidence=_CONFIDENCE["repeated_sequence"],
        charged=charged,
    )


def _abandoned_dispatch(run: Run, calls: list[LogicalCall]) -> Optional[WasteFinding]:
    """Work dispatched and never collected, priced at the dispatch call's own duration.

    The CONDITION is not re-implemented here. ``signature.shape_facets`` already owns it —
    including ``_TAIL_TOLERANCE``, the in-session-retrieve precondition that stops the facet
    accusing a sync/inline return (``36fafeb7``), and ``observed``, which is ``False`` when the
    harvest is known to have clipped this run's trajectory and could have dropped the very
    retrieve that would clear it. Reading the facet rather than copying its rule is what keeps the
    two from drifting; all this function adds is locating the dispatch so it can be priced.

    The pairing below is over LOGICAL CALLS while the facet's is over the full span-name sequence.
    Those agree, and it was checked rather than assumed: no non-tool span name matches
    ``_is_dispatch`` or ``_is_retrieve`` anywhere in the 549-session harvest, and the count this
    produces over that population (21) is the same 21 sessions the facet itself fires on.
    ``test_abandoned_dispatch_reuses_the_facets_claim_rather_than_recomputing_it`` pins the
    agreement on the ground-truth fixture.
    """
    facet = next((f for f in shape_facets(run) if f.name == _ABANDONED_FACET), None)
    if facet is None or not facet.observed or not facet.value:
        return None
    dispatches = [i for i, call in enumerate(calls) if _is_dispatch(call.name)]
    retrieves = [i for i, call in enumerate(calls) if _is_retrieve(call.name)]
    unpaired = [d for d in dispatches if not any(r > d for r in retrieves)]
    if not unpaired:  # pragma: no cover — the facet fired, so an unpaired dispatch exists
        return None
    position = unpaired[-1]
    return WasteFinding(
        run_id=run.run_id,
        kind="abandoned_dispatch",
        calls=1,
        wall_ms=_sum_durations(calls, (position,)),
        occurrences=1,
        detail=calls[position].name,
        confidence=_CONFIDENCE["abandoned_dispatch"],
        charged=(position,),
    )


def _error_then_retry(run_id: str, calls: list[LogicalCall]) -> list[WasteFinding]:
    """Every failed tool call that the same tool later completed without erroring.

    One finding per failed attempt, not one per tool: two failures of the same tool are two
    attempts paid for, and collapsing them would report half the cost. Only TOOL spans reach here
    (see the module docstring), which is what keeps the AWS-SDK's recovered internal retries —
    the DF-8/DF-10 measurement — out of it.
    """
    findings: list[WasteFinding] = []
    for position, call in enumerate(calls):
        if not call.errored:
            continue
        if not any(later.name == call.name and not later.errored
                   for later in calls[position + 1:]):
            continue
        findings.append(WasteFinding(
            run_id=run_id,
            kind="error_then_retry",
            calls=1,
            wall_ms=_sum_durations(calls, (position,)),
            occurrences=1,
            detail=call.name,
            confidence=_CONFIDENCE["error_then_retry"],
            charged=(position,),
        ))
    return findings


@dataclass
class WasteReport:
    """The findings, plus the two facts a reader needs to know what they are OVER.

    ``runs_examined`` is the denominator every count here is taken against — ``runsWithWaste: 12``
    means nothing without it, and this population is not the same size run to run.
    ``runs_not_scanned`` names the runs the repeated-block search skipped on
    ``_MAX_CALLS_SCANNED``: reported, not inferred from a missing finding, because "looked and
    found nothing" and "did not look" are different facts and only one is good news.
    """

    findings: list = field(default_factory=list)
    runs_examined: int = 0
    runs_not_scanned: list = field(default_factory=list)


def find_waste(runs: list) -> WasteReport:
    """Every waste finding over a population — ``runs`` only, never ``verdicts``.

    That signature is the assertion this whole module rests on: efficiency is orthogonal to
    correctness, so nothing here may branch on whether a run passed. Findings are ordered by
    ``run_id`` then by the order the signals are declared, so the artifact is byte-reproducible.
    """
    findings: list[WasteFinding] = []
    not_scanned: list[str] = []
    examined = 0
    for run in sorted(runs or (), key=lambda r: r.run_id):
        examined += 1
        calls = logical_calls(run.trajectory)
        if len(calls) > _MAX_CALLS_SCANNED:
            not_scanned.append(run.run_id)
        else:
            repeated = _repeated_sequence(run.run_id, calls)
            if repeated is not None:
                findings.append(repeated)
        abandoned = _abandoned_dispatch(run, calls)
        if abandoned is not None:
            findings.append(abandoned)
        findings.extend(_error_then_retry(run.run_id, calls))
    return WasteReport(findings=findings, runs_examined=examined,
                       runs_not_scanned=not_scanned)
