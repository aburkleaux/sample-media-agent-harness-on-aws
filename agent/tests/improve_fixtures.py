"""Real harvested fixtures for the offline improve tests (REQ-017).

REQ-017: "pure-logic tests use real harvested fixtures, never fabricated data."
The harvest itself lives OUTSIDE the repo (`ideas/staging-data`), so tests cannot
read it directly without breaking on any other checkout (OURS-2). `fixtures/
plan_state_golden.json` is the minimal real slice, committed: node ids, statuses,
timestamps, gateway_status and edges for the six golden sessions, plus their
ordered tool-span names.

Deliberately excluded from the fixture: node prompts, goal text, S3 URIs, tool
arguments and tool results. Arguments and results are sensitive (OURS-4), and none
of them is needed to exercise this logic.

Regenerate with `scripts/refresh_improve_fixtures.py` when the golden set changes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import (  # noqa: E402
    WINDOW_FROM_PLAN_STATE, Plan, PlanNode, RunWindow, Step, parse_utc,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "fixtures", "plan_state_golden.json")
_ASYNC_PATH = os.path.join(_HERE, "fixtures", "async_lifecycle_sessions.json")
_TRACE_PARENT_PATH = os.path.join(_HERE, "fixtures", "trace_parent_sessions.json")
_GOLDEN = os.path.join(_HERE, "..", "..", "tests", "evaluation",
                       "datasets", "golden", "golden.jsonl")


def _id(prefix: str) -> str:
    """Resolve a session PREFIX to its full id from golden.jsonl.

    Session ids are NEVER hardcoded here. An earlier version of this file wrote them
    out by hand and three of six were wrong — the fixture loader's prefix matching
    masked it, and the bug only surfaced when a full id was passed straight to
    build_run() and silently produced an empty Run (every judge tier scoring 0.0 on a
    blank prompt). Deriving from the dataset makes that class of error impossible.
    """
    for line in open(_GOLDEN):
        line = line.strip()
        if not line:
            continue
        sid = json.loads(line)["scenario_id"]
        if sid.startswith(prefix):
            return sid
    raise KeyError(f"no golden session with prefix {prefix!r}")


# The six golden sessions, named by the property each one pins. Resolved at import
# from golden.jsonl, so a dataset change cannot leave a stale literal behind.
FFAB73F8 = _id("ffab73f8")      # 6 nodes, 5 edges, ORDERING VIOLATION, label PASS
E2CB300B = _id("e2cb300b")      # 4 nodes, 3 edges, ORDERING VIOLATION, label FAIL
SIX309F154 = _id("6309f154")    # 2 nodes, failed node AND ordering violation, label PASS
A3A8FAC0 = _id("a3a8fac0")      # 2 nodes, failed node, label FAIL (dispatch tier)
ONEB42D744 = _id("1b42d744")    # 4 nodes, EDGELESS BY DESIGN (4 independent jobs) — clean reference
ABC839CA = _id("abc839ca")      # 4 nodes, edgeless


def _load() -> dict:
    with open(_PATH) as f:
        return json.load(f)["sessions"]


def _resolve(prefix: str) -> str:
    """Full session id from a prefix, so callers may pass either."""
    data = _load()
    if prefix in data:
        return prefix
    hits = [k for k in data if k.startswith(prefix[:8])]
    if not hits:
        raise KeyError(f"no fixture session matching {prefix!r}")
    return hits[0]


def real_plan(session: str) -> Plan:
    """The REAL plan-state rows for a golden session, as a contract Plan."""
    rows = _load()[_resolve(session)]["nodes"]
    return Plan(goal="", nodes=[
        PlanNode(id=r["node_id"], depends_on=list(r["depends_on"]),
                 status=r["status"], updated_at=r["updated_at"],
                 gateway_status=r["gateway_status"])
        for r in rows
    ])


def real_trajectory(session: str) -> list[Step]:
    """The REAL ordered spans for a golden session, as contract Steps: ALL of them.

    Carries ``status`` and ``duration_ms`` as recorded, not the ``Step`` defaults. A fixture
    that defaulted every status to ``UNSET`` could never exercise any status-dependent
    branch, and silently disagreed with production on the facets that read it — a fake looser
    than the real thing hides breakage rather than surfacing it.

    This used to return only the TOOL spans (11-70 per session) while production builds the
    trajectory from every span (164-541). That gap was documented and then bit: ``classify``
    reads non-tool spans (``chat``, ``execute_event_loop_cycle``, ``mcp tools/list``,
    ``POST /invocations``), so on the old fixture all six golden sessions classified as
    ``unclassified`` and every ``evaluate`` test scored UNSCORED. Use ``real_tool_names`` for
    the tool-only view. ``tests/test_improve_fixture_parity.py`` pins the agreement.
    """
    session_data = _load()[_resolve(session)]
    spans = session_data.get("spans")
    if spans is None:  # tool-only fixture; regenerate with scripts/refresh_improve_fixtures.py
        spans = session_data.get("tool_spans")
    if spans is None:  # pre-status fixture
        return [Step(name=n) for n in session_data.get("tool_span_names", [])]
    return [Step(name=s["name"], status=s.get("status", "UNSET"),
                 duration_ms=s.get("duration_ms"),
                 error_message=s.get("msg")) for s in spans]


def real_tool_trajectory(session: str) -> list[Step]:
    """Only the TOOL spans, as Steps. The slice, for tests about the slice.

    ``real_trajectory`` is production-faithful and therefore includes the ~90% of spans that
    are AWS SDK plumbing. A test asserting something about tool names specifically wants this
    one, so that its counts mean what they say.
    """
    session_data = _load()[_resolve(session)]
    spans = session_data.get("tool_spans")
    if spans is None:
        return [Step(name=n) for n in session_data.get("tool_span_names", [])]
    return [Step(name=s["name"], status=s.get("status", "UNSET")) for s in spans]


def real_tool_names(session: str) -> list[str]:
    """Just the tool spans' names, for the predicates that only ever read those."""
    return [s.name for s in real_tool_trajectory(session)]


