"""In-cloud, boto3 port of ``ideas/staging-data/harvest.py``.

The offline harvester (``ideas/staging-data/harvest.py``) is a laptop tool: stdlib +
``subprocess`` shelling out to the ``aws`` CLI, writing JSON into its own directory for the
loop to mine offline. It lives OUTSIDE ``agent/src`` and is never packaged, so the deployed
pipeline has no way to refresh its own evidence — the SNAPSHOT only moves when a human re-runs
the harvester and ``seed_snapshot``.

This module is that harvester, moved onto the deploy path so the ``improve-worker`` Lambda can
pull fresh telemetry itself (Option 1). Two things change and NOTHING else:

  * the ``aws`` CLI via ``subprocess`` becomes boto3 clients (``logs``, ``dynamodb``,
    ``cloudwatch``) — the managed ``python3.13`` runtime bundles boto3 and has no CLI for
    ``aws/spans`` Insights anyway;
  * output goes to a caller-chosen directory (``/tmp/improve-harvest`` in Lambda) instead of
    the package's own folder.

Everything else was copied verbatim on purpose: the DynamoDB-JSON (``{"S": ...}``) row shape the
reader expects, and — critically — the window/provenance reconstruction
(``_window_for_session`` and friends), whose contract with ``improve.contract`` (the ``WINDOW_*``
provenance strings) must not drift.

ONE thing has since diverged deliberately: the offline harvester's fixed per-day span window did
NOT beat the Insights row cap, it only made the loss less frequent. ``harvest_spans`` now bisects
any window that comes back at the cap and RECORDS what it still could not read; see its docstring
for the measurement that forced the change. The files land in exactly the paths
``improve.observe.span_trajectory.HarvestedSpanSource`` and
``improve.observe.build_run.HarvestedPlanSource`` read under ``IMPROVE_HARVEST_DIR``:

    <out>/sessions/sessions.json   (spans + per-session `window` + per-session `usage` +
                                   per-session `evidence_complete` + per-session `traces` +
                                   per-session `agentcore_eval`, keyed by session.id)
    <out>/ddb/plan-state.json      (bounded scan, DynamoDB-JSON list)
    <out>/alarms/alarm-history.json (ambient platform health per window)
    <out>/spans/spans_meta.json    (forensics: windows read, queries spent, intervals clipped)

boto3 is imported at MODULE scope deliberately: this module is producer-only, is never
imported by the Capability API image or the projection Lambda (which is what
``tests/test_capability_api_isolation.py`` guards), and the managed runtime has boto3. The
online ``improve/**`` code the API image *does* import stays stdlib-only.

STRICTLY READ-ONLY against AWS: ``logs:StartQuery``/``GetQueryResults``/``DescribeLogGroups``,
``dynamodb:Scan`` (bounded), ``cloudwatch:DescribeAlarms``/``DescribeAlarmHistory``. No writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

import boto3

# The ONE sanctioned timestamp converter for this package (invariant 4). This module keeps its
# own `_utc_iso` for FORMATTING (the offline harvester has the same function and cannot import
# ours), but COMPARING two recorded instants is where the string-vs-string bug lives —
# `'…19.5Z' > '…19.5+00:00'` because `'Z' > '+'` — so `_clipped_overlaps` parses through
# `contract.parse_utc` rather than growing a second rule. `contract` is pure stdlib, so this
# import is safe on the managed runtime and for `tests/test_capability_api_isolation.py`.
#
# `USAGE_AUDIT_RELPATH` comes from `observe.span_trajectory` for the same reason: that module owns
# the harvest LAYOUT, it is pure stdlib, and the reader of the file this module writes resolves its
# path from there. A second copy of the name here is how the writer and the reader would come to
# disagree about which file the audit is in.
try:
    from .contract import parse_utc
    from .observe.span_trajectory import USAGE_AUDIT_RELPATH
except ImportError:  # pragma: no cover — direct-script / CLI use
    from improve.contract import parse_utc  # type: ignore
    from improve.observe.span_trajectory import USAGE_AUDIT_RELPATH  # type: ignore

# ---- constants (kept in step with the offline harvester) --------------------
# The online-eval results log group is staging-specific. Overridable so a different account can
# point at its own; eval is skipped by default in Lambda anyway (see `harvest`), because live
# mode RE-JUDGES every run and does not read these recorded scores.
EVAL_LOG_GROUP = os.environ.get(
    "IMPROVE_EVAL_LOG_GROUP",
    "/aws/bedrock-agentcore/evaluations/results/mediaworks_harness_main_online_eval-UZKOq97qDT")
SPANS_LOG_GROUP = "aws/spans"

# The OTEL trace a span belongs to. It is a TOP-LEVEL span field, not an attribute, and that is
# the whole reason it went unnoticed: every audit of this store searched `attributes.*`, where it
# does not exist. Measured on staging (2026-09-02, 14 days): `fields traceId` resolves on every
# span, `fields attributes.traceId` resolves on none.
#
# Projected because it is the only recorded link from a plan sub-session to the request that
# spawned it. Measured over the same 14 days: 1,481 traces carrying 141 `plan-session-*`
# sub-sessions, 141 of 141 in a trace holding exactly ONE non-plan session, 0 unlinked, and 0
# traces holding two candidate parents. `observe.span_trajectory.parents_by_session` is the join;
# see its docstring for what makes the result exact rather than a heuristic.
#
# Not env-overridable: this is a field name in someone else's schema, not a bound. If the store
# renames it the correct response is to re-measure, not to reconfigure.
TRACE_FIELD = "traceId"

# The tables this harvest scans, and it is ONE. `job-registry` was scanned here nightly and written
# to `ddb/job-registry.json`, which no reader opens: the only `job_registry` consumers in `agent/src`
# are the runtime's own (`task_agent`, `main`, `mcp_manager`), and that is a different object reached
# through `JobRegistry` rather than through this file. Measured in staging on 2026-09-03 the scan read
# 2,157 rows — 2.3 MB of table — every night for nothing. `IMPROVE_JOB_REGISTRY_TABLE` is still set on
# the worker by `infra/lib/agent-infra-stack.ts` and is now read by nothing; that env var and the
# table's read grant are the follow-up.
DDB_TABLES = {
    "plan-state": os.environ.get("IMPROVE_PLAN_STATE_TABLE", "mediaworks-harness-plan-state"),
}

# Alarm pagination safety net. CloudWatch caps describe-alarms / describe-alarm-history at ~100
# per page; a busy account (or an actual outage generating many transitions) spills past one page.
# Reading only page 1 silently drops the rest — an outage past the cap reads as "platform fine",
# which the loop then mines as an AGENT bug. So follow NextToken, but bound the total so a runaway
# history cannot blow the Lambda's time/memory: cap the pages and LOG when the cap truncates.
_ALARM_PAGE_CAP = int(os.environ.get("IMPROVE_ALARM_PAGE_CAP", "20") or "20")

# ---- token usage -------------------------------------------------------------
# WHICH span carries a session's token usage. This is the whole correctness question, and the
# obvious answer is wrong. Three span families in `aws/spans` carry `gen_ai.usage.*`, measured on
# staging over 14 days (2026-08-28):
#
#   `chat`                        5,448 spans   ONE LLM call each, full breakdown incl. cache
#   `chat <model id>`             5,390 spans   a SECOND instrumentation's span for the SAME
#                                               calls (input/output only, no total, no cache)
#   `invoke_agent Strands Agents` 1,772 spans   the ACCUMULATED total for a whole agent
#                                               invocation, i.e. the sum of its own `chat` spans
#
# So `filter ispresent(attributes.gen_ai.usage.total_tokens) | stats sum(...)` — the shape
# `LiveSpanSource.total_tokens_for_session` used to carry — DOUBLE COUNTS. Measured against the
# six golden sessions, the naive sum is EXACTLY 2x the `chat`-only sum (e.g. session abc839ca:
# 3,599,064 naive vs 1,799,532 real), which is also exactly the number the un-provenanced
# `fresh/token-usage-fresh.json` on a laptop still reports. Filter to `chat` and nothing else.
USAGE_SPAN_NAME = "chat"

# Stamped onto every usage record this harvester writes, and CHECKED by the reader
# (`build_run._token_usage_from_harvest`) before it will believe a number. Same discipline as
# WINDOW_* below: a usage record whose provenance is absent or unrecognised is refused rather
# than trusted, because the one un-provenanced file in existence is 2x wrong.
USAGE_FROM_CHAT_SPANS = "chat_spans_grouped_by_session_and_model"

# WHICH SPELLING, and why `input_tokens` is not one of them. Each usage term has aliases —
# `output_tokens`/`completion_tokens`, `cache_read_input_tokens`/`cache_read.input_tokens`,
# `cache_write_input_tokens`/`cache_creation.input_tokens` — so pick one per term or a sum
# double-counts. The UNDERSCORE spellings are chosen because the DOTTED ones did not exist before
# 2026-08-28: measured per day over 14 days, `cache_read.input_tokens` is absent on all 4,730
# spans up to Aug 27 and present only on the 822 from Aug 28-29.
#
# `attributes.gen_ai.usage.input_tokens` is deliberately NOT read at all, because its MEANING
# changed at that same boundary and the change is invisible in the value:
#
#     Aug 15-27   4,730/4,730 spans   input_tokens EXCLUDES the cached tokens (Bedrock's raw
#                                     convention: total = input + output + cacheRead + cacheWrite)
#     Aug 28-29     822/822 spans     input_tokens INCLUDES them (total = input + output)
#
# So `uncached = input - cacheRead - cacheWrite` is right for TODAY's rows and, on last week's,
# evaluates to roughly minus-the-whole-prompt. Instead uncached input is DERIVED from
# `total - output - cacheRead - cacheWrite`, which is the same quantity under BOTH conventions and
# therefore cannot be broken by another instrumentation change. Verified: it lands between 1 and 3
# tokens on all 5,552 cache-bearing `chat` spans, and reproduces exactly the `inputTokens: 2` in an
# AgentCore Memory `message.metadata.usage` block for the same call. The deployed worker reads a
# 24h window so it sees one convention at a time, but a 168h investigation run straddles the
# boundary — which is what makes this a correctness requirement rather than a preference.
_USAGE_FIELDS = ("total_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "llm_calls")

# OPERATIONAL, env-overridable. How many `(session, model)` GROUPS the usage query may return.
#
# This is NOT the 5000 in `harvest_spans`' query, and the two must not be unified just because
# both bound an Insights result. That one is the RECORD cap — Insights' own hard ceiling on rows,
# which `harvest_spans` compares against to detect clipping. This one is a GROUP cap on a `stats`
# query, whose ceiling is 10,000; `stats` aggregates over every matching record regardless, which
# is exactly why usage survives windows where spans get clipped. Right value depends on account
# traffic: groups are sessions x models, measured at ~50 for a 24h staging window and 478 for 400h.
_USAGE_GROUP_LIMIT = int(os.environ.get("IMPROVE_USAGE_GROUP_LIMIT", "10000") or "10000")

# OPERATIONAL, env-overridable. The SEED Insights chunk size for both span and usage harvesting.
#
# One knob rather than two, still — but its meaning is now asymmetric, and the asymmetry is the
# point. `harvest_token_usage` queries exactly these windows. `harvest_spans` uses them as the
# STARTING size and SUBDIVIDES any window that comes back at the row cap (see
# `_INSIGHTS_ROW_CAP`), because a `stats` aggregation is immune to that cap and a raw record
# projection is not.
#
# What must hold between the two is therefore NOT identical boundaries — it is identical
# COVERAGE: same first start, same last end, no gaps, no overlaps, so every session's tokens are
# attributed to the same stretch of time its spans were read from. Because span windows are
# produced only by halving these seeds, that coverage equality is structural rather than
# coincidental, and `test_the_span_and_usage_halves_cover_the_same_time_range` asserts it in that
# form. It used to assert boundary equality, which was the right property only while spans could
# not subdivide.
#
# 24h is the default because it is the scheduled harvest's own window (`IMPROVE_HARVEST_HOURS=24`),
# so the deployed path issues exactly one usage query. It is deliberately NOT tuned down to beat
# the row cap: measured over 14 days on staging (2026-08-29) the mediaworks span rate ranges
# 2,070..13,766 per day — 6.6x — so a fixed window small enough for the worst day wastes 4 queries
# on the nine days in 14 that need one, and would still clip on a bigger spike. Adapting per
# window costs one extra query on a quiet day and scales itself on a busy one.
_INSIGHTS_WINDOW_SECONDS = int(
    os.environ.get("IMPROVE_INSIGHTS_WINDOW_SECONDS", "86400") or "86400")

# OPERATIONAL, env-overridable. The per-query ROW ceiling for the span harvest — both the number
# the query asks for and the number a returned chunk is compared against to know it was CLIPPED.
#
# It is OURS, not the service's: the query string ends `| sort @timestamp asc | limit N`. It lived
# as the bare literal `5000` in two places — inside that query string and in the clip test right
# below it — which is the defect invariant 12 names explicitly ("a `| limit N` buried in a query
# string, or a row cap inlined at its use site"). One constant interpolated into both is what makes
# them incapable of drifting apart, and what lets an account with different traffic move it.
#
# 5000 stays the default because raising it does not fix anything: `sort @timestamp asc | limit N`
# keeps the OLDEST N rows, so a bigger N only moves the truncation boundary later in the day while
# still dropping the session ENDINGS, where retrievals and outcomes live. What fixes it is asking
# for less TIME per query, which is what `harvest_spans` now does.
#
# `>=` is the right comparison and has no false negatives: a truncated read returns EXACTLY the
# cap, always. The reverse can happen — a window holding exactly `_INSIGHTS_ROW_CAP` rows and no
# more is split once unnecessarily — and that costs two queries and loses nothing, which is the
# direction to be wrong in. Measured on the 168h staging replay of 2026-08-29, the largest window
# accepted as complete returned 4,984 rows, so the margin is genuinely thin and the conservative
# comparison is doing work.
_INSIGHTS_ROW_CAP = int(os.environ.get("IMPROVE_INSIGHTS_ROW_CAP", "5000") or "5000")

# OPERATIONAL, env-overridable. The smallest window `harvest_spans` will bisect FROM. A window at
# or below this is read once and, if it still comes back at the row cap, RECORDED as clipped
# rather than split again — the recursion has to terminate somewhere, and terminating with a
# written-down gap is the only honest ending.
#
# 3600 s, from measurement rather than taste, and the measurement says this floor is never
# reached on this account. Over 14 days of staging traffic (2026-08-29, `stats count() by bin(1h)`
# under the same `svc like /mediaworks_harness/` filter) the busiest single HOUR carried 1,643
# spans and the busiest contiguous three hours 3,836 — both under the cap. Confirmed by the
# harvests that exercised it: a 24h run bisected 24h -> 12h -> 6h -> 3h and stopped there with
# 2,166..3,200 rows per window, and a 168h run did the same on its two busiest seeds. So bisection
# terminates because a window came back UNDER the cap, TWO levels above this floor.
#
# The floor therefore exists for the pathological case — a single hour at roughly 3x the observed
# peak — and its job there is to stop recursing and write down what it could not read, rather than
# to be tuned. Note the effective minimum window is just OVER half this value: a 1.5 h window is
# still > 3600 s, so it splits into two 45 min reads before the floor bites.
_MIN_SPAN_WINDOW_S = int(os.environ.get("IMPROVE_MIN_SPAN_WINDOW_S", "3600") or "3600")

# OPERATIONAL, env-overridable. Total Insights queries ONE span harvest may spend. Exhausting it
# records the unread windows instead of reading them, so the cost of an unusually busy account is
# a legible gap rather than a Lambda timeout.
#
# 120, sized off the TIME budget rather than off the query count, because time is the thing that
# actually kills a Lambda. Measured end-to-end against staging on 2026-08-29: a 24 h harvest spent
# 7 span queries + 1 usage query in 23.1 s wall, and a 168 h harvest 21 + 7 in 100.3 s — i.e.
# ~3.5 s per query including API latency, not the 2.0 s the poll alone suggests. The 168 h /
# limit-200 investigation run used 287 s of the 900 s ceiling with ~50 s of that being its own
# (unadapted) harvest, so roughly 600 s is available. 120 queries is ~420 s of it.
#
# Against what the work actually needs, measured on the same day: the deployed 24 h path spent 7
# (17x margin) and a 168 h manual replay spent 21 (5.7x). Nine of 14 days need a single query.
# Beyond the cap the harvest reports what it could not read, which is the behaviour this bound
# exists to produce — it is a guard rail, not a target, and the correct response to hitting it is
# to narrow `--hours`, not to raise it past the time budget.
_INSIGHTS_MAX_QUERIES = int(
    os.environ.get("IMPROVE_INSIGHTS_MAX_QUERIES", "120") or "120")

# Window provenance labels — the contract with `improve.contract` (WINDOW_* constants). Kept in
# step with the offline harvester, which cannot import them either. `collector_stamped` is never
# written here: this harvester reconstructs after the fact, it does not stamp.
WINDOW_FROM_PLAN_STATE = "derived_plan_updated_at"
WINDOW_FROM_SPANS = "derived_span_bounds"
WINDOW_FROM_PLAN_AND_SPANS = "derived_plan_and_span_bounds"


def default_out_dir() -> str:
    """Where the harvest lands. The reader looks here via ``IMPROVE_HARVEST_DIR``."""
    return os.environ.get("IMPROVE_HARVEST_DIR") or "/tmp/improve-harvest"


# ---- AWS clients (replace the offline harvester's `aws` CLI subprocess) ------
class _Aws:
    """Lazily-built, region/profile-scoped boto3 clients. Read-only usage only."""

    def __init__(self, region: str = "us-west-2", profile=None) -> None:
        self._session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self._region = region
        self._cache: dict = {}

    def client(self, name: str):
        if name not in self._cache:
            self._cache[name] = self._session.client(name, region_name=self._region)
        return self._cache[name]


def insights_query(aws: _Aws, log_group, query_string, start_s, end_s,
                   poll=2.0, max_wait=90):
    """Run a Logs Insights query to completion, return list of row-dicts.

    Same shape as the offline harvester's `insights_query`, over boto3 instead of the CLI:
    ``start_query`` returns a ``queryId``; ``get_query_results`` is polled until ``Complete``.
    """
    return _run_insights(aws, {"logGroupName": log_group}, query_string, start_s, end_s,
                         poll=poll, max_wait=max_wait)


def _run_insights(aws: _Aws, target: dict, query_string, start_s, end_s, poll=2.0, max_wait=90):
    """StartQuery against ``target`` (``logGroupName`` or ``logGroupNames``), poll to completion."""
    logs = aws.client("logs")
    qid = logs.start_query(
        **target, startTime=int(start_s), endTime=int(end_s),
        queryString=query_string)["queryId"]
    waited = 0.0
    while waited < max_wait:
        time.sleep(poll)
        waited += poll
        res = logs.get_query_results(queryId=qid)
        status = res.get("status")
        if status == "Complete":
            return [{f["field"]: f.get("value") for f in r} for r in res.get("results", [])]
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Insights query {qid} ended {status}")
    raise TimeoutError(f"query {qid} did not complete in {max_wait}s")


def write_json(out_dir, relpath, obj):
    path = os.path.join(out_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    print(f"  wrote {relpath}  ({len(json.dumps(obj, default=str)) // 1024} KB)")


# ---- span harvest (adaptive windowing that beats the Insights row cap) ------
def _seed_windows(start_s, end_s, size_s=None) -> list:
    """The initial disjoint `[start, end)` chunks, `_INSIGHTS_WINDOW_SECONDS` wide.

    Shared by `harvest_spans` (which BISECTS these when a read comes back at the row cap) and
    `harvest_token_usage` (which queries them as-is). One function rather than two identical
    loops so the two halves cannot come to cover different stretches of time — that coverage
    equality is the property that makes a session's tokens attributable to the spans it was
    scored from. PURE.
    """
    size = int(size_s or _INSIGHTS_WINDOW_SECONDS)
    out = []
    win_start = int(start_s)
    end_s = int(end_s)
    while win_start < end_s:
        win_end = min(win_start + size, end_s)
        out.append((win_start, win_end))
        win_start = win_end
    return out


def harvest_spans(aws: _Aws, out_dir, start_s, end_s):
    """``(rows, clipped)`` — every mediaworks span in the window, and what could not be read.

    ADAPTIVE WINDOWING. Logs Insights returns RECORDS for this query, so it is bounded by
    `_INSIGHTS_ROW_CAP`; the query sorts `@timestamp asc`, so a window that exceeds the cap keeps
    the OLDEST rows and drops the NEWEST — i.e. session ENDINGS, which is where retrievals and
    outcomes live. Fixed per-day windowing did not beat that, it just made it less frequent:
    measured over 14 days on staging (2026-08-29) five of 14 days exceeded 5000, the worst two at
    13,766 and 12,258 spans, so the SCHEDULED daily producer was reasoning about 36% and 41% of
    its own evidence on those days and losing exactly the part that decides whether a dispatch was
    ever retrieved.

    So a window that comes back AT the cap is not kept and warned about. It is DISCARDED and
    replaced by its two halves, recursively, until either every read is under the cap or a window
    is too small to split (`_MIN_SPAN_WINDOW_S`) or the query budget runs out
    (`_INSIGHTS_MAX_QUERIES`) — and those last two append to `clipped`, which is written to
    `spans/spans_meta.json` and carried into `reconstruct_sessions` so the sessions that overlap a
    gap are marked. Nothing is dropped silently; that was the whole defect.

    Discarding the capped read is load-bearing. Concatenating it with its halves would duplicate
    every row it did return, and the duplicates would land in one session's trajectory as repeated
    tool calls — inventing exactly the "repeated work" the efficiency path is meant to measure.
    """
    print("[spans] harvesting cross-runtime tool trajectory...")
    QUERY = (
        'fields @timestamp, attributes.session.id as sid, '
        'attributes.aws.local.service as svc, name, '
        'status.code as status, status.message as status_msg, durationNano, '
        f'{TRACE_FIELD} as trace '
        '| filter svc like /mediaworks_harness/ '
        f'| sort @timestamp asc | limit {_INSIGHTS_ROW_CAP}'
    )
    rows: list = []
    clipped: list = []
    read: list = []
    queries = 0
    # Time-ordered and contiguous at every step: the seeds are ordered, and a split re-inserts
    # its two halves at the FRONT in order, so `popleft` always takes the earliest unread window.
    queue = deque(_seed_windows(start_s, end_s))
    while queue:
        win_start, win_end = queue.popleft()
        if queries >= _INSIGHTS_MAX_QUERIES:
            # Record, never silently proceed. The remaining queue is the unread remainder of the
            # requested range, and each entry names a real interval nothing was read from.
            clipped.append({"from": win_start, "to": win_end, "kept": 0,
                            "from_iso": _epoch_iso(win_start), "to_iso": _epoch_iso(win_end),
                            "why": (f"query budget exhausted at {_INSIGHTS_MAX_QUERIES} queries "
                                    f"(IMPROVE_INSIGHTS_MAX_QUERIES) — nothing was read from "
                                    f"this interval")})
            continue
        chunk = insights_query(aws, SPANS_LOG_GROUP, QUERY, win_start, win_end)
        queries += 1
        if len(chunk) >= _INSIGHTS_ROW_CAP:
            if (win_end - win_start) > _MIN_SPAN_WINDOW_S:
                mid = win_start + (win_end - win_start) // 2
                queue.appendleft((mid, win_end))
                queue.appendleft((win_start, mid))
                print(f"  span window {_epoch_iso(win_start)}..{_epoch_iso(win_end)} "
                      f"({(win_end - win_start) // 60} min) returned {len(chunk)} rows at the "
                      f"{_INSIGHTS_ROW_CAP} cap — DISCARDING that read and splitting into two "
                      f"{(mid - win_start) // 60} min halves")
                continue          # discard the clipped read; the halves replace it
            clipped.append({"from": win_start, "to": win_end, "kept": len(chunk),
                            "from_iso": _epoch_iso(win_start), "to_iso": _epoch_iso(win_end),
                            "why": (f"still at the {_INSIGHTS_ROW_CAP}-row cap with the window "
                                    f"already down to {win_end - win_start}s, at or below the "
                                    f"{_MIN_SPAN_WINDOW_S}s floor (IMPROVE_MIN_SPAN_WINDOW_S) — "
                                    f"the newest rows in this interval are MISSING")})
            print(f"  WARNING: span window {_epoch_iso(win_start)}..{_epoch_iso(win_end)} is at "
                  f"the {_MIN_SPAN_WINDOW_S}s floor and still returned {len(chunk)} rows at the "
                  f"{_INSIGHTS_ROW_CAP} cap — recorded as clipped; raise "
                  f"IMPROVE_INSIGHTS_ROW_CAP or lower IMPROVE_MIN_SPAN_WINDOW_S")
        rows.extend(chunk)
        read.append({"from": win_start, "to": win_end, "rows": len(chunk)})
    rows.sort(key=lambda r: r.get("@timestamp") or "")
    write_json(out_dir, "spans/spans_all.json", rows)
    # Forensics, read by nothing in the pipeline: what was asked for, what was read, and what
    # could not be. `reconstruct_sessions` gets `clipped` directly — this file is for the human
    # answering "why does this session look truncated" a week later.
    write_json(out_dir, "spans/spans_meta.json", {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "requested": {"from": int(start_s), "to": int(end_s),
                      "from_iso": _epoch_iso(start_s), "to_iso": _epoch_iso(end_s)},
        "bounds": {"row_cap": _INSIGHTS_ROW_CAP, "seed_window_s": _INSIGHTS_WINDOW_SECONDS,
                   "min_window_s": _MIN_SPAN_WINDOW_S,
                   "max_queries": _INSIGHTS_MAX_QUERIES},
        "queries": queries,
        "windows_read": read,
        "clipped_windows": clipped,
    })
    print(f"  {len(rows)} spans over {len(read)} window(s) in {queries} Insights "
          f"quer{'y' if queries == 1 else 'ies'}"
          + (f"; {len(clipped)} CLIPPED interval(s) recorded" if clipped
             else "; no window hit the row cap"))
    return rows, clipped


def harvest_token_usage(aws: _Aws, start_s, end_s):
    """``{session_id: usage}`` for every session that ran an LLM call in the window.

    ONE Insights query per day-window, aggregated server-side by ``(session, model)`` — not one
    query per session. `total_tokens_for_session` was per-session, and the worker scores up to
    ``IMPROVE_LIMIT`` (60) sessions while every ``insights_query`` costs at least one 2s poll, so
    the per-session shape would add 120s+ to a run that already uses 287s of a 900s ceiling. A
    ``stats ... by sid, model`` is one round trip for the whole population, which is how
    ``harvest_spans`` already works.

    Grouped by MODEL as well as session because the population is not single-model: measured on
    staging, five distinct model ids appear (opus-4-8, sonnet-5, opus-4-6, sonnet-4-6, haiku-4-5),
    one session can span two of them, and `dispatch_tool` inherits whichever model the turn was
    using. A token count is model-agnostic; a COST is not, so the split is what lets the reader
    price each model at its own rate.

    ``stats`` returns GROUPS, not records, so this is immune to the ``_INSIGHTS_ROW_CAP`` that
    makes ``harvest_spans`` subdivide — and it deliberately does NOT subdivide with it. The two
    queries no longer share boundaries, only COVERAGE: both walk ``_seed_windows`` over the same
    ``(start_s, end_s)``, and the span half then splits individual seeds without changing what the
    union covers. Subdividing here too would buy nothing (an aggregation over 6 h + 6 h returns the
    same sums as one over 12 h, since the counts are additive and merged in ``_usage_by_session``)
    and would cost one 2 s poll per extra window on a 900 s ceiling.

    That asymmetry is also the CROSS-CHECK on the span half, and worth keeping for that alone:
    because this query is uncapped, any session it measures whose spans are absent from the
    harvest is a span the row cap lost. ``reconstruct_sessions`` counts and logs exactly that, so
    the producer audits its own truncation. At the deployed ``IMPROVE_HARVEST_HOURS=24`` this is
    exactly ONE query.
    """
    print("[usage] harvesting per-session token usage...")
    QUERY = (
        'fields attributes.session.id as sid, attributes.gen_ai.request.model as model '
        '| filter attributes.aws.local.service like /mediaworks_harness/ '
        f'| filter name = "{USAGE_SPAN_NAME}" '
        '| filter ispresent(attributes.gen_ai.usage.total_tokens) '
        '| stats sum(attributes.gen_ai.usage.total_tokens) as total_tokens, '
        'sum(attributes.gen_ai.usage.output_tokens) as output_tokens, '
        'sum(attributes.gen_ai.usage.cache_read_input_tokens) as cache_read_tokens, '
        'sum(attributes.gen_ai.usage.cache_write_input_tokens) as cache_write_tokens, '
        'count() as llm_calls '
        'by sid, model '
        f'| limit {_USAGE_GROUP_LIMIT}'
    )
    rows = []
    for win_start, win_end in _seed_windows(start_s, end_s):
        chunk = insights_query(aws, SPANS_LOG_GROUP, QUERY, win_start, win_end)
        if len(chunk) >= _USAGE_GROUP_LIMIT:
            print(f"  WARNING: usage window {win_start}..{win_end} returned "
                  f"{len(chunk)} groups, at the {_USAGE_GROUP_LIMIT} cap — some sessions' tokens "
                  f"are MISSING from this harvest, not zero. Raise IMPROVE_USAGE_GROUP_LIMIT or "
                  f"narrow IMPROVE_INSIGHTS_WINDOW_SECONDS.")
        rows.extend(chunk)
    usage = _usage_by_session(rows)
    measured = sum(u["total_tokens"] for u in usage.values())
    print(f"  {len(usage)} sessions with LLM usage, {measured} tokens "
          f"({sum(len(u['byModel']) for u in usage.values())} session-model pairs)")
    return usage


def _usage_by_session(rows):
    """Insights ``(sid, model)`` group rows -> ``{sid: usage}``. PURE.

    Two things are computed here rather than in the query. ``input_tokens`` is DERIVED as
    ``total - output - cache_read - cache_write`` because the recorded
    ``gen_ai.usage.input_tokens`` attribute is inconsistent about whether it includes the cached
    tokens (see ``_USAGE_FIELDS``); the derivation is Bedrock's own arithmetic and is the same
    number under either convention. It is floored at 0 so a span that recorded a total but no
    cache attributes can never produce a negative "input".

    ``cost`` is deliberately ABSENT from the record: the harvest carries token counts and the
    READER prices them (``observe.build_run._cost_usd``, beside the rate table and the provenance
    it publishes). One side of the seam owns the rates, so re-pricing needs no re-harvest, and a
    model with no rate row is skipped there — its tokens counted as unpriced — rather than arriving
    here as a zero cost.

    A row that does not carry ``total_tokens`` produces NO record, rather than a record of 0.
    That guard is load-bearing, not defensive: coercing a missing field with ``or 0`` wrote a
    trusted-provenance ``{"total_tokens": 0}`` for every session, which the reader would then
    have believed — absence arriving as a measured zero, the exact failure invariant 1 exists to
    prevent, and a "spend" chart reading 0 tokens for a run that cost real money.
    """
    by_session: dict = {}
    for row in rows or []:
        sid = row.get("sid")
        if not sid:
            continue
        if row.get("total_tokens") in (None, ""):
            print(f"  [warn] session {sid[:8]}: usage row carries no total_tokens "
                  f"(fields {sorted(row)}) — no usage recorded rather than a zero")
            continue
        model = row.get("model") or "UNKNOWN"
        try:
            counts = {f: int(float(row.get(f) or 0)) for f in _USAGE_FIELDS}
        except (TypeError, ValueError):
            print(f"  [warn] session {sid[:8]}: unparseable usage row {row!r} — skipped")
            continue
        counts["input_tokens"] = max(0, counts["total_tokens"] - counts["output_tokens"]
                                     - counts["cache_read_tokens"] - counts["cache_write_tokens"])
        record = by_session.setdefault(sid, {
            **{f: 0 for f in _USAGE_FIELDS}, "input_tokens": 0,
            "byModel": {}, "provenance": USAGE_FROM_CHAT_SPANS,
        })
        for key, value in counts.items():
            record[key] += value
        # A session's models are its own; two rows for the same (sid, model) can only come from
        # two day-windows of one long session, so they add.
        existing = record["byModel"].setdefault(model, {k: 0 for k in counts})
        for key, value in counts.items():
            existing[key] += value
    return by_session


def harvest_eval(aws: _Aws, out_dir, start_s, end_s):
    """Online-eval judge scores. Informational only in live mode (the judge re-scores)."""
    print("[eval] harvesting online-eval judge results...")
    rows = insights_query(
        aws, EVAL_LOG_GROUP,
        'fields @timestamp, attributes.session.id as sid, '
        'attributes.gen_ai.evaluation.name as evaluator, '
        'attributes.gen_ai.evaluation.score.value as score, '
        'attributes.gen_ai.evaluation.explanation as explanation '
        '| sort @timestamp desc | limit 2000',
        start_s, end_s,
    )
    write_json(out_dir, "eval-results/eval_results.json", rows)
    return rows


# ---- AgentCore online-eval scores, per session (the model cards' AgentCore rows) -------------
# Every online-eval config writes its results to `<prefix><config name>-<generated id>`, so the
# groups are DISCOVERED by prefix rather than named: a second config (or a re-created one, whose
# id changes) is picked up without a code or env change. The prefix is the project's own
# (`IMPROVE_EVAL_RESULTS_PREFIX`, set by the stack), so another project's configs in the same
# account are never read.
EVAL_RESULTS_PREFIX = os.environ.get(
    "IMPROVE_EVAL_RESULTS_PREFIX", "/aws/bedrock-agentcore/evaluations/results/mediaworks_harness_")
# StartQuery accepts up to 50 groups; one per online-eval config, and a project has two today.
_MAX_EVAL_LOG_GROUPS = int(os.environ.get("IMPROVE_MAX_EVAL_LOG_GROUPS", "10") or "10")
# Insights' own result ceiling. The query is a server-side `stats ... by sid, evaluator`, so a row
# is one (session, evaluator) PAIR — not one evaluation — and the cap binds only past 10k pairs
# (measured on staging, 7 days, 3 evaluators: 332 pairs from 1,308 records).
_EVAL_ROW_CAP = 10000
AGENTCORE_EVAL_QUERY = (
    'filter name = "gen_ai.evaluation.result" '
    '| fields attributes.session.id as sid, attributes.gen_ai.evaluation.name as evaluator, '
    'attributes.gen_ai.evaluation.score.value as score '
    '| filter ispresent(score) and ispresent(sid) '
    f'| stats avg(score) as mean, count(*) as n by sid, evaluator | limit {_EVAL_ROW_CAP}'
)


def discover_eval_log_groups(aws: _Aws, prefix: str = None) -> list:
    """The online-eval RESULT log groups under ``prefix``, sorted, at most ``_MAX_EVAL_LOG_GROUPS``."""
    prefix = prefix or EVAL_RESULTS_PREFIX
    names = []
    for page in aws.client("logs").get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=prefix):
        names.extend(g["logGroupName"] for g in page.get("logGroups", []) if g.get("logGroupName"))
    names = sorted(set(names))
    if len(names) > _MAX_EVAL_LOG_GROUPS:
        print(f"  [warn] {len(names)} eval result groups under {prefix}; reading the first "
              f"{_MAX_EVAL_LOG_GROUPS}")
    return names[:_MAX_EVAL_LOG_GROUPS]


def agentcore_eval_by_session(rows) -> dict:
    """``{sid: {evaluator: {"mean": float, "n": int}}}`` from the stats rows. PURE.

    A row whose mean is not a number is dropped, never zeroed: it is an evaluation that produced
    no score, and a 0 would read as the worst score. ``n`` is how many evaluations the mean is
    over — one for a SESSION-level evaluator, one per trace or tool call for the others.
    """
    out: dict = {}
    for row in rows or ():
        sid, evaluator = row.get("sid"), row.get("evaluator")
        if not sid or not evaluator:
            continue
        try:
            mean = float(row.get("mean"))
            n = int(float(row.get("n") or 0))
        except (TypeError, ValueError):
            continue
        if mean != mean or n <= 0:  # NaN, or nothing counted
            continue
        out.setdefault(sid, {})[evaluator] = {"mean": round(mean, 4), "n": n}
    return out


def harvest_agentcore_eval(aws: _Aws, start_s, end_s):
    """AgentCore online-eval scores per session over the harvest window, or ``None`` if unread.

    ``None`` (no result group, or the read failed) and ``{}`` (groups read, nothing scored) are
    different facts and stay different: ``reconstruct_sessions`` writes nothing for the first.
    Bounded by construction: one Insights query across every discovered group, aggregated
    server-side. The window is the span harvest's; a session's results arrive ~session-timeout
    after it ends, so the newest sessions may not be scored YET — they read as unmeasured.
    """
    print("[agentcore-eval] harvesting AgentCore online-eval scores...")
    try:
        groups = discover_eval_log_groups(aws)
        if not groups:
            print(f"  [skip] no online-eval result groups under {EVAL_RESULTS_PREFIX}")
            return None
        rows = _run_insights(aws, {"logGroupNames": groups}, AGENTCORE_EVAL_QUERY, start_s, end_s)
    except Exception as e:  # noqa: BLE001 — informational evidence; never fail the harvest on it
        print(f"  [warn] AgentCore eval scores unread ({type(e).__name__}: {e})")
        return None
    if len(rows) >= _EVAL_ROW_CAP:
        print(f"  [warn] AgentCore eval query hit its {_EVAL_ROW_CAP}-row cap; some pairs are missing")
    by_session = agentcore_eval_by_session(rows)
    print(f"  {len(rows)} (session, evaluator) pairs over {len(by_session)} session(s) "
          f"from {len(groups)} group(s)")
    return by_session


# ---- timestamp / window reconstruction (copied verbatim; PURE) --------------
def _utc_iso(raw, plus_ms=0):
    """Canonical ``...+00:00`` string for a recorded timestamp, or None if unusable.

    ONE format, matching ``improve.contract.parse_utc``: Insights ``@timestamp`` is
    offset-LESS UTC and the plan writer emits both ``...Z`` and ``...+00:00``; all three are
    normalized here. Mixing them is not cosmetic — as STRINGS, ``'…19.5Z' > '…19.5+00:00'``.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if plus_ms:
        from datetime import timedelta
        parsed = parsed + timedelta(milliseconds=plus_ms)
    return parsed.astimezone(timezone.utc).isoformat()


