"""Reconstruct a Run's cross-runtime trajectory from OTEL spans.

Worker tool calls (task/render/planning/design) are not in AgentCore Memory on
the internal path but are in the ``aws/spans`` store, stitched by ``session.id``.
Two sources behind one protocol: ``HarvestedSpanSource`` (offline JSON, for
golden-set work) and ``LiveSpanSource`` (read-only Logs Insights).

Sub-sessions minted as ``plan-session-<epoch>-<uuid>`` are children of a parent run
and are merged into it, never emitted standalone. Only the PREFIX is matched on —
the suffix was lengthened so the id clears AgentCore's ``runtimeSessionId`` minimum
length and can be propagated.

WHICH parent is answered by ``parents_by_session``, from the OTEL trace the harvest
records per session. The suffix does NOT carry it and never did; see that function.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional, Protocol

try:
    from ..contract import NO_DATA, UNKNOWN, Step, parse_utc
except ImportError:
    from improve.contract import NO_DATA, UNKNOWN, Step, parse_utc  # type: ignore

PLAN_SESSION_PREFIX = "plan-session-"
SPANS_LOG_GROUP = "aws/spans"


def parents_by_session(sessions: dict) -> dict:
    """``{sub_session_id: parent_session_id}`` for every sub-session a trace links to a parent.

    The join, in one place, over the ``traces`` list ``harvest_live.reconstruct_sessions`` writes
    per session. A plan sub-session's spans and the spans of the request that spawned it share an
    OTEL trace, so the trace IS the link — nothing has to be inferred from the id, from a time
    window, or from an upstream change.

    Measured on staging over 14 days (2026-09-02), which is what makes this exact rather than a
    heuristic:

        1,481 traces carrying 141 `plan-session-*` sub-sessions
          141 of 141 sat in a trace holding exactly ONE non-plan session   (0 unlinked)
            0 traces held two candidate parents
            0 sub-sessions appeared in more than one trace
           70 parents, 26 of them with MORE THAN ONE child, 6 on the busiest

    The trace TOTAL drifts with the window; none of the ratios below it did. Re-measure with
    `scripts/refresh_trace_fixtures.py`, which recomputes and prints them before it writes.

    What this REPLACED, and why it eroded rather than broke: the parent used to be taken as the
    id's own suffix (`run_id[len("plan-session-"):]`), which is unique per sub-session, so it
    named no parent and no two siblings ever shared one — 141 sub-sessions, 141 distinct values.
    The suffix was lengthened from `<epoch>` to `<epoch>-<32 hex>` so ids clear AgentCore's
    `runtimeSessionId` minimum length, which removed the last chance of a collision; but the
    epochs never collided either (measured: 0 epochs shared by two sub-sessions across both id
    formats), so the guard that de-duplicates siblings was inert under both.

    A session whose traces name no single parent is deliberately given NO entry at all. Two states
    are collapsed here on purpose, because neither yields a usable parent and inventing a third
    sentinel for a case measured at zero would buy nothing: a trace holding only sub-sessions, and
    a trace holding more than one candidate. ``parent_for`` reports both as "looked, none named".

    PURE, and takes the MERGED harvest so a parent read from one file can still claim a child
    read from another — the same reason ``HarvestedSpanSource`` merges at all.
    """
    by_trace: dict = {}
    for session_id, record in (sessions or {}).items():
        for trace in ((record or {}).get("traces") or ()):
            by_trace.setdefault(trace, set()).add(session_id)

    found: dict = {}
    for members in by_trace.values():
        children = {s for s in members if s.startswith(PLAN_SESSION_PREFIX)}
        candidates = members - children
        # Exactly one candidate, or nothing. `!= 1` covers the empty case and the ambiguous one
        # together, and picking from an ambiguous trace is the one thing that would turn an exact
        # link into a guess.
        if not children or len(candidates) != 1:
            continue
        for child in children:
            found.setdefault(child, set()).add(next(iter(candidates)))
    # A child seen in two traces that disagree has no single parent either, and must not silently
    # take whichever the iteration reached last.
    return {child: next(iter(parents)) for child, parents in found.items() if len(parents) == 1}


def harvest_dir() -> str:
    """Root of the offline harvest, kept outside the repo. ``IMPROVE_HARVEST_DIR`` wins.

    One definition, imported by every offline source in this package. Previously each module
    walked up to ``ideas/staging-data`` itself and only some honored the override, so pointing
    the loop at a different harvest moved some reads and silently left others behind.
    """
    override = os.environ.get("IMPROVE_HARVEST_DIR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    return os.path.join(os.path.dirname(repo_root), "ideas", "staging-data")


def existing_harvests(*paths: str) -> list[str]:
    """Every harvest file that exists, oldest first so a later one wins on conflict.

    MERGED, not chosen. Two rules were tried and both lost data:

      "prefer the file called fresh"   a narrow Aug-13 `fresh/` shadowed a 44-session Aug-25
                                      harvest, so the pipeline mined six sessions while the new
                                      ones sat unread. A filename is not evidence of freshness.

      "newest mtime wins"             the Aug-25 harvest REPLACED rather than extended, so all
                                      six golden sessions lost their evidence and the labelled
                                      benchmark stopped resolving at all.

    A session's evidence is wherever it exists. Merging means the benchmark keeps resolving from
    the older file while new sessions resolve from the newer one, and a re-harvest of the same
    session updates it rather than duplicating.
    """
    return sorted((p for p in paths if os.path.exists(p)), key=os.path.getmtime)


def default_sessions_paths() -> list[str]:
    """Every sessions harvest to merge, oldest first. `IMPROVE_HARVEST_SESSIONS` pins one."""
    override = os.environ.get("IMPROVE_HARVEST_SESSIONS")
    if override:
        return [override]
    return existing_harvests(
        os.path.join(harvest_dir(), "fresh", "sessions-fresh.json"),
        os.path.join(harvest_dir(), "sessions", "sessions.json"),
    )


def default_sessions_path() -> str:
    """The newest sessions harvest. Prefer `default_sessions_paths`, which loses nothing."""
    paths = default_sessions_paths()
    return paths[-1] if paths else os.path.join(harvest_dir(), "sessions", "sessions.json")


#: The harvest's own audit of token usage it MEASURED but could not attach to a trajectory,
#: relative to the harvest root. Named here rather than at either end because the writer
#: (``harvest_live._usage_shortfall``) and the reader (``build_run.harvest_usage_shortfall``) are in
#: different modules and a hand-copied path is how the two would come to disagree about one file.
#:
#: Its OWN file rather than a key inside ``sessions/sessions.json``: every reader of that file
#: iterates it as ``{session id: record}``, so a reserved key would arrive as a session with no
#: spans. Not merged across harvests either, unlike the sessions files — two audits describe two
#: different windows and adding them would invent a measurement neither took.
USAGE_AUDIT_RELPATH = os.path.join("sessions", "usage_audit.json")


def default_usage_audit_path() -> str:
    """Where the usage-shortfall audit lives for the harvest currently in play."""
    return os.path.join(harvest_dir(), USAGE_AUDIT_RELPATH)


class TrajectorySource(Protocol):
    """Yields raw span dicts {ts, runtime, name, status, msg, duration_ms} for a session.

    ``completeness_for`` is deliberately NOT part of the protocol: only a source that read a
    harvest can know whether the read was clipped, and `LiveSpanSource` (one query, one session)
    cannot. `build_run` therefore probes for the method and falls back to UNKNOWN, which is the
    correct answer for a source that cannot say — see `completeness_for` below.
    """

    def spans_for(self, session_id: str) -> list[dict]: ...


class HarvestedSpanSource:
    """Reads spans from the local harvests (offline, deterministic).

    Every harvest is MERGED rather than one chosen, so a session resolves from whichever file
    holds it. See `existing_harvests` for the two rules that lost data before this.
    """

    def __init__(self, sessions_path: Optional[str] = None) -> None:
        self._paths = [sessions_path] if sessions_path else default_sessions_paths()
        # Kept for callers and tests that report which harvest is in play.
        self._path = self._paths[-1] if self._paths else ""
        self._cache: Optional[dict] = None
        # The trace join, computed once over the whole merged harvest. `build_run` asks per
        # session, and re-inverting every session's traces for each of them would make an O(n)
        # read O(n^2) on the population the producer scores in one Lambda invocation.
        self._parents: Optional[dict] = None

    def _load(self) -> dict:
        if self._cache is None:
            merged: dict = {}
            # Oldest first, so a re-harvest of the same session overwrites the earlier copy.
            for path in self._paths:
                with open(path) as f:
                    merged.update(json.load(f))
            self._cache = merged
        return self._cache

    def spans_for(self, session_id: str) -> list[dict]:
        session = self._load().get(session_id)
        return list(session["spans"]) if session else []

    def completeness_for(self, session_id: str) -> tuple:
        """``(evidence_complete, evidence_note)`` as the HARVESTER recorded it, or UNKNOWN.

        Read from the same merged dict `spans_for` reads, because the flag is written next to the
        spans it describes (`harvest_live.reconstruct_sessions`) and pointing this at a second file
        is how the two would come to disagree about the same session.

        UNKNOWN — never True — when the key is absent or not a bool. Every harvest taken before
        this existed is in that state, and defaulting those to True would assert that a truncation
        check ran and passed on evidence nobody checked. `bool` is required explicitly so a JSON
        `null`, or a note-shaped string, cannot arrive as a verdict.
        """
        session = self._load().get(session_id) or {}
        recorded = session.get("evidence_complete")
        note = str(session.get("evidence_note") or "")
        if not isinstance(recorded, bool):
            return UNKNOWN, note
        return recorded, note

    def parent_for(self, session_id: str):
        """The parent SESSION id this session's trace names, or a sentinel. Never a fabrication.

        The SESSION id and not the trace id, deliberately: this value reaches
        ``Verdict.workload_parent``, which a consumer both de-duplicates on and may want to look
        up, and a trace id resolves to nothing in this system — no store keys on it, and the UI
        has no route for it. Keying on the trace would also be subtly wrong in the other
        direction, because one request spans MANY traces (measured: up to 163 for a single
        conversation), so two sub-sessions of one request that happened to be dispatched in
        different traces would still read as independent.

        Three answers, and the difference between the first two is the whole point:

          UNKNOWN     nobody looked — this session is not in the harvest, or the harvest predates
                      the `traces` projection. Every harvest taken before that field existed is
                      in this state, and reporting it as "no parent" would silently re-arm the
                      inert de-duplication this exists to fix.
          NO_DATA     looked, and the trace names no single parent. A sub-session whose parent's
                      spans fell outside the harvest window is here, and so is any session that
                      is not a sub-session at all — a request has no parent by construction.
          a str       the parent's session id.
        """
        harvest = self._load()
        if self._parents is None:
            self._parents = parents_by_session(harvest)
        record = harvest.get(session_id)
        if not record or not record.get("traces"):
            return UNKNOWN
        return self._parents.get(session_id) or NO_DATA


class LiveSpanSource:
    """Queries CloudWatch Logs Insights against aws/spans (read-only)."""

    def __init__(self, region: str = "us-west-2", profile: Optional[str] = None,
                 lookback_hours: int = 24) -> None:
        self._region = region
        self._profile = profile
        self._lookback = lookback_hours

    def spans_for(self, session_id: str) -> list[dict]:
        import boto3
        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        logs = session.client("logs", region_name=self._region)
        end = int(time.time())
        start = end - self._lookback * 3600
        query = (
            'fields @timestamp, attributes.aws.local.service as svc, name, '
            'status.code as status, status.message as msg, durationNano '
            f'| filter attributes.session.id = "{session_id}" '
            '| filter svc like /mediaworks_harness/ '
            '| sort @timestamp asc | limit 5000'
        )
        query_id = logs.start_query(logGroupName=SPANS_LOG_GROUP, startTime=start,
                                    endTime=end, queryString=query)["queryId"]
        while True:
            time.sleep(1.5)
            result = logs.get_query_results(queryId=query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled"):
                break
        rows = []
        for row in result.get("results", []):
            fields = {f["field"]: f.get("value") for f in row}
            duration_nano = fields.get("durationNano")
            rows.append({
                "ts": fields.get("@timestamp", ""),
                "runtime": (fields.get("svc") or "").replace("mediaworks_harness_", "").replace(".DEFAULT", ""),
                "name": fields.get("name", ""),
                "status": fields.get("status", "UNSET"),
                "msg": fields.get("msg"),
                "duration_ms": (int(duration_nano) // 1_000_000) if duration_nano else None,
            })
        return rows

    # `total_tokens_for_session` used to live here. It is GONE rather than fixed, for two
    # independent reasons, both measured against staging on 2026-08-28:
    #
    #   WRONG   `filter ispresent(attributes.gen_ai.usage.total_tokens) | stats sum(total)`
    #           sums three overlapping span families — `chat` (one LLM call each),
    #           `chat <model id>` (a second instrumentation's span for the SAME calls) and
    #           `invoke_agent Strands Agents` (the ACCUMULATED total for the whole invocation).
    #           On all six golden sessions the result is EXACTLY 2x the truth.
    #
    #   SHAPED WRONG   it was per-SESSION, and the worker scores up to `IMPROVE_LIMIT` (60) of
    #           them while every Insights query costs at least one 2s poll. A single
    #           `stats ... by sid, model` is one round trip for the whole population.
    #
    # Both are fixed in `harvest_live.harvest_token_usage`, which writes per-session usage into
    # `sessions/sessions.json` for `build_run.HarvestedUsageSource` to read. Nothing called this
    # method, so nothing needed migrating — but see that function before writing a token query.


def _sort_key(raw: str):
    """Parsed timestamp for ordering, or None when unusable.

    Delegates to ``contract.parse_utc`` — the single timestamp rule for this package. The
    harvest carries offset-LESS span timestamps (``2026-08-13 21:39:22.278``) alongside the
    plan side's ``Z``/``+00:00`` mix, so all three must land on the same aware instant type.
    """
    return parse_utc(raw)


def _to_step(raw: dict) -> Step:
    duration = raw.get("duration_ms")
    return Step(
        ts=raw.get("ts", ""),
        runtime=raw.get("runtime", ""),
        name=raw.get("name", ""),
        status=raw.get("status", "UNSET"),
        error_message=raw.get("msg"),
        duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
    )


def trajectory_for_session(session_id: str, source: Optional[TrajectorySource] = None) -> list[Step]:
    """Ordered Step list for a session.

    Sorted on the PARSED timestamp, not the raw ISO string. String ordering is wrong across
    the two formats this system emits (``'…19.5Z' > '…19.5+00:00'`` because ``'Z' > '+'``) —
    the same bug `evaluators_improve._parse_ts` exists to fix on the plan side. Step order is
    what the async-lifecycle facets read, so mis-ordering here silently changes whether a
    dispatch appears to precede its retrieve. Unparseable timestamps sort last rather than
    raising, keeping their relative order.
    """
    src = source or HarvestedSpanSource()
    steps = [_to_step(r) for r in src.spans_for(session_id)]
    steps.sort(key=lambda s: (_sort_key(s.ts) is None, _sort_key(s.ts) or datetime.min.replace(tzinfo=timezone.utc)))
    return steps