def exemplar(workload_class: str) -> tuple:
    """A REAL harvested session of a given non-conversational class: ``(session_id, steps)``.

    The golden set is six conversations, so without these the objectives for the other
    classes could only be tested on a hand-built Run, which REQ-017 forbids and which would
    also not have caught the thing worth catching: that these sessions are ordinary and
    successful, not broken. Keys: ``tool_discovery``, ``control_plane``,
    ``control_plane_error``, ``plan_node``.
    """
    with open(_PATH) as f:
        exemplars = json.load(f).get("exemplars") or {}
    if workload_class not in exemplars:
        raise KeyError(f"no committed exemplar for {workload_class!r}; regenerate with "
                       "scripts/refresh_improve_fixtures.py")
    entry = exemplars[workload_class]
    return entry["session_id"], [
        Step(name=s["name"], status=s.get("status", "UNSET"),
             duration_ms=s.get("duration_ms"),
             error_message=s.get("msg")) for s in entry["spans"]]


def async_lifecycle_trajectory(prefix: str) -> list[Step]:
    """REAL ordered spans for one of the three async-lifecycle ground-truth sessions.

    These three are NOT golden-set members, so ``plan_state_golden.json`` cannot carry them,
    and they span a week so no single harvest window contains all three. They live in their
    own committed slice (``fixtures/async_lifecycle_sessions.json``, regenerate with
    ``scripts/refresh_async_lifecycle_fixture.py``) because they ARE the ground truth for the
    async-lifecycle facets: V-02a names ``36fafeb7`` and ``23a1fa24`` (both returned dispatch
    results INLINE and must not be accused of leaking), V-02c names ``74b0a063`` (retrieved
    early, then left a late dispatch unpaired at the session's end).

    ALL spans, in production order, exactly as ``real_trajectory`` does for the golden set —
    the tail test reads the total span count, so a tool-only slice would silently change the
    answer.
    """
    with open(_ASYNC_PATH) as f:
        sessions = json.load(f)["sessions"]
    if prefix not in sessions:
        raise KeyError(f"no async-lifecycle fixture for {prefix!r}; have {sorted(sessions)}")
    return [Step(name=s["name"], status=s.get("status", "UNSET"),
                 duration_ms=s.get("duration_ms")) for s in sessions[prefix]["spans"]]