def _epoch_iso(epoch_s) -> str:
    """Canonical ``...+00:00`` for an Insights window bound, which is epoch SECONDS.

    Same one format as ``_utc_iso`` produces for a recorded timestamp, so a clipped interval and
    a session's span bounds are written in the same spelling and a human comparing the two by eye
    is not also comparing two formats. Returns "" for anything unusable.
    """
    try:
        return datetime.fromtimestamp(float(epoch_s), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _clipped_overlaps(session_spans, clipped) -> list:
    """The recorded clip intervals this session's span bounds intersect. PURE.

    Compared on PARSED INSTANTS, never on strings: ``_span_bounds`` returns canonical
    ``...+00:00`` text while a clip carries epoch seconds, and the one rule for turning recorded
    text into an instant is ``contract.parse_utc`` (invariant 4).

    Bounds are treated as CLOSED on both sides, so a session that merely touches the edge of a
    clipped interval counts as overlapping it. That is the safe direction: the flag's job is to
    stop a partially-read run from being trusted, and over-marking costs a run its vote while
    under-marking manufactures a control — which is the defect this whole change exists to fix.

    A session with no parseable span bounds returns [] rather than being marked incomplete: it has
    no trajectory to be missing part of, and `trajectory_absent` already says so.
    """
    if not clipped:
        return []
    start_iso, end_iso = _span_bounds(session_spans)
    start, end = parse_utc(start_iso), parse_utc(end_iso)
    if start is None or end is None:
        return []
    hits = []
    for entry in clipped:
        try:
            gap_start = datetime.fromtimestamp(float(entry.get("from")), timezone.utc)
            gap_end = datetime.fromtimestamp(float(entry.get("to")), timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if start <= gap_end and gap_start <= end:
            hits.append(entry)
    return hits


def _evidence_note(hits) -> str:
    """One bounded sentence naming the clipped stretch a session overlaps.

    The UNION of the overlapping intervals rather than a list of them, so the note is the same
    size for a session that straddles one clip and one that straddles thirty — this string reaches
    `Run.evidence_note` and a session in a saturated window could otherwise carry an unbounded
    list. The COUNT is exact; only the intervals are collapsed.
    """
    if not hits:
        return ""
    starts = [h.get("from_iso") or _epoch_iso(h.get("from")) for h in hits]
    ends = [h.get("to_iso") or _epoch_iso(h.get("to")) for h in hits]
    lo = min(s for s in starts if s) if any(starts) else "?"
    hi = max(e for e in ends if e) if any(ends) else "?"
    return (f"part of this session's trajectory is KNOWN missing: its spans overlap "
            f"{len(hits)} harvest window(s) that hit the Insights row cap, spanning {lo}..{hi}. "
            f"Trajectory-derived facets are unobservable for this run, not False.")


def _span_bounds(session_spans):
    """(first span start, last span END) for a session, canonical form, or (None, None).

    The end bound adds the last span's duration: ``ts`` is when a span STARTED, so using it raw
    would clip the window by however long the final call took.
    """
    starts, ends = [], []
    for span in session_spans:
        start = _utc_iso(span.get("ts"))
        if not start:
            continue
        starts.append(start)
        ends.append(_utc_iso(span.get("ts"), plus_ms=span.get("duration_ms") or 0))
    if not starts:
        return None, None
    return min(starts), max(e for e in ends if e)


def _plan_state_bounds(plan_rows, session_id):
    """(min, max) of this session's plan-state ``updated_at`` writes, canonical form.

    A DIFFERENT store from spans, which is the point: when the span pipeline is the thing that
    broke, plan rows may still place the run in time.
    """
    stamps = []
    for row in plan_rows or []:
        if row.get("session_id", {}).get("S") != session_id:
            continue
        stamp = _utc_iso(row.get("updated_at", {}).get("S"))
        if stamp:
            stamps.append(stamp)
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def _window_for_session(session_id, session_spans, plan_rows, recorded_at):
    """The run's wall-clock interval, from plan-state and/or span bounds, or None.

    Provenance names which source(s) produced the bounds; nothing here claims a collector
    stamp. Returning None (rather than a guess) lets the reader emit its UNKNOWN sentinel,
    which is never treated as "the platform was fine".
    """
    plan_start, plan_end = _plan_state_bounds(plan_rows, session_id)
    span_start, span_end = _span_bounds(session_spans)
    starts = [s for s in (plan_start, span_start) if s]
    ends = [e for e in (plan_end, span_end) if e]
    if not starts or not ends:
        return None
    if plan_start and span_start:
        provenance = WINDOW_FROM_PLAN_AND_SPANS
    elif plan_start:
        provenance = WINDOW_FROM_PLAN_STATE
    else:
        provenance = WINDOW_FROM_SPANS
    return {
        "start": min(starts),
        "end": max(ends),
        "provenance": provenance,
        "recorded_at": recorded_at,
        "note": (f"harvest_live.py; {len(session_spans)} spans"
                 f"{'; plan-state rows present' if plan_start else '; no plan-state rows'}"
                 f"; span bounds {span_start or '-'}..{span_end or '-'}"
                 f"; plan bounds {plan_start or '-'}..{plan_end or '-'}"),
    }


def reconstruct_sessions(out_dir, spans, eval_rows, plan_rows=None, recorded_at="", usage=None,
                         clipped=None, agentcore_eval=None):
    """Write ``sessions/sessions.json``: spans, window, ``usage``, ``traces`` and COMPLETENESS.

    ``usage`` lands HERE, next to ``window``, rather than in a file of its own, because this is
    the file the readers actually open — ``HarvestedSpanSource`` and ``HarvestedWindowSource``
    both read it, while ``spans/spans_all.json`` is read by nothing. A session with no LLM usage
    in the window gets NO ``usage`` key at all, not a zeroed one: absent means "we did not
    measure it", and `{"total_tokens": 0}` would claim a free run.

    ``clipped`` is ``harvest_spans``' second return value, and lands the same way for the same
    reason: the reader that needs it (``HarvestedSpanSource.completeness_for`` ->
    ``build_run`` -> ``signature.shape_facets``) opens THIS file.

    A usage query that RAN also writes :data:`USAGE_AUDIT_RELPATH` beside this file, holding how
    much of the measured spend has no trajectory to attach to (`_usage_shortfall`). ``usage=None``
    — the caller had no usage query — writes NO audit, so the reader answers UNMEASURED rather than
    a zero that would claim the cap cost nothing.

    ``None`` (the default) means the caller had no clip information, so NEITHER key is written and
    the reader reports UNKNOWN — "nobody looked". ``[]`` means the harvest checked and clipped
    nothing, which is a measurement and is written as ``evidence_complete: true``. Those two must
    not collapse: defaulting to ``true`` would let every harvest written before these two keys
    existed claim its evidence was verified complete.

    SCOPE, stated because the field name is broader than the fact: this flag is about the Insights
    ROW CAP only. A session that began before ``--hours`` opened is also partially observed, and
    that is what ``window``'s provenance and note describe; conflating the two here would mark
    every run still in flight at harvest time and route most of the population to inconclusive.
    """
    print("[sessions] reconstructing per-session trajectories...")
    from collections import defaultdict
    recorded_at = recorded_at or datetime.now(timezone.utc).isoformat()
    sessions = defaultdict(
        lambda: {"spans": [], "scores": [], "runtimes": set(), "had_error": False,
                 "traces": set()})
    for s in spans:
        sid = s.get("sid")
        if not sid:
            continue
        sess = sessions[sid]
        svc = (s.get("svc") or "").replace("mediaworks_harness_", "").replace(".DEFAULT", "")
        sess["spans"].append({
            "ts": s.get("@timestamp"), "runtime": svc, "name": s.get("name"),
            "status": s.get("status"), "msg": s.get("status_msg"),
            "duration_ms": (int(s["durationNano"]) // 1_000_000) if s.get("durationNano") else None,
        })
        sess["runtimes"].add(svc)
        if s.get("trace"):
            sess["traces"].add(s["trace"])
        if s.get("status") == "ERROR":
            sess["had_error"] = True
    for e in eval_rows:
        sid = e.get("sid")
        if sid in sessions:
            sessions[sid]["scores"].append(
                {"evaluator": e.get("evaluator"), "score": e.get("score")})
    out = {}
    incomplete = 0
    for sid, s in sessions.items():
        out[sid] = {
            "runtimes": sorted(s["runtimes"]),
            "multi_runtime": len(s["runtimes"]) > 1,
            "had_error": s["had_error"],
            "n_spans": len(s["spans"]),
            "scores": s["scores"],
            "window": _window_for_session(sid, s["spans"], plan_rows, recorded_at),
            # Which OTEL traces this session's spans belong to, so the reader can join a plan
            # sub-session to the request that spawned it (`parents_by_session`). PLURAL, and it
            # has to be: a sub-session is confined to one trace, but a conversation spans one per
            # invocation — measured on staging, up to 163 for a single session — so `trace`
            # singular would silently keep whichever span came last and break the children whose
            # trace it discarded. Written per SESSION rather than per span because the join reads
            # only the (session, trace) association, and repeating a 32-hex id on every span
            # would roughly double this file for no new fact.
            #
            # Needs no bound of its own: each span contributes at most one entry, so the list is
            # already bounded by `_INSIGHTS_ROW_CAP`.
            "traces": sorted(s["traces"]),
            "spans": s["spans"],
        }
        if clipped is not None:
            hits = _clipped_overlaps(s["spans"], clipped)
            out[sid]["evidence_complete"] = not hits
            out[sid]["evidence_note"] = _evidence_note(hits)
            incomplete += 1 if hits else 0
        recorded_usage = (usage or {}).get(sid)
        if recorded_usage:
            out[sid]["usage"] = {**recorded_usage, "recorded_at": recorded_at}
        # AgentCore online-eval scores, `{evaluator: {mean, n}}` (`harvest_agentcore_eval`). Same
        # absence rule as `usage`: a session nobody scored gets NO key, never an empty block.
        recorded_eval = (agentcore_eval or {}).get(sid)
        if recorded_eval:
            out[sid]["agentcore_eval"] = recorded_eval
    if clipped:
        print(f"  [warn] {incomplete} of {len(out)} session(s) overlap a CLIPPED harvest window, "
              f"so part of their trajectory is known missing — their trajectory-derived facets "
              f"are unobservable, which routes them to 'inconclusive' rather than letting them "
              f"act as controls")
    if usage is not None:
        audit = _usage_shortfall(usage, out, recorded_at)
        write_json(out_dir, USAGE_AUDIT_RELPATH, audit)
        if audit["sessions_discarded"]:
            print(f"  [warn] {audit['sessions_discarded']} session(s) had measured usage but NO "
                  f"spans in this harvest ({audit['tokens_discarded']} tokens dropped vs "
                  f"{audit['tokens_kept']} kept) — `harvest_spans` clipped them at the "
                  f"{_INSIGHTS_ROW_CAP}-row cap, so they cannot be scored. Narrow --hours or "
                  f"IMPROVE_INSIGHTS_WINDOW_SECONDS to keep them.")
    write_json(out_dir, "sessions/sessions.json", out)
    return out


def _usage_shortfall(usage: dict, sessions: dict, recorded_at: str) -> dict:
    """Usage the SPAN harvest could not host — THE producer's audit of its own truncation.

    ``harvest_spans`` is bounded by ``_INSIGHTS_ROW_CAP`` per window; the usage query aggregates
    server-side so it is not, which means it measures sessions whose spans the cap dropped. Those
    sessions cannot be scored (no trajectory), so their usage is discarded — and this is the only
    quantified read on what the cap costs. It is computed from the OTHER SIDE of the pipeline, so it
    cannot be fooled by a bug in the windowing above. Measured on staging 2026-08-29 BEFORE adaptive
    windowing, a 24h harvest: 44 sessions measured, 20 kept, 13.3M of 36.8M tokens discarded. A
    near-zero gap here is the empirical acceptance test for the bisection.

    WRITTEN rather than only logged, because the number this quantifies is missing from a figure
    that reads as complete: ``scorecard.spend.tokens`` sums only sessions the population admitted,
    and those are a subset of ``sessions`` here, which is itself a subset of what ``usage``
    measured. So a log line is visible to whoever tails the worker and to nobody who reads the
    spend. ``dto._spend`` publishes it beside that sum (`scorecard.spend.usageShortfall`).

    ``tokens_kept`` is over every RECONSTRUCTED session, which is a WIDER set than the scored
    population: the span floor and the population cap narrow it again afterwards
    (`observe.population.harvest_population`). So ``tokens_kept`` is an upper bound on the spend
    the scorecard reports, never an identity with it.
    """
    discarded = {sid: u for sid, u in usage.items() if sid not in sessions}
    return {
        "sessions_measured": len(usage),
        "sessions_discarded": len(discarded),
        "tokens_measured": sum(u.get("total_tokens", 0) for u in usage.values()),
        "tokens_discarded": sum(u.get("total_tokens", 0) for u in discarded.values()),
        "tokens_kept": sum(u.get("total_tokens", 0)
                           for sid, u in usage.items() if sid in sessions),
        "row_cap": _INSIGHTS_ROW_CAP,
        "recorded_at": recorded_at,
    }


# ---- DynamoDB scan (boto3 low-level, preserves the {"S": ...} row shape) -----
def harvest_ddb(aws: _Aws, out_dir, max_items=300):
    """Bounded scans of the tables a reader reads. Returns ``{label: [items]}``.

    Uses the LOW-LEVEL ``dynamodb`` client (not the resource layer) on purpose: the reader
    (``HarvestedPlanSource``) expects DynamoDB-JSON rows (``{"session_id": {"S": ...}}``), which
    is exactly what ``client.scan`` returns. The resource layer would deserialize to plain
    Python and the shape would no longer match. ``MaxItems`` bounds the total, mirroring the
    offline harvester's ``--max-items 300``.

    ``max_items`` TRUNCATES SILENTLY unless something says so, which is why the cap is reported
    rather than merely applied. A short plan snapshot does not fail: `HarvestedPlanSource` simply
    finds no row for a session, `build_run` records ``plan_absent`` for it, and that reads as an
    observability defect in the runtime — when the cause was this function's own bound. What is
    printed is the KEPT count against the cap, not a count of what was dropped: a paginator that
    stopped at its limit cannot know how many rows were behind it, and inventing that number would be
    the kind of confident figure the rest of this file refuses.
    """
    print("[ddb] snapshotting plan-state (bounded scan)...")
    ddb = aws.client("dynamodb")
    items = {}
    for label, table in DDB_TABLES.items():
        try:
            rows = []
            paginator = ddb.get_paginator("scan")
            # Session_Hit_Store items (project_search/session_hits.py's DynamoSessionHitStore,
            # design Data Models "Session_Hit_Store") share the plan-state table but live in a
            # `HITS#<session_id>` partition. They are delivered-Hit payloads, not plan rows, so
            # exclude them from the scan — a Hit item must never be read as plan state. The
            # partition-key attribute is `session_id`, matching the Hit item's PK `HITS#<sid>`.
            for page in paginator.paginate(
                    TableName=table,
                    FilterExpression="NOT begins_with(session_id, :hits)",
                    ExpressionAttributeValues={":hits": {"S": "HITS#"}},
                    PaginationConfig={"MaxItems": max_items}):
                rows.extend(page.get("Items", []) or [])
            items[label] = rows
            write_json(out_dir, f"ddb/{label}.json", rows)
            if len(rows) >= max_items:
                print(f"  [warn] {label} scan stopped at its {max_items}-row cap "
                      f"({len(rows)} kept) — whether the table holds more is UNKNOWN from here, so "
                      f"a session with no plan row in this snapshot may have one in the table; "
                      f"raise `--max-items` before filing that as missing plan state.")
        except Exception as e:  # noqa: BLE001 — a missing table is a warning, not a crash
            print(f"  [warn] {table}: {e}")
            items[label] = []
    return items


# ---- alarms (ambient platform health per run window) ------------------------
def harvest_alarms(aws: _Aws, out_dir, start_s, end_s, account="", region="us-west-2"):
    """CloudWatch alarm state + retained state-change history over the harvest window.

    The "was the platform degraded?" evidence: ``describe-alarms`` for current state and
    ``describe-alarm-history`` for transitions. ``coverage`` is recorded so the reader refuses
    to answer for a run window outside it — "no transitions" over an unharvested stretch is not
    evidence of health. Both current state AND transitions are needed: an alarm firing before
    the window and staying firing emits no item inside it.
    """
    print("[alarms] harvesting CloudWatch alarm state + history...")
    cw = aws.client("cloudwatch")
    start_iso = datetime.fromtimestamp(start_s, timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_s, timezone.utc).isoformat()
    # Current state — page through EVERY alarm. Stopping at page 1 would miss a firing alarm past
    # the cap, so the window would read as healthy when it was not.
    alarms = {}
    try:
        pages = cw.get_paginator("describe_alarms").paginate(
            PaginationConfig={"MaxItems": _ALARM_PAGE_CAP * 100})
        n_pages = 0
        for page in pages:
            n_pages += 1
            for key in ("MetricAlarms", "CompositeAlarms"):
                for alarm in page.get(key, []) or []:
                    alarms[alarm["AlarmName"]] = {
                        "name": alarm["AlarmName"],
                        "current_state": alarm.get("StateValue", ""),
                        "state_updated_at": str(alarm.get("StateUpdatedTimestamp", "")),
                        "kind": "composite" if key == "CompositeAlarms" else "metric",
                        "transitions": [],
                    }
            if n_pages >= _ALARM_PAGE_CAP:
                print(f"  [warn] describe-alarms hit the {_ALARM_PAGE_CAP}-page cap "
                      f"({len(alarms)} alarms so far) — later alarms are NOT in this snapshot; "
                      f"raise IMPROVE_ALARM_PAGE_CAP if the account has more.")
                break
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] describe-alarms failed ({e}) — writing nothing; the reader reports "
              f"UNKNOWN rather than assuming a healthy platform")
        return None

    # State-change history — same pagination story: an outage generates many transitions, and only
    # reading page 1 would hide every transition past the cap, i.e. hide the outage itself.
    history_items = []
    try:
        pages = cw.get_paginator("describe_alarm_history").paginate(
            HistoryItemType="StateUpdate", StartDate=start_iso, EndDate=end_iso,
            ScanBy="TimestampAscending",
            PaginationConfig={"MaxItems": _ALARM_PAGE_CAP * 100})
        n_pages = 0
        for page in pages:
            n_pages += 1
            history_items.extend(page.get("AlarmHistoryItems", []) or [])
            if n_pages >= _ALARM_PAGE_CAP:
                print(f"  [warn] describe-alarm-history hit the {_ALARM_PAGE_CAP}-page cap "
                      f"({len(history_items)} transitions so far) — older transitions in the "
                      f"window are dropped; raise IMPROVE_ALARM_PAGE_CAP for a busier window.")
                break
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] describe-alarm-history failed ({e}) — recording alarms with no "
              f"history, which the reader treats as indeterminate, not healthy")
        history_items = []

    for item in history_items:
        name = item.get("AlarmName", "")
        entry = alarms.get(name)
        if entry is None:
            entry = alarms.setdefault(name, {"name": name, "current_state": "",
                                             "kind": "unknown", "transitions": []})
        data = item.get("HistoryData")
        try:
            parsed = json.loads(data) if isinstance(data, str) else (data or {})
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        entry["transitions"].append({
            "ts": _utc_iso(str(item.get("Timestamp", ""))) or str(item.get("Timestamp", "")),
            "old_state": (parsed.get("oldState") or {}).get("stateValue", ""),
            "new_state": (parsed.get("newState") or {}).get("stateValue", ""),
            "summary": (item.get("HistorySummary") or "")[:200],
        })

    out = {
        "_provenance": {
            "source": "cloudwatch describe-alarms + describe-alarm-history (read-only)",
            "account": account or "(caller account)", "region": region,
            "why": ("ambient platform health per run window; ACCOUNT-WIDE, so it supports "
                    "correlation and never per-run attribution"),
        },
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {"start": start_iso, "end": end_iso},
        "alarms": sorted(alarms.values(), key=lambda a: a["name"]),
    }
    write_json(out_dir, "alarms/alarm-history.json", out)
    firing = [a["name"] for a in out["alarms"] if a["current_state"] == "ALARM"]
    print(f"  {len(out['alarms'])} alarms, {len(firing)} currently in ALARM")
    return out


# ---- top-level entrypoint the Lambda calls ----------------------------------
def harvest(out_dir=None, hours=24, region="us-west-2", profile=None,
            skip_eval=True, skip_ddb=False, skip_alarms=False, skip_usage=False,
            skip_agentcore_eval=False, insights_poll=2.0, insights_max_wait=90, ddb_max_items=300):
    """Pull fresh telemetry into ``out_dir`` (default ``IMPROVE_HARVEST_DIR``).

    Bounded on purpose for the 15-minute Lambda ceiling: ``hours`` defaults to 24 (one Insights
    day-window, one round trip) and ``skip_eval`` defaults True because live mode re-judges and
    never reads the recorded eval scores. DDB runs BEFORE session reconstruction — plan-state
    rows are the span-independent half of each session's window.

    Token usage costs ONE extra Insights query per day-window (server-side ``stats by sid,
    model``), so ~2-4s at the deployed 24h setting. ``skip_usage`` exists for the same reason
    ``skip_alarms`` does — to take a cheaper harvest — and defaults False because spend is the
    point of the exercise.

    ``skip_agentcore_eval`` is NOT ``skip_eval``: that one is the raw judge-explanation dump
    nothing reads; this is one aggregated query whose per-session scores the model cards show.
    A failure there costs the cards their AgentCore rows, never the harvest.
    """
    out_dir = out_dir or default_out_dir()
    end_s = int(time.time())
    start_s = end_s - int(hours) * 3600
    harvested_at = datetime.now(timezone.utc).isoformat()
    write_json(out_dir, "raw/harvest_meta.json", {
        "harvested_at": harvested_at, "window_hours": hours, "region": region,
        "note": "read-only harvest; harvest_live.py (in-cloud boto3 port)",
    })

    aws = _Aws(region=region, profile=profile)
    account = ""
    try:
        account = aws.client("sts").get_caller_identity().get("Account", "")
    except Exception:  # noqa: BLE001 — identity is advisory
        pass

    eval_rows = [] if skip_eval else harvest_eval(aws, out_dir, start_s, end_s)
    # `clipped` is the list of intervals the row cap defeated. It travels to
    # `reconstruct_sessions` rather than being logged and dropped, because a session that overlaps
    # one has a KNOWN-partial trajectory and every downstream reader has to be able to tell.
    spans, clipped = harvest_spans(aws, out_dir, start_s, end_s)
    # `None` and not `{}` when skipped, and the difference is load-bearing downstream: `{}` is a
    # query that ran and found no usage, which lets `reconstruct_sessions` write a MEASURED
    # shortfall of zero, while `None` is a query nobody ran and must stay unmeasured.
    usage = None if skip_usage else harvest_token_usage(aws, start_s, end_s)
    ddb_items = {} if skip_ddb else harvest_ddb(aws, out_dir, max_items=ddb_max_items)
    agentcore_eval = None if skip_agentcore_eval else harvest_agentcore_eval(aws, start_s, end_s)
    reconstruct_sessions(out_dir, spans, eval_rows,
                         plan_rows=ddb_items.get("plan-state"), recorded_at=harvested_at,
                         usage=usage, clipped=clipped, agentcore_eval=agentcore_eval)
    if not skip_alarms:
        harvest_alarms(aws, out_dir, start_s, end_s, account=account, region=region)
    print(f"\nDONE. Harvest under {out_dir}")
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="output dir (default: $IMPROVE_HARVEST_DIR or /tmp/improve-harvest)")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--with-eval", action="store_true", help="also pull online-eval scores (informational in live mode)")
    ap.add_argument("--skip-ddb", action="store_true")
    ap.add_argument("--skip-alarms", action="store_true")
    ap.add_argument("--skip-usage", action="store_true", help="skip the per-session token-usage query")
    ap.add_argument("--skip-agentcore-eval", action="store_true",
                    help="skip the per-session AgentCore online-eval score query")
    args = ap.parse_args(argv)
    harvest(out_dir=args.out, hours=args.hours, region=args.region, profile=args.profile,
            skip_eval=not args.with_eval, skip_ddb=args.skip_ddb, skip_alarms=args.skip_alarms,
            skip_usage=args.skip_usage, skip_agentcore_eval=args.skip_agentcore_eval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