def async_lifecycle_session_id(prefix: str) -> str:
    """The full session id behind an async-lifecycle fixture prefix."""
    with open(_ASYNC_PATH) as f:
        return json.load(f)["sessions"][prefix]["session_id"]


def fanout_span_rows(group: str = "fanout_1") -> tuple:
    """``(parent_session_id, [sub_session_ids], insights_span_rows)`` for a real fanned-out request.

    Raw Insights rows in the harvester's own projection, so a test can run the REAL producer over
    them rather than hand-writing what it thinks the producer writes. Two groups are committed:
    ``fanout_1`` is 6 sub-sessions of one request (and the sub-sessions are in the OLD
    ``plan-session-<epoch>`` id format), ``fanout_2`` is 5 in the current
    ``plan-session-<epoch>-<32 hex>`` format. Neither the golden set nor the async-lifecycle
    fixture contains a sub-session with a sibling, so this slice is its own file
    (``fixtures/trace_parent_sessions.json``, regenerate with
    ``scripts/refresh_trace_fixtures.py``).
    """
    with open(_TRACE_PARENT_PATH) as f:
        groups = json.load(f)["groups"]
    if group not in groups:
        raise KeyError(f"no fan-out fixture {group!r}; have {sorted(groups)}")
    entry = groups[group]
    return (entry["parent_session"], list(entry["sub_sessions"]),
            [dict(row) for row in entry["insights_span_rows"]])


def fanout_harvest(group: str = "fanout_1") -> tuple:
    """``(parent_session_id, [sub_session_ids], span_source)``: real producer, then real reader.

    Runs ``harvest_live.reconstruct_sessions`` over the committed rows and hands back a
    ``HarvestedSpanSource`` reading its output, which is the pair of components the parent link
    actually travels between. A test that built the sessions dict by hand would be asserting
    against its own idea of the harvest, and the field it needs (``traces``) is precisely the one
    the producer had to start writing.

    The output directory is temporary and unmanaged on purpose: nothing here is a durable artifact,
    and pointing it at the real harvest would overwrite evidence the rest of the suite reads.
    """
    parent, kids, rows = fanout_span_rows(group)
    from improve.harvest_live import reconstruct_sessions  # noqa: E402 — needs sys.path above
    from improve.observe.span_trajectory import HarvestedSpanSource  # noqa: E402

    out_dir = tempfile.mkdtemp(prefix="improve-fanout-")
    reconstruct_sessions(out_dir, rows, [], recorded_at="2026-09-02T00:00:00+00:00")
    return parent, kids, HarvestedSpanSource(os.path.join(out_dir, "sessions", "sessions.json"))


def real_window(session: str) -> RunWindow:
    """A golden session's wall-clock window from its REAL plan-state ``updated_at`` values.

    Labelled ``derived_plan_updated_at``, which is what it is — the same reconstruction
    ``ideas/staging-data/backfill_run_windows.py`` performs on the collection side, over the
    same real timestamps. Deliberately NOT ``collector_stamped``: no test should be able to
    manufacture an authoritative-looking window, because the whole point of the provenance
    field is that a reader can tell the difference.

    Note this is test-support, not production behaviour: ``build_run`` never derives a window
    (see its module docstring). It exists so the offline tests exercise the field on real
    timestamps rather than on invented ones (REQ-017).
    """
    # Sorted on the PARSED instant, never on the string: this harvest mixes `...Z` and
    # `...+00:00`, and as strings the same instant compares unequal ('Z' > '+').
    stamps = sorted(
        (stamp for stamp in (node.updated_at for node in real_plan(session).nodes)
         if parse_utc(stamp) is not None),
        key=parse_utc)
    if not stamps:
        raise KeyError(f"no usable plan-state timestamps in the fixture for {session[:8]}")
    return RunWindow(start=stamps[0], end=stamps[-1], provenance=WINDOW_FROM_PLAN_STATE,
                     note="improve_fixtures.real_window; plan-state updated_at bounds")
