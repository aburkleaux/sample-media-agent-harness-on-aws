"""The PRODUCER/READER seam: does what ``harvest_live`` WRITES match what the
``Harvested*`` sources READ?

``improve.harvest_live`` runs in the ``improve-worker`` Lambda and writes JSON under
``IMPROVE_HARVEST_DIR``. ``improve.observe.span_trajectory.HarvestedSpanSource``,
``improve.observe.build_run.HarvestedPlanSource`` / ``HarvestedWindowSource`` and
``improve.observe.infra_health.HarvestedAlarmSource`` read it back in the same invocation.
Nothing else joins them: no shared schema, no shared constant for the relative paths, no test.
If the shapes disagree the Lambda still exits 0 and publishes a SNAPSHOT built from an empty
or partly-sentinel population, which is indistinguishable from "the platform was fine".

So this module runs the REAL producer against a fake AWS and then the REAL readers against
its output. The fake is derived from the staging harvest (``tests/fixtures/
improve_harvest_live_aws.json``): the Insights rows, DynamoDB-JSON items and CloudWatch alarm
set are lifted verbatim from ``ideas/staging-data``, so the field names
(``@timestamp``/``sid``/``svc``/``status_msg``/``durationNano``, ``{"S": ...}``) are the real
ones rather than a guess. Only ONE transform is applied: every ``@timestamp`` is shifted by a
constant so the rows land inside the harvest window a Lambda would ask for. The stubs also
validate every request against botocore's own service models, so a call that boto3 would
reject cannot pass here (see ``tests/test_capability_registry_param_shapes.py`` for the same
rule applied to the Registry).

Two harvests are run, because they diverge:

  ALIGNED       spans and plan-state ``updated_at`` shifted together — the ordinary case,
                everything inside the harvest window.
  LONG_SESSION  spans shifted, plan-state rows left at their real (2-day-old) timestamps —
                a session whose plan rows were written in an earlier turn, before the harvest
                window opened. The DynamoDB scan is time-UNBOUNDED while Insights is not, so
                the reconstructed window starts outside the alarm coverage the same harvest
                declared, and infra health can only answer UNKNOWN.

What the assertions pin, beyond "it works": the fields that come back as sentinels and WHY,
so a later change that fixes one has to say so here. ``fresh/run-windows-fresh.json`` is read by
``build_run`` but written by NOTHING on the deploy path, so a session that depends on it has no
window in the cloud.

TOKENS ARE NO LONGER ONE OF THOSE FIELDS, and this file used to assert that they were. The old
assertions were ``resources['tokens'] is UNMEASURED`` plus ``not os.path.exists(fresh/
token-usage-fresh.json)``, both true and both describing a gap that is now closed:
``harvest_live.harvest_token_usage`` measures usage from the session's ``chat`` spans and
``reconstruct_sessions`` writes it into ``sessions/sessions.json`` beside ``window``, which is a
file the readers already open. So the deploy path now carries real tokens, and the assertion moved
from "absent" to "measured, per category, with the model it was spent on".

``fresh/token-usage-fresh.json`` is still never written here — but the reason it no longer matters
is worth stating, because it is the opposite of the obvious one. That file EXISTS on a laptop and
is **exactly 2x wrong**: it was built by summing ``gen_ai.usage.total_tokens`` over every span that
carried the attribute, and both ``chat`` (one LLM call) and ``invoke_agent Strands Agents`` (the
accumulated total for the whole invocation) carry it. So the old asymmetry was not "the laptop has
a number production lacks" but "the laptop publishes double the truth while production honestly
says UNMEASURED". ``build_run`` now REFUSES any usage record without a recognised ``provenance``,
which is what closes it in the honest direction — see ``_TRUSTED_USAGE_PROVENANCE``.

``cost`` is now an ESTIMATE rather than the sentinel: ``build_run`` carries hand-entered per-model
Bedrock rates and prices the four token categories from them, so the deploy path reports dollars
for any session whose every model has a rate row — and still reports UNMEASURED, all-or-nothing,
for one whose models it cannot all price. ``costBasis`` travels beside the figure, naming where the
rates came from and when.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve import harvest_live  # noqa: E402
from improve.contract import (  # noqa: E402
    NO_DATA, UNKNOWN, UNMEASURED, WINDOW_FROM_PLAN_AND_SPANS, WINDOW_FROM_SPANS,
    InfraHealth, InfraState, Plan, Run, RunWindow, Scenario, Step,
)
from improve.observe.build_run import (  # noqa: E402
    HarvestedPlanSource, HarvestedWindowSource, build_run, harvest_usage_shortfall,
)
from improve.observe.infra_health import (  # noqa: E402
    HarvestedAlarmSource, default_alarm_source, infra_health_for_window,
)
from improve.observe.population import harvest_population  # noqa: E402
from improve.classify import WorkloadClass, classify
from improve.evaluate import resolve_plan
from improve.observe.span_trajectory import (  # noqa: E402
    USAGE_AUDIT_RELPATH, HarvestedSpanSource, parents_by_session, trajectory_for_session,
)
from improve_fixtures import fanout_harvest, fanout_span_rows  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "improve_harvest_live_aws.json")

# The REAL table names, snapshotted before any test patches `DDB_TABLES`. `_build_fake_aws` keys
# the fake DynamoDB off THIS, not the live dict — otherwise `test_a_missing_table` patching
# `DDB_TABLES["plan-state"]` to a bogus name would also rename the fake's table to that same bogus
# name, so the scan would still find it and "absent table" could never be exercised.
_REAL_DDB_TABLES = dict(harvest_live.DDB_TABLES)

# The real offline harvest, for the live-vs-offline comparison. Outside the repo by design
# (OURS-2), so every test that touches it is skipped when it is absent.
OFFLINE_HARVEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "..", "ideas", "staging-data")

INSIGHTS_TS = "%Y-%m-%d %H:%M:%S.%f"   # offset-LESS UTC, exactly as Logs Insights returns it

# A plan harvest that does not exist. `HarvestedPlanSource` skips an absent file, so this pins the
# plan side to NO_DATA on purpose: the fan-out fixture carries spans only, and the DEFAULT source
# would read the out-of-repo harvest (OURS-2) and make these tests depend on the checkout.
_NO_PLAN_HARVEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures", "__absent-plan-harvest__.json")


def _fixture() -> dict:
    with open(FIXTURE) as f:
        return json.load(f)


# --------------------------------------------------------------------------------------
# fake AWS — real response shapes, botocore-validated requests
# --------------------------------------------------------------------------------------

def _validate(service: str, operation: str, params: dict) -> None:
    """Reject a request boto3 itself would reject. No creds, no network.

    A fake looser than the real API hides total breakage, so the stubs below refuse anything
    the vendored service model would refuse — a renamed or mistyped parameter fails here
    instead of at 3am in the Lambda.
    """
    import botocore.session
    from botocore.validate import ParamValidator
    shape = (botocore.session.get_session()
             .get_service_model(service).operation_model(operation).input_shape)
    report = ParamValidator().validate(params, shape)
    if report.has_errors():
        raise AssertionError(f"{service}.{operation} params rejected by botocore:\n"
                             f"{report.generate_report()}")


def _epoch(insights_ts: str) -> float:
    """Insights ``@timestamp`` -> epoch seconds. Offset-less and UTC (see contract.parse_utc)."""
    return datetime.strptime(insights_ts, INSIGHTS_TS).replace(tzinfo=timezone.utc).timestamp()


def _shift_insights_ts(raw: str, delta_s: float) -> str:
    moved = datetime.strptime(raw, INSIGHTS_TS) + timedelta(seconds=delta_s)
    return moved.strftime(INSIGHTS_TS)[:-3]        # keep milliseconds, as the real rows do


def _shift_ddb_ts(raw: str, delta_s: float) -> str:
    """Shift a plan-state ``updated_at``, PRESERVING its suffix style.

    The plan writer emits both ``...Z`` and ``...+00:00`` for the same field (both appear in
    the real rows for one session), and the difference is not cosmetic: compared as strings
    ``'…19.5Z' > '…19.5+00:00'``. Keeping both styles here is what makes the harvester's
    normalization actually under test.
    """
    zulu = raw.endswith("Z")
    moved = datetime.fromisoformat(raw.replace("Z", "+00:00")) + timedelta(seconds=delta_s)
    out = moved.astimezone(timezone.utc).isoformat()
    return out.replace("+00:00", "Z") if zulu else out


class _FakeLogs:
    """``logs`` with the two calls ``insights_query`` makes, time-filtered like the real API.

    The fake DISPATCHES ON THE QUERY, because the producer now runs two different ones against
    the same log group: the raw per-span projection and a server-side ``stats ... by sid, model``
    aggregation for token usage. Answering both with the span rows is what a fake looser than the
    real API looks like, and it hid a real defect — the usage parser coerced the missing fields to
    0 and wrote a trusted `{"total_tokens": 0}` for every session, i.e. absence arriving as a
    measured zero. See ``AGENTS.md``: "A fake looser than the real API hides total breakage."

    ``stats`` rows are NOT time-filtered here. Real Insights aggregates only records inside the
    window, but the usage fixture is a group ALREADY summed over a session's whole lifetime, so
    there is no per-row timestamp to filter on and splitting it would be inventing data. The
    windows the producer asked for are still recorded, so the per-window loop stays under test.

    The record query's ``| sort @timestamp asc | limit N`` IS honoured, and that is the difference
    between this fake and the one that hid the truncation bug for a month. A fake that returned
    every matching row could never produce a chunk AT the cap, so the whole clipping branch — the
    warning, and now the bisection that replaces it — was unreachable in the suite while the
    deployed producer was silently dropping two thirds of a busy day. See ``AGENTS.md``: "A fake
    looser than the real API hides total breakage."
    """

    def __init__(self, rows: list[dict], usage_rows: Optional[list[dict]] = None) -> None:
        self._rows = rows
        self._usage_rows = list(usage_rows or ())
        self._queries: dict[str, list[dict]] = {}
        self.windows: list[tuple[int, int]] = []
        self.usage_windows: list[tuple[int, int]] = []
        # The record queries' TEXT, so a test can assert what the producer projected rather than
        # only what came back. The two are different claims: the fake can only answer with fields
        # the fixture holds, while the query is what a real account would be asked for.
        self.span_queries: list[str] = []

    @staticmethod
    def _limit(query: str) -> Optional[int]:
        """The `| limit N` the caller actually asked for, or None. Parsed, not assumed.

        Read off the query string rather than off `harvest_live._INSIGHTS_ROW_CAP` so a test that
        patches the constant is genuinely testing that the constant reaches the query — patching a
        value the fake also reads independently would pass even if the query string still carried a
        hardcoded literal.
        """
        marker = "| limit "
        if marker not in query:
            return None
        try:
            return int(query.rsplit(marker, 1)[1].strip().split()[0])
        except (ValueError, IndexError):
            return None

    def start_query(self, **params):
        _validate("logs", "StartQuery", params)
        start, end = int(params["startTime"]), int(params["endTime"])
        query = params["queryString"]
        is_usage = "stats sum(attributes.gen_ai.usage.total_tokens)" in query
        (self.usage_windows if is_usage else self.windows).append((start, end))
        if not is_usage:
            self.span_queries.append(query)
        if is_usage:
            # One window's worth: the first, so a multi-window harvest does not multiply the
            # already-summed fixture groups by the number of windows.
            matched = self._usage_rows if len(self.usage_windows) == 1 else []
        else:
            # Insights only returns events inside the requested window. Reproducing that is the
            # whole point: it is the asymmetry with the time-unbounded DynamoDB scan.
            matched = [r for r in self._rows if start <= _epoch(r["@timestamp"]) < end]
            # ...then `sort @timestamp asc | limit N`, in that order, which is what makes the rows
            # a capped read KEEPS the oldest and the rows it LOSES the newest.
            if "sort @timestamp asc" in query:
                matched = sorted(matched, key=lambda r: r["@timestamp"])
            limit = self._limit(query)
            if limit is not None:
                matched = matched[:limit]
            # A field the query did not PROJECT does not come back. Insights answers with the
            # `fields` clause and nothing else, and a fake that volunteered the trace anyway could
            # not tell a producer that projects it from one that does not — which is the whole
            # defect this fixture was extended for. Enforced for `trace` alone rather than by
            # parsing the clause: it is the one field here whose projection is load-bearing and
            # newly added, and a general parser would be a second query language to maintain.
            if harvest_live.TRACE_FIELD not in query:
                matched = [{k: v for k, v in r.items() if k != "trace"} for r in matched]
        qid = f"q{len(self._queries) + 1}"
        self._queries[qid] = matched
        return {"queryId": qid}

    def get_query_results(self, **params):
        _validate("logs", "GetQueryResults", params)
        rows = self._queries[params["queryId"]]
        return {
            "status": "Complete",
            # An Insights result row is a LIST of {field, value} pairs, not a dict.
            "results": [[{"field": k, "value": v} for k, v in row.items()] for row in rows],
            "statistics": {"recordsMatched": float(len(rows)), "recordsScanned": float(len(rows))},
        }


class _FakeDynamoDB:
    """``dynamodb`` LOW-LEVEL client: pages of DynamoDB-JSON, which is what the reader expects."""

    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self._tables = tables

    def get_paginator(self, name):
        assert name == "scan"
        return _FakeScanPaginator(self._tables)


class _FakeScanPaginator:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self._tables = tables

    def paginate(self, **params):
        pagination = params.pop("PaginationConfig", None) or {}
        _validate("dynamodb", "Scan", params)
        table = params["TableName"]
        if table not in self._tables:
            # Exactly what a real scan of an absent table raises, and the branch
            # `harvest_ddb` catches to keep going.
            raise RuntimeError(f"ResourceNotFoundException: Requested resource not found: "
                               f"Table: {table} not found")
        rows = self._tables[table]
        limit = pagination.get("MaxItems")
        if limit is not None:
            rows = rows[:limit]
        # Two pages, so the paginator loop is genuinely exercised.
        half = max(1, len(rows) // 2)
        return iter([{"Items": rows[:half], "Count": half, "ScannedCount": half},
                     {"Items": rows[half:], "Count": len(rows) - half,
                      "ScannedCount": len(rows) - half}])


class _FakeCwPaginator:
    """A botocore paginator, minus the wire. Two pages, so NextToken-following is exercised."""

    def __init__(self, owner: "_FakeCloudWatch", operation: str, pages: list[dict]) -> None:
        self._owner = owner
        self._operation = operation
        self._pages = pages

    def paginate(self, **params):
        config = params.pop("PaginationConfig", None)
        assert config is None or "MaxItems" in config
        _validate("cloudwatch", self._operation, params)
        self._owner.calls.append((self._operation, params, config))
        return iter(self._pages)


class _FakeCloudWatch:
    """``cloudwatch`` with real response types: ``StateUpdatedTimestamp``/``Timestamp`` are
    ``datetime`` objects, as botocore deserializes them — NOT strings.

    ``harvest_alarms`` reaches CloudWatch only through paginators, and BOTH operations really
    are pageable in the vendored model (``can_paginate`` is True for
    ``describe_alarms``/``describe_alarm_history``), so an unpageable-operation error is not a
    risk here. ``describe_alarms``/``describe_alarm_history`` are left off this fake on
    purpose: the direct calls no longer exist in the producer, and a fake that still answers
    them would hide a revert.
    """

    _PAGINATORS = {"describe_alarms": "DescribeAlarms",
                   "describe_alarm_history": "DescribeAlarmHistory"}

    def __init__(self, metric_alarms: list[dict], history_items: list[dict]) -> None:
        half = max(1, len(metric_alarms) // 2)
        self._alarm_pages = [
            {"MetricAlarms": metric_alarms[:half], "CompositeAlarms": []},
            {"MetricAlarms": metric_alarms[half:], "CompositeAlarms": []},
        ]
        self._history_pages = [{"AlarmHistoryItems": history_items}]
        self.calls: list[tuple] = []

    def get_paginator(self, name):
        return _FakeCwPaginator(self, self._PAGINATORS[name],
                                self._alarm_pages if name == "describe_alarms"
                                else self._history_pages)


class _FakeSts:
    def get_caller_identity(self, **params):
        return {"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/x/y",
                "UserId": "AROAEXAMPLE:improve-worker"}


class _FakeSession:
    def __init__(self, clients: dict) -> None:
        self._clients = clients

    def client(self, name, region_name=None, **_):
        assert region_name, "harvest_live must pin a region on every client"
        return self._clients[name]


def _build_fake_aws(delta_s: float, shift_plan_rows: bool, history_items=None):
    fix = _fixture()
    rows = [{**r, "@timestamp": _shift_insights_ts(r["@timestamp"], delta_s)}
            for r in fix["insights_span_rows"]]

    plan_items = []
    for item in fix["ddb_plan_state_items"]:
        item = json.loads(json.dumps(item))          # deep copy; keep the real row intact
        if shift_plan_rows and "updated_at" in item:
            item["updated_at"]["S"] = _shift_ddb_ts(item["updated_at"]["S"], delta_s)
        plan_items.append(item)

    alarms = []
    for alarm in fix["cw_metric_alarms"]:
        alarms.append({**alarm,
                       "StateUpdatedTimestamp": datetime.fromisoformat(
                           alarm["StateUpdatedTimestamp"])})

    logs = _FakeLogs(rows, fix.get("insights_usage_rows"))
    cw = _FakeCloudWatch(alarms, list(history_items or []))
    ddb = _FakeDynamoDB({_REAL_DDB_TABLES["plan-state"]: plan_items})
    return _FakeSession({"logs": logs, "dynamodb": ddb, "cloudwatch": cw, "sts": _FakeSts()}), \
        {"logs": logs, "cloudwatch": cw}


def _run_harvest(monkeypatch, out_dir: str, *, delta_s: float, shift_plan_rows: bool,
                 hours: int = 24, history_items=None):
    """Run the REAL ``harvest_live.harvest`` against the fake AWS, into ``out_dir``."""
    session, handles = _build_fake_aws(delta_s, shift_plan_rows, history_items)
    monkeypatch.setattr(harvest_live.boto3, "Session", lambda *a, **k: session)
    # `harvest()` accepts `insights_poll`/`insights_max_wait` but never forwards them, so the
    # only way to keep the 2s-per-poll sleep out of the suite is to stub the clock.
    monkeypatch.setattr(harvest_live.time, "sleep", lambda _s: None)
    harvest_live.harvest(out_dir=out_dir, hours=hours, region="us-west-2")
    return handles


def _point_readers_at(monkeypatch, path: str) -> None:
    monkeypatch.setenv("IMPROVE_HARVEST_DIR", path)
    monkeypatch.delenv("IMPROVE_HARVEST_SESSIONS", raising=False)
    monkeypatch.delenv("IMPROVE_INFRA_ALARMS", raising=False)


def _delta_to_now(fix: dict, end_offset_s: float = 900.0) -> float:
    """Shift that lands the LAST fixture span ``end_offset_s`` seconds before now."""
    latest = max(_epoch(r["@timestamp"]) for r in fix["insights_span_rows"])
    return datetime.now(timezone.utc).timestamp() - end_offset_s - latest


def _delta_placing_session_start(fix: dict, sid: str, seconds_before_now: float) -> float:
    """Shift that puts ONE session's first span ``seconds_before_now`` seconds ago.

    Used to straddle a harvest boundary on purpose: the session starts before the window opens
    and finishes inside it, which is what a run in progress at harvest time looks like.
    """
    first = min(_epoch(r["@timestamp"]) for r in fix["insights_span_rows"] if r["sid"] == sid)
    return datetime.now(timezone.utc).timestamp() - seconds_before_now - first


def _describe(value) -> str:
    if value is NO_DATA:
        return "NO_DATA"
    if value is UNKNOWN:
        return "UNKNOWN"
    if value is UNMEASURED:
        return "UNMEASURED"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, Plan):
        return f"Plan(goal={bool(value.goal)}, nodes={len(value.nodes)}, edges_available={value.edges_available})"
    if isinstance(value, RunWindow):
        return f"RunWindow({value.provenance}, {value.duration_s():.0f}s)"
    if isinstance(value, InfraHealth):
        return f"InfraHealth({value.state.value})"
    if isinstance(value, str):
        return f"str[{len(value)}]" if len(value) > 40 else repr(value)
    return repr(value)


def _run_field_map(run) -> dict:
    return {
        "run_id": _describe(run.run_id), "goal": _describe(run.goal),
        "response": _describe(run.response), "trajectory": _describe(run.trajectory),
        "plan": _describe(run.plan), "turns": _describe(run.turns),
        "resources.latency_ms": _describe(run.resources.get("latency_ms")),
        "resources.tokens": _describe(run.resources.get("tokens")),
        "resources.cost": _describe(run.resources.get("cost")),
        "harness_snapshot": _describe(run.harness_snapshot),
        "window": _describe(run.window), "infra_health": _describe(run.infra_health),
        "parent_run_id": _describe(run.parent_run_id),
    }


# --------------------------------------------------------------------------------------
# 1. the files land where the readers look
# --------------------------------------------------------------------------------------

def test_harvest_writes_exactly_seven_files_and_no_fresh_dir(tmp_path, monkeypatch):
    """The file list, pinned. (Named "five" until it wasn't — six, then seven, then eight, now seven.)

    ``sessions/usage_audit.json`` is a FILE rather than a log line because the number in it —
    measured spend the row cap left with no trajectory — is missing from a spend figure that reads as
    complete, and the reader of that figure is not tailing the worker.

    It went back DOWN by one: ``ddb/job-registry.json`` was written nightly off a 2,157-row scan and
    opened by nothing, so the scan and the file are gone. That is what this list is for — a written
    file with no reader is invisible everywhere except here.
    """
    fix = _fixture()
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)

    written = sorted(os.path.relpath(os.path.join(root, name), out)
                     for root, _dirs, files in os.walk(out) for name in files)
    assert written == ["alarms/alarm-history.json",
                       "ddb/plan-state.json", "raw/harvest_meta.json",
                       "sessions/sessions.json", "sessions/usage_audit.json",
                       "spans/spans_all.json", "spans/spans_meta.json"]
    # No `fresh/` at all — the readers' FIRST-choice path never exists in the cloud. Every
    # `fresh/*` consumer therefore falls through to its second path, or to a sentinel:
    #   fresh/sessions-fresh.json    -> sessions/sessions.json          (works)
    #   fresh/plan-state-fresh.json  -> ddb/plan-state.json             (works)
    #   fresh/alarm-history-fresh.json -> alarms/alarm-history.json     (works)
    #   fresh/run-windows-fresh.json -> no fallback; the sessions harvest carries `window`
    #   fresh/token-usage-fresh.json -> no fallback -> tokens/cost UNMEASURED forever
    #   fresh/nodes-json/<key>       -> no fallback -> edges_available=False for offloaded plans
    assert not os.path.isdir(os.path.join(out, "fresh"))


def test_harvest_meta_and_span_dump_are_written_but_read_by_nothing(tmp_path, monkeypatch):
    fix = _fixture()
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    meta = json.load(open(os.path.join(out, "raw", "harvest_meta.json")))
    assert set(meta) == {"harvested_at", "window_hours", "region", "note"}
    # The offline harvester records `account` here too; nothing reads either file, so this is
    # provenance drift rather than a break.
    assert "account" not in meta
    raw_spans = json.load(open(os.path.join(out, "spans", "spans_all.json")))
    assert raw_spans and set(raw_spans[0]) >= {"@timestamp", "sid", "svc", "name", "durationNano"}
    # `spans/spans_meta.json` is the same kind of file: forensics, read by no reader. It exists so
    # "why does this session look truncated" is answerable a week later, and it is where the
    # acceptance check for adaptive windowing looks (`clipped_windows` empty or fully accounted).
    meta = json.load(open(os.path.join(out, "spans", "spans_meta.json")))
    assert set(meta) == {"harvested_at", "requested", "bounds", "queries", "windows_read",
                         "clipped_windows"}
    assert meta["bounds"] == {"row_cap": harvest_live._INSIGHTS_ROW_CAP,
                              "seed_window_s": harvest_live._INSIGHTS_WINDOW_SECONDS,
                              "min_window_s": harvest_live._MIN_SPAN_WINDOW_S,
                              "max_queries": harvest_live._INSIGHTS_MAX_QUERIES}
    # 49 fixture rows in a 24h window is nowhere near the cap, so one query and no clipping.
    assert meta["queries"] == 1 and meta["clipped_windows"] == []
    assert sum(w["rows"] for w in meta["windows_read"]) == len(raw_spans)


# --------------------------------------------------------------------------------------
# 2. the readers, against what the producer just wrote
# --------------------------------------------------------------------------------------

def test_span_source_reads_harvested_sessions_and_all_six_step_fields_survive(tmp_path, monkeypatch, capsys):
    fix = _fixture()
    sess = fix["sessions"]
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    _point_readers_at(monkeypatch, out)

    source = HarvestedSpanSource()
    assert source._paths == [os.path.join(out, "sessions", "sessions.json")]

    sid = sess["plan_and_spans_offloaded_edges"]     # the session that has ERROR spans
    raw = source.spans_for(sid)
    assert raw, "HarvestedSpanSource read nothing out of the live harvest"
    assert set(raw[0]) == {"ts", "runtime", "name", "status", "msg", "duration_ms"}

    steps = trajectory_for_session(sid, source=source)
    assert steps and all(isinstance(s, Step) for s in steps)
    # All six span fields make it from the Insights row into Step.
    assert all(s.ts and s.name and s.status for s in steps)
    # `svc` -> `runtime` keeps the cross-runtime stitch: the prefix and `.DEFAULT` are stripped
    # and both runtimes this session touched survive, which is what `multi_runtime` reads.
    assert {s.runtime for s in steps} == {"main_agent", "render_agent"}
    assert all(s.duration_ms is not None and s.duration_ms >= 0 for s in steps)
    errored = [s for s in steps if s.status == "ERROR"]
    assert errored and errored[0].error_message, "status.message did not survive as error_message"
    # Sorted on the parsed timestamp.
    assert [s.ts for s in steps] == sorted(s.ts for s in steps)

    print(f"\n[spans] {sid}: {len(steps)} steps, runtimes={sorted({s.runtime for s in steps})}, "
          f"errors={len(errored)}")
    print(f"[spans] first step: {steps[0]}")
    print(f"[spans] error step: {errored[0]}")


def test_plan_source_reads_harvested_ddb_json_inline_and_offloaded(tmp_path, monkeypatch):
    fix = _fixture()
    sess = fix["sessions"]
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    _point_readers_at(monkeypatch, out)

    plans = HarvestedPlanSource()
    assert plans._paths == [os.path.join(out, "ddb", "plan-state.json")]

    inline = plans.plan_for(sess["plan_and_spans_inline_edges"])
    assert isinstance(inline, Plan)
    assert inline.goal, "the PLAN row's goal did not survive the DynamoDB-JSON round trip"
    assert len(inline.nodes) == 5 and any(n.id == "PLAN" for n in inline.nodes)
    assert inline.edges_available is True
    assert any(n.gateway_status for n in inline.nodes)
    assert all(n.updated_at for n in inline.nodes)
    # This session's plan is genuinely a parallel fan-out (`depends_on: []` on every node in
    # `nodes_json`), so an edgeless Plan here is a FACT and `edges_available` says so. Note the
    # node rows also outnumber `nodes_json`: two of them (`shot_segmentation`, `face_recognition`)
    # belong to a SUPERSEDED plan in the same session, because the PLAN row is overwritten
    # per-turn while node rows are not. Nothing in the seam notices.
    assert not any(n.depends_on for n in inline.nodes)
    assert {n.id for n in inline.nodes} - {"PLAN"} > {"sg1_shot_segmentation", "sg2_face_recognition"}

    # A plan that really does have edges: they are backfilled from `nodes_json` onto the rows.
    edged = plans.plan_for(sess["plan_with_real_edges"])
    assert isinstance(edged, Plan) and edged.edges_available is True
    assert {n.id: n.depends_on for n in edged.nodes if n.depends_on} == {
        "inspect_vehicles": ["extract_frames"]}

    # Same shape, but this plan's node list was offloaded to S3 (>300KB inline cap). The
    # reader looks for `fresh/nodes-json/<basename>` — which harvest_live never writes — and
    # has no bucket configured, so the edges are UNAVAILABLE, not absent.
    offloaded = plans.plan_for(sess["plan_and_spans_offloaded_edges"])
    assert isinstance(offloaded, Plan)
    assert offloaded.edges_available is False
    assert offloaded.nodes and not any(n.depends_on for n in offloaded.nodes)

    assert plans.plan_for(sess["spans_only"]) is NO_DATA


def test_window_source_reads_the_window_harvest_live_stamped(tmp_path, monkeypatch, capsys):
    fix = _fixture()
    sess = fix["sessions"]
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    _point_readers_at(monkeypatch, out)

    windows = HarvestedWindowSource()
    both = windows.window_for(sess["plan_and_spans_inline_edges"])
    assert isinstance(both, RunWindow)
    assert both.provenance == WINDOW_FROM_PLAN_AND_SPANS
    assert both.derived() is True and both.recorded_at and "harvest_live.py" in both.note
    assert both.duration_s() > 0

    span_only = windows.window_for(sess["spans_only"])
    assert isinstance(span_only, RunWindow)
    assert span_only.provenance == WINDOW_FROM_SPANS
    assert "no plan-state rows" in span_only.note

    assert windows.window_for("never-harvested-session") is UNKNOWN
    print(f"\n[window] plan+spans: {both}")
    print(f"[window] spans only : {span_only}")


def test_alarm_source_reads_the_alarm_file_harvest_live_wrote(tmp_path, monkeypatch, capsys):
    fix = _fixture()
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    _point_readers_at(monkeypatch, out)

    source = default_alarm_source()
    assert isinstance(source, HarvestedAlarmSource)
    assert source._path == os.path.join(out, "alarms", "alarm-history.json")
    start, end = source.coverage()
    assert start is not None and end is not None, "coverage did not survive harvest_live"

    window = HarvestedWindowSource().window_for(fix["sessions"]["plan_and_spans_inline_edges"])
    health = infra_health_for_window(window, source=source)
    lookup = source.alarms_in_window(window.start_dt(), window.end_dt())
    assert len(lookup.alarms) == 10, "the alarm records did not round-trip"

    # The real staging account holds one chronically-firing alarm and three in
    # INSUFFICIENT_DATA, none with a retained transition. `_state_over_window` calls a chronic
    # ALARM indeterminate (nothing ties it to this run) and INSUFFICIENT_DATA indeterminate
    # too, so the honest answer is UNKNOWN, not HEALTHY. That is the source data, not a seam
    # defect — the file crossed the seam intact.
    assert health.state is InfraState.UNKNOWN
    assert "could not be placed" in health.reason
    assert health.window is window and "DERIVED" in health.reason
    print(f"\n[alarms] coverage {start.isoformat()} .. {end.isoformat()}")
    print(f"[alarms] state={health.state.value} source={health.source}")
    print(f"[alarms] reason={health.reason}")


def test_state_updated_at_drifts_from_the_offline_harvester(tmp_path, monkeypatch):
    """boto3 hands back a ``datetime``; ``str()`` of one is not the CLI's ISO string.

    Nothing reads ``state_updated_at``, so this is recorded rather than fixed — but the two
    harvesters no longer write the same file for the same account, which is exactly the kind
    of silent divergence this seam has no other guard against.
    """
    fix = _fixture()
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    alarms = json.load(open(os.path.join(out, "alarms", "alarm-history.json")))["alarms"]
    stamps = [a["state_updated_at"] for a in alarms]
    assert all(" " in s and "T" not in s for s in stamps), stamps[:2]


def test_build_run_every_field_from_the_live_harvest(tmp_path, monkeypatch, capsys):
    fix = _fixture()
    sess = fix["sessions"]
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    _point_readers_at(monkeypatch, out)

    sid = sess["plan_and_spans_inline_edges"]
    # The population the Lambda mines is DISCOVERED from the harvest: goal="" and no labels.
    scenarios = harvest_population()
    discovered = [s for s in scenarios if s.source_session == sid]
    assert discovered, "harvest_population did not discover the session harvest_live wrote"
    run = build_run(discovered[0])

    fields = _run_field_map(run)
    print("\n[build_run] " + json.dumps(fields, indent=2))

    # REAL, carried across the seam:
    assert run.run_id == sid
    assert isinstance(run.trajectory, list) and len(run.trajectory) >= 5
    assert isinstance(run.plan, Plan) and run.plan.nodes
    assert isinstance(run.window, RunWindow)
    assert isinstance(run.infra_health, InfraHealth)
    assert isinstance(run.resources["latency_ms"], int) and run.resources["latency_ms"] > 0

    # SENTINELS, each with a named cause:
    #  goal      — Scenario.goal is "" for a discovered session and Memory is the only other
    #              source; build_run never reads the PLAN row's goal, which IS in the harvest
    #              (run.plan.goal below), so this one is recoverable and simply not wired.
    assert run.goal is NO_DATA and run.plan.goal
    #  response/turns — AgentCore Memory, not the harvest. harvest_live pulls no conversation
    #              at all, so with no MemoryReader injected the judge has no answer to grade.
    assert run.response is NO_DATA and run.turns == []
    #  harness_snapshot — hardcoded UNKNOWN in build_run (M1).
    assert run.harness_snapshot is UNKNOWN
    #  parent_run_id — NO_DATA and not UNKNOWN, and the difference is the point: this session's
    #  traces WERE read (see below), and a request has no parent by construction. UNKNOWN here
    #  would mean the harvest carried no trace projection at all.
    assert run.parent_run_id is NO_DATA

    # TOKENS: real, and REAL PER CATEGORY. This is the assertion that replaced
    # `run.resources["tokens"] is UNMEASURED` — see the module docstring for why the thing it
    # replaced was describing a gap AND a 2x-wrong laptop-only file.
    usage = {r["sid"]: r for r in fix["insights_usage_rows"]}
    # a8c489e7 ran on TWO models, so its total is the sum of both groups — which is exactly why
    # the harvest records `byModel` and why a one-rate cost would be wrong.
    expected = [r for r in fix["insights_usage_rows"] if r["sid"] == sid]
    assert len(expected) == 2, "the fixture's own multi-model session, kept on purpose"
    assert run.resources["tokens"] == sum(int(r["total_tokens"]) for r in expected)
    assert run.resources["output_tokens"] == sum(int(r["output_tokens"]) for r in expected)
    assert run.resources["cache_read_tokens"] == sum(int(r["cache_read_tokens"]) for r in expected)
    assert run.resources["cache_write_tokens"] == sum(int(r["cache_write_tokens"]) for r in expected)
    assert run.resources["llm_calls"] == sum(int(r["llm_calls"]) for r in expected)
    # Uncached input is DERIVED (total - output - cacheRead - cacheWrite), because the recorded
    # `input_tokens` attribute changed meaning mid-population. On real rows it is a handful of
    # tokens per call, never the whole prompt.
    assert 0 < run.resources["input_tokens"] < run.resources["cache_read_tokens"]
    assert run.resources["models"] == tuple(sorted(r["model"] for r in expected))

    #  cost — an ESTIMATE now rather than the sentinel, priced per model from hand-entered Bedrock
    #  rates. Recomputed here from the fixture's own two groups at rates spelled out as LITERALS, so
    #  a mistyped rate in `build_run` fails this seam as well as its own unit test rather than
    #  agreeing with itself. Uncached input is derived the way `harvest_live` derives it.
    rates = {"global.anthropic.claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
             "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.00, 5.00, 0.10, 1.25)}
    by_hand = 0.0
    for row in expected:
        whole, generated = int(row["total_tokens"]), int(row["output_tokens"])
        read, write = int(row["cache_read_tokens"]), int(row["cache_write_tokens"])
        per_in, per_out, per_read, per_write = rates[row["model"]]
        by_hand += ((whole - generated - read - write) * per_in + generated * per_out
                    + read * per_read + write * per_write) / 1_000_000
    # 2.74382825 on opus 4.8 + 0.00760325 on haiku 4.5, and cache writes are most of both.
    assert by_hand == pytest.approx(2.7514315)
    assert run.resources["cost"] == pytest.approx(by_hand)
    #  ...and what makes that figure auditable travels with it: the page, and the date it was read.
    assert "aws.amazon.com/bedrock/pricing/" in run.resources["cost_basis"]

    # A session the usage query returned NOTHING for has no `usage` key and therefore no zeros.
    # `{"total_tokens": 0}` would claim a free run; absence says we did not measure it.
    unmeasured_sid = sess["spans_only"]
    assert unmeasured_sid not in usage, "the fixture keeps one session with no LLM usage"
    written = json.load(open(os.path.join(out, "sessions", "sessions.json")))
    assert "usage" not in written[unmeasured_sid]
    bare = build_run(Scenario(scenario_id=unmeasured_sid, source_session=unmeasured_sid))
    # Cost included, and now for the reason that survives rates existing: no per-model counts is
    # nothing to price. A run with rates available and no usage is unpriceable, never free.
    assert bare.resources["tokens"] is UNMEASURED and bare.resources["cost"] is UNMEASURED


# --------------------------------------------------------------------------------------
# 3. the time-boundedness asymmetry: Insights is windowed, the DynamoDB scan is not
# --------------------------------------------------------------------------------------

def test_plan_rows_older_than_the_harvest_window_push_the_window_out_of_alarm_coverage(
        tmp_path, monkeypatch, capsys):
    """A session whose plan rows predate the harvest window gets an unanswerable window.

    ``harvest_spans`` is bounded to ``[now-hours, now]``; ``harvest_ddb`` is a bounded-by-COUNT
    scan with no time predicate, so ``_plan_state_bounds`` can see ``updated_at`` values from
    before the window opened. ``_window_for_session`` takes ``min(plan_start, span_start)``,
    so the stamped window starts outside the ``coverage`` the SAME harvest declared, and
    ``lookup_from_records`` refuses to answer. Sessions span turns and hours, so this is the
    ordinary case for any long conversation, not a corner.
    """
    fix = _fixture()
    out = str(tmp_path / "harvest")
    # spans shifted into the last 15 minutes; plan-state rows left at their real timestamps.
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=False)
    _point_readers_at(monkeypatch, out)

    sid = fix["sessions"]["plan_and_spans_inline_edges"]
    window = HarvestedWindowSource().window_for(sid)
    assert isinstance(window, RunWindow)
    assert window.provenance == WINDOW_FROM_PLAN_AND_SPANS
    # The window is now the union of a 2-day-old plan bound and a 15-minute-old span bound:
    # nearly two days wide for a run that lasted ~19 minutes.
    assert window.duration_s() > 24 * 3600

    source = default_alarm_source()
    coverage_start, coverage_end = source.coverage()
    assert coverage_start > window.start_dt(), "expected the window to precede coverage"
    health = infra_health_for_window(window, source=source)
    assert health.state is InfraState.UNKNOWN
    # The window's start predates answerable alarm coverage, so the platform's health
    # over it is UNKNOWN — never falsely healthy. WHICH phrasing depends on where the
    # (fixed-fixture) plan bound falls relative to today's ~13d retention horizon:
    # "only covers back to <coverage_start>" while the start is within retention but
    # before the source's coverage, or "past …alarm-history retention" once it ages
    # beyond ~13d. Both are the same honest "can't answer over this window" outcome, so
    # accept either — asserting only the exact string made this drift past retention and
    # flip the message as the fixed fixture date aged.
    assert ("only covers back to" in health.reason
            or "alarm-history retention" in health.reason), health.reason
    print(f"\n[coverage] window   {window.start} .. {window.end} ({window.duration_s():.0f}s)")
    print(f"[coverage] coverage {coverage_start.isoformat()} .. {coverage_end.isoformat()}")
    print(f"[coverage] {health.state.value}: {health.reason}")


def test_a_short_harvest_window_truncates_the_trajectory_but_not_the_plan(tmp_path, monkeypatch,
                                                                         capsys):
    """``IMPROVE_HARVEST_HOURS=1`` keeps every plan row and drops most of the spans.

    The two stores are bounded differently, so a run that straddles the boundary is mined with
    a partial trajectory and a full plan — which reads as "the plan did more than the agent
    did" rather than as missing evidence.
    """
    fix = _fixture()
    sid = fix["sessions"]["plan_with_real_edges"]
    # The session starts 65 minutes ago and ends ~54 minutes ago; the harvest window is the
    # last 60, so it straddles the boundary exactly as a run in flight at harvest time does.
    delta = _delta_placing_session_start(fix, sid, 65 * 60)
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=delta, shift_plan_rows=True, hours=1)
    _point_readers_at(monkeypatch, out)

    total = sum(1 for r in fix["insights_span_rows"] if r["sid"] == sid)
    kept = len(HarvestedSpanSource().spans_for(sid))
    plan = HarvestedPlanSource().plan_for(sid)
    assert 0 < kept < total, "expected the 1h window to clip this session's spans"
    assert isinstance(plan, Plan) and len(plan.nodes) == 3, "plan rows are not clipped at all"
    window = HarvestedWindowSource().window_for(sid)
    print(f"\n[1h window] {sid}: {kept}/{total} spans kept, {len(plan.nodes)} plan nodes kept, "
          f"window={window.provenance} {window.duration_s():.0f}s")


def test_harvest_spans_windows_per_day_and_concatenates(tmp_path, monkeypatch):
    """72h -> three 24h Insights queries, disjoint, no duplicate rows.

    Nothing subdivides here: 49 fixture rows against a 5000-row cap. The seed windowing is still
    the base case and must stay exactly this cheap on a quiet account — nine of 14 measured
    staging days need one query per day, and adaptive windowing must not tax them.
    """
    fix = _fixture()
    out = str(tmp_path / "harvest")
    handles = _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True,
                           hours=72)
    windows = handles["logs"].windows
    assert len(windows) == 3
    assert [b - a for a, b in windows] == [86400, 86400, 86400]
    assert windows[0][1] == windows[1][0] and windows[1][1] == windows[2][0]
    rows = json.load(open(os.path.join(out, "spans", "spans_all.json")))
    assert len(rows) == len(fix["insights_span_rows"])
    assert len({r["@ptr"] for r in rows}) == len(rows)


# --------------------------------------------------------------------------------------
# 4b. the row cap, and what the harvest does when it hits one
#
# Measured on staging 2026-08-29, `stats count() by bin(1d)` under the same
# `svc like /mediaworks_harness/` filter as the harvest query, 14 days:
#
#     08-15  4,413   08-16  4,789   08-17  6,195 *  08-18  8,363 *  08-19  3,365
#     08-20  7,275 * 08-21  4,325   08-22  2,415    08-24  2,070    08-25  3,176
#     08-26  2,941   08-27 12,258 * 08-28 13,766 *  08-29  4,047
#
# FIVE of fourteen days exceed the 5000-row cap (*), and the worst two lose the majority of
# their evidence: Aug 28 kept 5,000 of 13,766 (64% dropped), Aug 27 kept 5,000 of 12,258 (59%).
# Because the query is `sort @timestamp asc`, the rows kept are the OLDEST and the rows dropped
# are session ENDINGS — where retrievals and outcomes live. That is the defect these tests pin.
# --------------------------------------------------------------------------------------

def _spans_only_aws(monkeypatch, rows):
    """``(_Aws, fake logs)`` for calling ``harvest_spans`` directly, with the clock stubbed.

    Direct rather than through ``harvest``, so the window bounds are the FIXTURE's own real
    timestamps instead of wall-clock-now minus N hours. The bisection arithmetic is about exact
    window boundaries, and a test that cannot name them cannot assert on them.
    """
    logs = _FakeLogs(rows)
    session = _FakeSession({"logs": logs})
    monkeypatch.setattr(harvest_live.boto3, "Session", lambda *a, **k: session)
    monkeypatch.setattr(harvest_live.time, "sleep", lambda _s: None)
    return harvest_live._Aws(region="us-west-2"), logs


def _fixture_span_range(fix) -> tuple[int, int]:
    """Epoch bounds that contain every real fixture row and nothing else."""
    stamps = [_epoch(r["@timestamp"]) for r in fix["insights_span_rows"]]
    return int(min(stamps)), int(max(stamps)) + 1


def _every_window_is_under_cap_or_recorded(meta: dict) -> None:
    """Acceptance V-01a, as an assertion: no window is both clipped and silent.

    This is the whole contract. Every window the harvest actually read either came back under the
    row cap — so its rows are complete — or appears in ``clipped_windows``. There is no third
    outcome, and the old code's third outcome (warn, then keep the truncated read anyway) is what
    manufactured a false control.
    """
    recorded = {(c["from"], c["to"]) for c in meta["clipped_windows"]}
    for window in meta["windows_read"]:
        assert (window["rows"] < meta["bounds"]["row_cap"]
                or (window["from"], window["to"]) in recorded), window


def test_a_capped_window_is_bisected_and_the_clipped_read_discarded(tmp_path, monkeypatch):
    """A read AT the cap is thrown away and replaced by its two halves — not concatenated.

    With the cap at 40 the fixture's 49 rows over 19.4h saturate one window; the halves hold 37
    and 12, both under. So three queries, and the answer is all 49 rows exactly once.

    The load-bearing assertion is the duplicate check. Keeping the capped read AND its halves
    would return 89 rows and put 40 of them in a session's trajectory TWICE, inventing repeated
    tool calls — which is precisely the signal the efficiency path is supposed to measure.
    """
    fix = _fixture()
    start, end = _fixture_span_range(fix)
    monkeypatch.setattr(harvest_live, "_INSIGHTS_ROW_CAP", 40)
    aws, logs = _spans_only_aws(monkeypatch, fix["insights_span_rows"])

    rows, clipped = harvest_live.harvest_spans(aws, str(tmp_path), start, end)

    assert len(logs.windows) == 3, "one saturated read, then its two halves"
    assert logs.windows[0] == (start, end)
    mid = start + (end - start) // 2
    assert logs.windows[1:] == [(start, mid), (mid, end)]
    # All 49 real rows, each exactly once. The discarded read's 40 are not in here twice.
    assert len(rows) == len(fix["insights_span_rows"])
    assert len({r["@ptr"] for r in rows}) == len(rows)
    assert clipped == [], "both halves came back under the cap, so nothing is unresolved"
    meta = json.load(open(os.path.join(str(tmp_path), "spans", "spans_meta.json")))
    assert meta["queries"] == 3
    _every_window_is_under_cap_or_recorded(meta)


def test_bisection_stops_at_the_floor_and_records_the_clip(tmp_path, monkeypatch):
    """Termination, and what it costs. A floor is reached ⇒ a clip is RECORDED, rows still kept.

    Cap 10, floor 6h: the 19.4h range halves to 9.7h, halves again to 4.85h, and 4.85h is below
    the floor — so those windows are read once and the ones still at the cap are written down.
    Nothing recurses forever and nothing disappears quietly. On real staging traffic this branch
    is unreachable at the default 1h floor (busiest measured hour: 1,643 spans, busiest three
    hours: 3,836 — both under 5,000), which is why the floor is a guard rail rather than a
    tuning knob.
    """
    fix = _fixture()
    start, end = _fixture_span_range(fix)
    monkeypatch.setattr(harvest_live, "_INSIGHTS_ROW_CAP", 10)
    monkeypatch.setattr(harvest_live, "_MIN_SPAN_WINDOW_S", 21600)
    aws, _logs = _spans_only_aws(monkeypatch, fix["insights_span_rows"])

    rows, clipped = harvest_live.harvest_spans(aws, str(tmp_path), start, end)

    assert clipped, "a window that stayed at the cap below the floor must be recorded"
    for entry in clipped:
        assert entry["to"] - entry["from"] <= 21600, "a clip can only be recorded at the floor"
        assert "floor" in entry["why"] and "MISSING" in entry["why"]
        assert entry["kept"] == 10, "the truncated rows are still kept — the gap is what is new"
        assert entry["from_iso"].endswith("+00:00") and entry["to_iso"].endswith("+00:00")
    # Rows are NOT discarded when a clip is recorded; a partial read is still evidence, it is
    # just evidence that has to be labelled.
    assert rows
    meta = json.load(open(os.path.join(str(tmp_path), "spans", "spans_meta.json")))
    _every_window_is_under_cap_or_recorded(meta)


def test_query_budget_exhaustion_is_recorded_not_silent(tmp_path, monkeypatch):
    """Running out of queries is a written-down gap, not a shorter answer.

    Cap 10, budget 3: the harvest spends its three reads splitting and then has a queue of
    windows it cannot afford. Every one of them is recorded, and the union of the recorded
    intervals is the WHOLE requested range — so a reader can tell that this harvest read nothing
    conclusive, rather than inferring a quiet account from a small row count.
    """
    fix = _fixture()
    start, end = _fixture_span_range(fix)
    monkeypatch.setattr(harvest_live, "_INSIGHTS_ROW_CAP", 10)
    monkeypatch.setattr(harvest_live, "_INSIGHTS_MAX_QUERIES", 3)
    aws, logs = _spans_only_aws(monkeypatch, fix["insights_span_rows"])

    _rows, clipped = harvest_live.harvest_spans(aws, str(tmp_path), start, end)

    assert len(logs.windows) == 3, "the budget is a hard stop, not a suggestion"
    budgeted = [c for c in clipped if "query budget exhausted" in c["why"]]
    assert budgeted, "the unread remainder must be recorded"
    assert all(c["kept"] == 0 for c in budgeted), "nothing was read from these"
    # The whole requested range is accounted for: every second is either in a window that was
    # read, or in a window that was written down as unread.
    accounted = _coverage([(c["from"], c["to"]) for c in clipped]
                          + [(w["from"], w["to"]) for w in json.load(
                              open(os.path.join(str(tmp_path), "spans",
                                                "spans_meta.json")))["windows_read"]])
    assert accounted == [(start, end)]


def test_a_session_overlapping_a_clipped_window_is_marked_incomplete(tmp_path):
    """The ``74b0a063`` shape: the run is in the harvest, and part of its trajectory is not.

    That session ran 2026-08-27 12:39:15..12:48:21Z with 249 spans — dispatch at 90 paired by
    retrieves at 179/230/232, and a second dispatch at 239 that nothing fetched. The truncated
    harvest kept spans up to 12:40:57 and dropped everything after, so its dispatch survived and
    all three of its retrieves did not: a healthy run entered the control pool looking like
    "dispatched, never retrieved" and blocked REQ-018 for four real failures.

    Decided on the session's own SPAN BOUNDS against the clipped interval, through
    ``contract.parse_utc`` — never on ISO strings, which compare wrong across the two spellings
    this system emits (``'…19.5Z' > '…19.5+00:00'``). A session outside the gap is untouched.
    """
    fix = _fixture()
    late = fix["sessions"]["plan_with_real_edges"]                # 08-25 08:46:33..08:57:30
    early = fix["sessions"]["plan_and_spans_offloaded_edges"]     # 08-24 13:34:04..14:32:42
    gap_start = int(_epoch("2026-08-25 08:00:00.000"))
    gap_end = int(_epoch("2026-08-25 09:00:00.000"))

    written = harvest_live.reconstruct_sessions(
        str(tmp_path), fix["insights_span_rows"], [],
        recorded_at="2026-08-29T00:00:00+00:00",
        clipped=[{"from": gap_start, "to": gap_end, "kept": 5000,
                  "from_iso": harvest_live._epoch_iso(gap_start),
                  "to_iso": harvest_live._epoch_iso(gap_end),
                  "why": "still at the 5000-row cap"}])

    assert written[late]["evidence_complete"] is False
    assert "KNOWN missing" in written[late]["evidence_note"]
    assert harvest_live._epoch_iso(gap_start) in written[late]["evidence_note"]
    assert written[early]["evidence_complete"] is True
    assert written[early]["evidence_note"] == ""

    # And the reader picks it up from the file, which is the only path build_run has to it.
    source = HarvestedSpanSource(os.path.join(str(tmp_path), "sessions", "sessions.json"))
    assert source.completeness_for(late)[0] is False
    assert source.completeness_for(early)[0] is True


def test_a_harvest_that_recorded_no_completeness_reports_unknown_not_complete(tmp_path):
    """``None`` and ``[]`` are different measurements and must not collapse.

    ``clipped=None`` — the state of every harvest taken before adaptive windowing — writes NO
    completeness key, and the reader answers UNKNOWN. ``clipped=[]`` means the harvest checked and
    found no clipping, which is a real measurement and is written as ``true``.

    Defaulting the first to ``true`` is the trap: it would assert that a truncation check ran and
    passed over evidence nobody checked, on the entire golden set.
    """
    fix = _fixture()
    sid = fix["sessions"]["plan_with_real_edges"]

    unknown_dir = os.path.join(str(tmp_path), "unknown")
    unrecorded = harvest_live.reconstruct_sessions(
        unknown_dir, fix["insights_span_rows"], [], recorded_at="2026-08-29T00:00:00+00:00")
    assert all("evidence_complete" not in s for s in unrecorded.values())
    complete, note = HarvestedSpanSource(
        os.path.join(unknown_dir, "sessions", "sessions.json")).completeness_for(sid)
    assert complete is UNKNOWN and note == ""

    clean_dir = os.path.join(str(tmp_path), "clean")
    measured = harvest_live.reconstruct_sessions(
        clean_dir, fix["insights_span_rows"], [], recorded_at="2026-08-29T00:00:00+00:00",
        clipped=[])
    assert measured[sid]["evidence_complete"] is True
    assert HarvestedSpanSource(
        os.path.join(clean_dir, "sessions", "sessions.json")).completeness_for(sid)[0] is True

    # A session the harvest never saw is UNKNOWN too, not complete.
    assert HarvestedSpanSource(
        os.path.join(clean_dir, "sessions", "sessions.json")
    ).completeness_for("no-such-session")[0] is UNKNOWN


def test_a_clipped_harvest_reaches_the_run_and_unobserves_the_async_facets(tmp_path, monkeypatch):
    """End to end: a clipped harvest ⇒ ``evidence_complete is False`` ⇒ facets unobserved.

    Cap 6 with the floor pinned at the seed width, so the harvest cannot split and has to record
    the clip. `trajectory_absent` must stay False — spans WERE captured — while every
    `_NEEDS_TRAJECTORY` facet goes unobserved. Those are two different facts about the evidence
    and the whole point of routing them separately.
    """
    from improve.signature import _NEEDS_TRAJECTORY, shape_facets

    fix = _fixture()
    out = str(tmp_path / "harvest")
    monkeypatch.setattr(harvest_live, "_INSIGHTS_ROW_CAP", 6)
    monkeypatch.setattr(harvest_live, "_MIN_SPAN_WINDOW_S", 86400)
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True, hours=24)
    _point_readers_at(monkeypatch, out)

    written = json.load(open(os.path.join(out, "sessions", "sessions.json")))
    assert written, "the truncated read still kept some rows"
    sid = next(iter(written))
    assert written[sid]["evidence_complete"] is False

    run = build_run(Scenario(scenario_id=sid, source_session=sid))
    assert run.evidence_complete is False
    assert "KNOWN missing" in run.evidence_note
    facets = {f.name: f for f in shape_facets(run)}
    assert facets["trajectory_absent"].value is False, "spans were captured; only clipped"
    for name in sorted(_NEEDS_TRAJECTORY):
        assert facets[name].observed is False, name


# --------------------------------------------------------------------------------------
# 4c. the trace, and the parent it names
#
# A plan sub-session shares an OTEL trace with the request that spawned it, and `traceId` is a
# TOP-LEVEL span field — not an attribute, which is why an audit of `attributes.*` never found it.
# Measured on staging over 14 days (2026-09-02): 1,481 traces carrying 141 `plan-session-*`
# sub-sessions, 141 of 141 in a trace holding exactly ONE non-plan session, 0 unlinked, 0 traces
# with two candidate parents, and 70 parents of which 26 fanned out (6 on the busiest).
#
# What this replaced named no parent at all: the hint was the sub-session id's own suffix, unique
# per row, so `mine`'s sibling de-duplication had nothing to collapse and had been inert since it
# was written. These tests are the ones that would have caught that, and the shape of them is why
# — the positive assertion is that two DIFFERENT sub-sessions agree, which no per-row value can
# satisfy.
# --------------------------------------------------------------------------------------

def test_the_span_query_projects_the_trace_or_the_reader_gets_nothing(tmp_path, monkeypatch):
    """The one-line projection, pinned from both sides.

    Insights answers with the ``fields`` clause and nothing else, and the fake honours that, so
    dropping ``traceId`` from the query empties every session's ``traces`` — and with it every
    parent link — while the harvest still exits 0 and writes seven files. That is the failure mode
    to keep loud.
    """
    fix = _fixture()
    start, end = _fixture_span_range(fix)
    aws, logs = _spans_only_aws(monkeypatch, fix["insights_span_rows"])

    rows, _clipped = harvest_live.harvest_spans(aws, str(tmp_path), start, end)
    assert all(harvest_live.TRACE_FIELD in q for q in logs.span_queries), logs.span_queries
    assert all(r.get("trace") for r in rows), "the projected trace did not reach the rows"

    written = harvest_live.reconstruct_sessions(str(tmp_path), rows, [],
                                               recorded_at="2026-09-02T00:00:00+00:00")
    assert all(s["traces"] for s in written.values())
    # A request spans MANY traces — one per invocation — which is why the field is a list. The
    # fixture's four real conversations carry 11 between them.
    assert max(len(s["traces"]) for s in written.values()) > 1
    assert sum(len(s["traces"]) for s in written.values()) == len(
        {r["trace"] for r in fix["insights_span_rows"]})


def test_sibling_sub_sessions_resolve_to_the_one_parent_session_across_the_seam():
    """Producer -> reader -> classify, on a request that really did fan out.

    Asserts the property the old id-derived hint could not have: that SEVERAL sub-sessions agree on
    ONE key, and that the key is a session id a consumer can look up rather than a trace id that
    resolves to nothing in this system.
    """
    for group in ("fanout_1", "fanout_2"):
        parent, kids, span_source = fanout_harvest(group)
        assert len(kids) >= 2, f"{group} is not a fan-out"

        resolved = {kid: span_source.parent_for(kid) for kid in kids}
        assert set(resolved.values()) == {parent}, resolved
        # The parent's own record is in the same harvest, so it was looked at — and it has no
        # parent, which is NO_DATA rather than UNKNOWN.
        assert span_source.parent_for(parent) is NO_DATA

        for kid in kids:
            run = build_run(Scenario(scenario_id=kid, source_session=kid),
                            span_source=span_source,
                            plan_source=HarvestedPlanSource(_NO_PLAN_HARVEST))
            assert run.parent_run_id == parent
            workload = classify(run)
            assert workload.cls is WorkloadClass.PLAN_NODE
            assert workload.parent_hint == parent
            # The id's own suffix is what this used to report. It must not be the answer now.
            assert workload.parent_hint != kid[len("plan-session-"):]


def test_a_harvest_with_no_trace_projection_reports_unknown_not_a_parentless_population(tmp_path):
    """The stale-harvest case, and why it is UNKNOWN rather than "no parent".

    Every harvest taken before the projection existed is in this state. Reporting those runs as
    parentless would look exactly like a healthy population of independent sub-sessions and would
    silently re-arm the inert de-duplication — so the two are kept apart, and a consumer that
    de-duplicates on the value keys each run on itself instead of merging them all.
    """
    parent, kids, _source = fanout_harvest()
    _p2, _k2, rows = fanout_span_rows()
    stale = {sid: {"spans": [], "n_spans": 0}
             for sid in {r["sid"] for r in rows}}
    path = str(tmp_path / "sessions-without-traces.json")
    with open(path, "w") as f:
        json.dump(stale, f)

    source = HarvestedSpanSource(path)
    for kid in kids:
        assert source.parent_for(kid) is UNKNOWN
    assert source.parent_for(parent) is UNKNOWN
    # And a session the harvest never held at all is UNKNOWN too, never a guess.
    assert source.parent_for("no-such-session") is UNKNOWN

    run = build_run(Scenario(scenario_id=kids[0], source_session=kids[0]),
                    span_source=source, plan_source=HarvestedPlanSource(_NO_PLAN_HARVEST))
    assert run.parent_run_id is UNKNOWN
    assert classify(run).parent_hint is None


def test_a_trace_with_no_parent_and_one_with_two_are_both_reported_as_none():
    """The join refuses to choose. Neither case occurs on the measured population (0 of 1,481
    traces), and both are one edit away from a fabricated link, so the rule is asserted directly
    on the pure function rather than left to the next harvest to discover.

    Built from the real fixture's own ids and traces — the only thing rearranged is which session
    sits in which trace, because a harvest that exhibits these two shapes does not exist to be
    read.
    """
    parent, kids, _source = fanout_harvest()
    other_parent, _other_kids, _rows = fanout_span_rows("fanout_2")

    orphan = parents_by_session({kids[0]: {"traces": ["t1"]}, kids[1]: {"traces": ["t1"]}})
    assert orphan == {}, "a trace of only sub-sessions names no parent"

    ambiguous = parents_by_session({kids[0]: {"traces": ["t1"]},
                                    parent: {"traces": ["t1"]},
                                    other_parent: {"traces": ["t1"]}})
    assert ambiguous == {}, "two candidate parents in one trace is not a coin toss"

    disagreeing = parents_by_session({kids[0]: {"traces": ["t1", "t2"]},
                                      parent: {"traces": ["t1"]},
                                      other_parent: {"traces": ["t2"]}})
    assert disagreeing == {}, "two traces naming different parents is not a coin toss either"

    # The same function on the same ids in the arrangement the harvest actually records.
    agreeing = parents_by_session({kids[0]: {"traces": ["t1"]}, kids[1]: {"traces": ["t1"]},
                                   parent: {"traces": ["t1", "t2"]}})
    assert agreeing == {kids[0]: parent, kids[1]: parent}


def _coverage(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """``windows`` merged into the minimal set of intervals they cover. PURE.

    The comparison unit for "did these two queries read the same stretch of time", which is a
    weaker and truer property than "did they use the same boundaries" once one of them subdivides.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def test_token_usage_is_one_query_per_seed_window_not_one_per_session(tmp_path, monkeypatch):
    """The efficiency property, asserted rather than assumed.

    Usage is aggregated SERVER-SIDE by ``(sid, model)``, so the number of Insights round trips is
    a function of the window, never of the population: three days of harvest, four sessions, three
    usage queries. Per-session would have been four, and at the deployed ``IMPROVE_LIMIT`` sixty, each
    costing at least one 2s poll against a 900s Lambda ceiling that a real run already spends 287s of.
    """
    fix = _fixture()
    out = str(tmp_path / "harvest")
    handles = _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True,
                           hours=72)
    sessions = json.load(open(os.path.join(out, "sessions", "sessions.json")))
    assert len(sessions) >= 4
    assert len(handles["logs"].usage_windows) == 3, "one per seed window, NOT one per session"


def test_the_span_and_usage_halves_cover_the_same_time_range(tmp_path, monkeypatch):
    """The property that actually matters, and it is COVERAGE, not boundaries.

    This assertion used to read ``usage_windows == windows`` — identical boundary lists — and the
    reason given was that the alignment is load-bearing rather than coincidental. The reason was
    right; the assertion was a proxy for it that only held while spans could not subdivide.

    Adaptive windowing breaks boundary equality BY DESIGN: a span window that comes back at the
    Insights row cap is discarded and replaced by its two halves, while the usage query — a
    ``stats`` aggregation, not a record projection, and therefore not row-capped — has nothing to
    gain from splitting and would pay a 2s poll per extra window for it. Deleting the assertion to
    make it pass would have thrown away the property; asserting boundary equality would have
    forced usage to subdivide for no measurement gain.

    What the alignment is FOR is attribution: a session's tokens must be summed over the same
    stretch of time its spans were read from, or the two halves of one run describe different
    runs. That is coverage — same first start, same last end, no gaps, no overlaps — and it is
    structural here rather than asserted-and-hoped, because span windows are produced only by
    halving the seed windows the usage query walks (`harvest_live._seed_windows`).
    """
    fix = _fixture()
    out = str(tmp_path / "harvest")
    # A 12h seed over 24h and a row cap the 49 fixture rows exceed, so the span half genuinely
    # subdivides and the two boundary lists genuinely differ. Without this the test would pass
    # against the old equality too and prove nothing.
    monkeypatch.setattr(harvest_live, "_INSIGHTS_WINDOW_SECONDS", 43200)
    monkeypatch.setattr(harvest_live, "_INSIGHTS_ROW_CAP", 20)
    handles = _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True,
                           hours=24)
    span_windows = handles["logs"].windows
    usage_windows = handles["logs"].usage_windows

    assert usage_windows == _seed_pair(handles), "usage walks the seed windows unchanged"
    assert span_windows != usage_windows, (
        "the span half must have subdivided, or this test is asserting nothing new")

    # `logs.windows` is every window ASKED FOR, parents of a split included, so it deliberately
    # overlaps itself. What covers the range is the set of windows whose rows were KEPT, plus the
    # intervals recorded as unresolvable — and that is what has to match the usage side.
    meta = json.load(open(os.path.join(out, "spans", "spans_meta.json")))
    kept = [(w["from"], w["to"]) for w in meta["windows_read"]]
    gaps = [(c["from"], c["to"]) for c in meta["clipped_windows"]]
    assert _coverage(kept + gaps) == _coverage(usage_windows), "same range, different boundaries"
    assert len(_coverage(kept + gaps)) == 1, "one contiguous range, no gaps"
    ordered = sorted(kept)
    assert all(b[0] >= a[1] for a, b in zip(ordered, ordered[1:])), "kept windows are disjoint"
    # And the rows are read once each: a discarded capped read that was concatenated instead
    # would show up here as duplicates.
    rows = json.load(open(os.path.join(out, "spans", "spans_all.json")))
    assert len({r["@ptr"] for r in rows}) == len(rows)


def _seed_pair(handles) -> list[tuple[int, int]]:
    """The seed windows for the harvest just run, recomputed from what the span half asked for.

    The union of the span windows IS the requested range, so the seeds are derivable from it —
    which keeps this test from having to know the wall-clock ``now`` the producer used.
    """
    start, end = _coverage(handles["logs"].windows)[0]
    return harvest_live._seed_windows(start, end)


def test_usage_measured_for_a_session_with_no_spans_is_dropped_loudly(tmp_path, capsys):
    """The span cap costs real measurements, and the amount is now countable.

    ``harvest_spans`` clips at 5000 rows per day-window; the usage query aggregates server-side and
    does not. So the usage half routinely measures sessions the span half lost — observed on
    staging on 2026-08-29, where a 24h harvest measured 44 sessions and kept 20, discarding 13.3M
    of 36.8M tokens. Those sessions have no trajectory so they cannot be scored, and orphaning
    their usage would be worse than dropping it; what must not happen is dropping it in silence.
    """
    fix = _fixture()
    spans = [{"sid": fix["sessions"]["spans_only"], "@timestamp": "2026-08-24 13:34:04.889",
              "svc": "mediaworks_harness_main_agent.DEFAULT", "name": "chat", "status": "UNSET"}]
    usage = harvest_live._usage_by_session(fix["insights_usage_rows"])
    harvest_live.reconstruct_sessions(str(tmp_path), spans, [], usage=usage,
                                      recorded_at="2026-08-29T00:00:00+00:00")
    out = capsys.readouterr().out
    assert "had measured usage but NO spans" in out
    # Three of the four fixture sessions were measured; only the span-bearing one is kept.
    assert "3 session(s)" in out
    written = json.load(open(os.path.join(str(tmp_path), "sessions", "sessions.json")))
    assert set(written) == {fix["sessions"]["spans_only"]}
    assert "usage" not in written[fix["sessions"]["spans_only"]]


def test_the_discarded_spend_is_written_where_a_reader_of_the_spend_can_find_it(tmp_path,
                                                                               monkeypatch):
    """A log line is invisible to whoever reads the cost figure the tokens are missing from.

    The kept half of this fixture is one real session's 551,627 tokens; the other two measured
    sessions have no spans here, so 2,274,408 of the 2,826,035 tokens the usage query measured
    cannot be scored. That is the whole shape of the staging observation this file already records
    in the test above — and until now the only trace of it was stdout.
    """
    fix = _fixture()
    kept_sid = fix["sessions"]["plan_and_spans_offloaded_edges"]
    # A SUBSET of the fixture's span rows, which is exactly what the row cap produces: the usage
    # query still aggregates every session server-side, the span query comes back short.
    spans = [r for r in fix["insights_span_rows"] if r["sid"] == kept_sid]
    usage = harvest_live._usage_by_session(fix["insights_usage_rows"])
    harvest_live.reconstruct_sessions(str(tmp_path), spans, [], usage=usage,
                                      recorded_at="2026-08-29T00:00:00+00:00")

    _point_readers_at(monkeypatch, str(tmp_path))
    shortfall = harvest_usage_shortfall()
    assert shortfall == {"tokens_discarded": 2274408, "tokens_measured": 2826035,
                        "sessions_discarded": 2, "sessions_measured": 3}
    # The audit is a sibling of sessions.json and not a key inside it: every reader of that file
    # iterates it as {session id: record}, so a reserved key would arrive as a spanless session.
    assert os.path.exists(os.path.join(str(tmp_path), USAGE_AUDIT_RELPATH))
    written = json.load(open(os.path.join(str(tmp_path), "sessions", "sessions.json")))
    assert set(written) == {kept_sid}
    # And the kept side reconciles: what the reader will sum plus what it says was discarded is
    # everything the usage query measured. No third bucket, nothing rounded away.
    assert written[kept_sid]["usage"]["total_tokens"] + shortfall["tokens_discarded"] == 2826035


def test_a_harvest_that_took_no_usage_query_reports_unmeasured_not_a_clean_bill(tmp_path,
                                                                               monkeypatch):
    """``usage=None`` writes NO audit, and the reader answers UNMEASURED for every term.

    The trap is a zero. A harvest that never measured usage and one that measured it and found the
    cap dropped nothing produce the same ``0`` if this file is written unconditionally — and only
    the second is good news. Same rule, and the same reason, as ``evidence_complete``'s
    ``None``-versus-``[]`` above.
    """
    fix = _fixture()
    harvest_live.reconstruct_sessions(str(tmp_path), fix["insights_span_rows"], [], usage=None,
                                      recorded_at="2026-08-29T00:00:00+00:00")
    assert not os.path.exists(os.path.join(str(tmp_path), USAGE_AUDIT_RELPATH))

    _point_readers_at(monkeypatch, str(tmp_path))
    shortfall = harvest_usage_shortfall()
    assert set(shortfall) == {"tokens_discarded", "tokens_measured",
                             "sessions_discarded", "sessions_measured"}
    assert all(value is UNMEASURED for value in shortfall.values())


def test_a_usage_query_that_ran_and_lost_nothing_records_a_measured_zero(tmp_path, monkeypatch):
    """The other half of the pair: every measured session kept, so the shortfall IS zero.

    A measured zero and an unmeasured one are different facts, and this is the one that says the
    row cap did not bind on this harvest — which is the empirical acceptance test for the adaptive
    windowing above.
    """
    fix = _fixture()
    usage = harvest_live._usage_by_session(fix["insights_usage_rows"])
    harvest_live.reconstruct_sessions(str(tmp_path), fix["insights_span_rows"], [], usage=usage,
                                      recorded_at="2026-08-29T00:00:00+00:00")
    _point_readers_at(monkeypatch, str(tmp_path))
    shortfall = harvest_usage_shortfall()
    assert shortfall["tokens_discarded"] == 0 and shortfall["sessions_discarded"] == 0
    assert shortfall["tokens_measured"] == 2826035


def test_one_span_with_no_duration_unmeasures_the_latency_rather_than_shortening_it(tmp_path,
                                                                                   monkeypatch):
    """``latency_ms`` is ALL-OR-NOTHING, the rule its own comment has always promised.

    Summing the steps that happen to carry a duration produces a number smaller than the truth, and
    `dto` counts the run as MEASURED the moment it is a number — so the population's
    ``spend.latencyMs`` reads "measured on 25 of 25" over a total missing whatever the untimed steps
    cost. There is no denominator on ``Run.resources`` to declare a shortfall in, so the sentinel is
    the only honest answer.
    """
    fix = _fixture()
    sid = fix["sessions"]["plan_and_spans_offloaded_edges"]
    rows = [r for r in fix["insights_span_rows"] if r["sid"] == sid]
    assert len(rows) >= 3 and all(r.get("durationNano") for r in rows)

    whole = str(tmp_path / "timed")
    harvest_live.reconstruct_sessions(whole, rows, [], recorded_at="2026-08-29T00:00:00+00:00")
    _point_readers_at(monkeypatch, whole)
    timed = build_run(Scenario(scenario_id=sid, source_session=sid))
    assert isinstance(timed.resources["latency_ms"], int)
    assert timed.resources["latency_ms"] > 0

    # One span loses its duration — the state a harvest is in whenever the exporter dropped
    # `durationNano`, which `_to_step` already reports as `None` rather than as 0.
    holed = str(tmp_path / "one-untimed")
    partial = [{**rows[0], "durationNano": None}] + rows[1:]
    harvest_live.reconstruct_sessions(holed, partial, [], recorded_at="2026-08-29T00:00:00+00:00")
    _point_readers_at(monkeypatch, holed)
    run = build_run(Scenario(scenario_id=sid, source_session=sid))
    assert len(run.trajectory) == len(rows), "the trajectory itself is unaffected"
    assert run.resources["latency_ms"] is UNMEASURED


def test_the_insights_window_and_group_cap_are_tunable_and_the_cap_warns(tmp_path, monkeypatch,
                                                                        capsys):
    """The two operational limits the usage harvest introduces, both env-overridable.

    ``IMPROVE_INSIGHTS_WINDOW_SECONDS`` SEEDS both Insights queries — one knob, because usage must
    cover the same range as the spans it is attributed to, and two knobs could drift. (The span
    half then subdivides a seed that comes back at the row cap; see
    ``test_the_span_and_usage_halves_cover_the_same_time_range`` for why that is coverage
    equality, not boundary equality.) ``IMPROVE_USAGE_GROUP_LIMIT`` caps returned
    ``(session, model)`` groups; hitting it means some sessions' tokens are MISSING rather than
    zero, which must be loud. Both are read at import (like ``DDB_TABLES`` and
    ``_ALARM_PAGE_CAP``), so the constants are patched, not the env.
    """
    fix = _fixture()
    # A 12h window over a 24h harvest: two chunks per query instead of one, for both queries. The
    # 49 fixture rows are far under the row cap, so nothing subdivides and the lists still match.
    monkeypatch.setattr(harvest_live, "_INSIGHTS_WINDOW_SECONDS", 43200)
    handles = _run_harvest(monkeypatch, str(tmp_path / "a"), delta_s=_delta_to_now(fix),
                           shift_plan_rows=True, hours=24)
    assert len(handles["logs"].windows) == 2
    assert handles["logs"].usage_windows == handles["logs"].windows
    assert [b - a for a, b in handles["logs"].windows] == [43200, 43200]

    # A group cap the fixture's four usage rows reach: truncation is reported, not silent.
    monkeypatch.setattr(harvest_live, "_USAGE_GROUP_LIMIT", 4)
    _run_harvest(monkeypatch, str(tmp_path / "b"), delta_s=_delta_to_now(fix),
                 shift_plan_rows=True, hours=24)
    out = capsys.readouterr().out
    assert "at the 4 cap" in out and "MISSING from this harvest, not zero" in out


def test_a_usage_row_with_no_total_tokens_records_nothing_rather_than_zero(capsys):
    """Absence must not arrive as a measured zero — the ``or 0`` bug, pinned.

    ``_usage_by_session`` coerces the four sums with ``or 0`` so a genuinely-zero cache read stays
    0. Applied to a row that carries no usage at all, that wrote a record with a TRUSTED
    provenance and ``total_tokens: 0``, which the reader believed — a run that cost real money
    reported as free. So a row without ``total_tokens`` yields no record, loudly.
    """
    fix = _fixture()
    real = harvest_live._usage_by_session(fix["insights_usage_rows"])
    assert real and all(u["total_tokens"] > 0 for u in real.values())

    # A raw SPAN row: it has `sid`, and none of the usage fields. This is exactly what a
    # query-blind fake (or a query whose `stats` clause was edited away) hands back.
    empty = harvest_live._usage_by_session(fix["insights_span_rows"][:3])
    assert empty == {}
    assert "no total_tokens" in capsys.readouterr().out


def test_the_only_table_scanned_is_the_only_one_a_reader_reads(tmp_path, monkeypatch):
    """``DDB_TABLES``, pinned against the reader — a scan with no reader is a nightly bill.

    Structural rather than behavioural, and that is the point: the file-list test above cannot catch a
    table being added back, because a table this file's fake does not hold raises ResourceNotFound and
    is swallowed as a warning, so no file appears and the list still passes. This is the assertion with
    teeth.

    ``job-registry`` was the case. It was scanned every night and written to ``ddb/job-registry.json``,
    which nothing opens — the only ``job_registry`` readers under ``agent/src`` are the runtime's own
    (``task_agent``, ``main``, ``mcp_manager``), reached through ``JobRegistry`` and not through a file.
    Measured in staging on 2026-09-03 that was 2,157 rows and 2.3 MB read for nothing.
    """
    assert set(harvest_live.DDB_TABLES) == {"plan-state"}
    # ...and the label IS the filename the reader resolves, so the two halves of the seam are checked
    # against each other rather than both against a string in this test.
    _point_readers_at(monkeypatch, str(tmp_path))
    reader_paths = [os.path.join(str(tmp_path), "fresh", "plan-state-fresh.json"),
                    os.path.join(str(tmp_path), "ddb", "plan-state.json")]
    for label in harvest_live.DDB_TABLES:
        assert any(p.endswith(os.path.join("ddb", f"{label}.json")) for p in reader_paths), label


def test_the_plan_scan_says_when_its_own_row_cap_truncated(tmp_path, monkeypatch, capsys):
    """A truncated plan snapshot must not look like a table with nothing in it.

    THE FAILURE THIS CLOSES, and it is a misattribution rather than a lost number. A short snapshot
    is indistinguishable downstream from a session that genuinely has no plan: `HarvestedPlanSource`
    finds no row, `build_run` records ``plan_absent``, and the session is then read as an
    observability defect in the RUNTIME — when the cause was this producer's own ``--max-items``.
    Nothing said the cap had bound.

    Not currently binding, and that is stated rather than assumed: `plan-state` held 106 rows in
    staging on 2026-09-03 against a cap of 300, and it carries a 24h TTL so its size tracks traffic.
    So this is a trace for the day it does bind, exercised here at a cap small enough to make it.

    The line reports the KEPT count against the cap and never a count of what was dropped, which a
    paginator stopped at its limit cannot know.
    """
    fix = _fixture()
    session = _FakeSession({"dynamodb": _FakeDynamoDB(
        {_REAL_DDB_TABLES["plan-state"]: fix["ddb_plan_state_items"]})})
    monkeypatch.setattr(harvest_live.boto3, "Session", lambda *a, **k: session)
    out = str(tmp_path / "harvest")

    # 13 real rows, a cap of 4: the paginator stops inside the table.
    items = harvest_live.harvest_ddb(harvest_live._Aws(region="us-west-2"), out, max_items=4)
    assert len(items["plan-state"]) == 4
    printed = capsys.readouterr().out
    assert "stopped at its 4-row cap" in printed
    assert "4 kept" in printed
    # It says what the consequence IS, so the log line answers the question it was written for
    # rather than merely announcing a number.
    assert "UNKNOWN from here" in printed
    assert "missing plan state" in printed

    # ...and it stays SILENT when the cap did not bind, or every nightly run carries a warning that
    # means nothing and the one that means something is unreadable.
    harvest_live.harvest_ddb(harvest_live._Aws(region="us-west-2"), out, max_items=300)
    assert "cap" not in capsys.readouterr().out


def test_a_missing_table_is_a_warning_not_a_crash(tmp_path, monkeypatch, capsys):
    """An unreachable plan-state table must not take the harvest down — and does not.

    Aimed at ``plan-state`` because that is now the only table scanned, and it makes the branch
    matter MORE than it did when a spare table stood in front of it: the whole span half of the
    harvest is independent of this scan, so a plan table that has been renamed, deleted or lost its
    read grant must cost the plan rows and nothing else.

    ``DDB_TABLES`` is read at import, so the env override is not reachable from a test; the dict is
    patched instead. (That is itself worth knowing: ``IMPROVE_PLAN_STATE_TABLE`` only takes effect if
    it is set before the module is imported, which in Lambda it is.)
    """
    fix = _fixture()
    monkeypatch.setitem(harvest_live.DDB_TABLES, "plan-state", "table-that-does-not-exist")
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)

    # The absent table's file is simply never written, so a later reader cannot tell
    # "table missing" from "table empty" — and the warning is the only trace either way.
    assert not os.path.exists(os.path.join(out, "ddb", "plan-state.json"))
    assert "table-that-does-not-exist" in capsys.readouterr().out
    # ...and the SPAN half of the harvest is unaffected: the sessions file is still written and its
    # trajectories are still readable. The plan side degrades to the NO_DATA SENTINEL rather than to
    # an exception — and not to `None`, which would be a plan this reader claims does not exist.
    assert json.load(open(os.path.join(out, "sessions", "sessions.json")))
    _point_readers_at(monkeypatch, out)
    assert HarvestedPlanSource().plan_for(fix["sessions"]["plan_with_real_edges"]) is NO_DATA


# --------------------------------------------------------------------------------------
# 4. live harvest vs the real offline harvest, same readers
# --------------------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.isdir(os.path.join(OFFLINE_HARVEST, "sessions")),
                    reason="the offline harvest lives outside the repo (ideas/staging-data)")
def test_the_un_provenanced_offline_token_file_is_refused_not_believed(monkeypatch, capsys):
    """The laptop/production asymmetry, closed in the honest direction.

    This test used to assert the opposite — ``run.resources["tokens"] ==
    usage[golden]["total_tokens"]`` — i.e. that the offline harvest supplied a token count the
    deploy path could not. That framing was backwards, and the number was wrong.

    ``fresh/token-usage-fresh.json`` was built by summing ``gen_ai.usage.total_tokens`` over every
    span carrying the attribute. Three span families carry it and they OVERLAP: ``chat`` (one LLM
    call each), ``chat <model id>`` (a second instrumentation's span for the same calls) and
    ``invoke_agent Strands Agents`` (the accumulated total for a whole invocation). Re-measured
    against staging on 2026-08-28, that sum is **exactly 2x** the ``chat``-only truth for all six
    sessions in the file — e.g. ``abc839ca``: 3,599,064 recorded, 1,799,532 real. Summed over the
    file it is 13,344,216 against a real 6,672,108.

    So the old behaviour was worse than a gap: a laptop ``--emit-wire`` reported double the tokens
    over ``measuredOf: 6/6`` while the deployed worker honestly said UNMEASURED, and a screenshot
    of the former captioned "production" would have published a 2x-inflated number. The reader now
    refuses any usage record whose ``provenance`` it does not recognise — the same discipline
    ``_window_from_record`` already applied to windows — so BOTH paths agree, and the offline path
    reports a real number again as soon as an offline harvest stamps one.

    The window half of the original test is unchanged and still true: ``fresh/
    run-windows-fresh.json`` is a real backfill that nothing on the deploy path writes.
    """
    _point_readers_at(monkeypatch, OFFLINE_HARVEST)
    legacy_path = os.path.join(OFFLINE_HARVEST, "fresh", "token-usage-fresh.json")
    usage = json.load(open(legacy_path))
    sidecar = json.load(open(os.path.join(OFFLINE_HARVEST, "fresh", "run-windows-fresh.json")))
    golden = next(sid for sid in usage if sid in (sidecar.get("windows") or sidecar))

    run = build_run(Scenario(scenario_id=golden, source_session=golden))
    print(f"\n[offline] {golden}: " + json.dumps(_run_field_map(run), indent=2))

    # The file still exists, still names this session, and is still NOT believed.
    assert usage[golden]["total_tokens"] > 0
    assert "provenance" not in usage[golden]
    assert run.resources["tokens"] is UNMEASURED
    assert run.resources["cost"] is UNMEASURED
    # The window backfill IS provenanced, so it is still trusted. The two behaviours are the
    # point: provenance is what separates them, not which file the value came from.
    assert isinstance(run.window, RunWindow)

    # And a record WITH a recognised provenance, in the place `harvest_live` writes it, is
    # believed — so this is a refusal of unattributable data, not of offline data.
    from improve.observe.build_run import HarvestedUsageSource
    trusted = tmp_sessions_with_usage(monkeypatch, golden, provenance=harvest_live.USAGE_FROM_CHAT_SPANS)
    assert HarvestedUsageSource([trusted]).usage_for(golden)["tokens"] == 1234


def test_a_usage_record_with_an_unrecognised_provenance_is_refused(monkeypatch, caplog):
    """Provenance is CHECKED, not merely stamped.

    The failure this guards is a future harvester (or a hand-patched file) writing a usage block
    from a different, unvalidated query and having it silently believed. An unrecognised
    provenance yields the sentinels and logs why, so the number is absent rather than wrong.
    """
    from improve.observe.build_run import HarvestedUsageSource
    sid = "a8c489e7-17c0-4973-9371-80a81294084d"
    good = tmp_sessions_with_usage(monkeypatch, sid, provenance=harvest_live.USAGE_FROM_CHAT_SPANS)
    assert HarvestedUsageSource([good]).usage_for(sid)["tokens"] == 1234

    for bad in ("", "summed_every_usage_bearing_span", "reaudit.py"):
        path = tmp_sessions_with_usage(monkeypatch, sid, provenance=bad)
        with caplog.at_level("WARNING"):
            got = HarvestedUsageSource([path]).usage_for(sid)
        assert got["tokens"] is UNMEASURED, bad
        assert got["cost"] is UNMEASURED
        assert "does not trust" in caplog.text


def test_the_reader_carries_the_per_model_SPLIT_and_not_only_the_model_names(monkeypatch):
    """``models`` says WHICH models a run used; ``by_model`` says how much each one cost.

    Only ``_cost_usd`` ever saw the per-model counts, so the wire could report how many models a
    population spanned and never which of them the spend concentrated on — the one figure on that
    surface a reader can act on, because tokens are wildly unevenly distributed across a roster.

    The terms are renamed into ``Run.resources``' own vocabulary (``total_tokens`` -> ``tokens``) so
    the wire mapper needs one set of names for the population and for its split.
    """
    from improve.observe.build_run import HarvestedUsageSource
    sid = "a8c489e7-17c0-4973-9371-80a81294084d"
    opus = {"total_tokens": 90570, "output_tokens": 831, "cache_read_tokens": 60671,
            "cache_write_tokens": 29064, "input_tokens": 4, "llm_calls": 3}
    haiku = {"total_tokens": 5399, "output_tokens": 228, "cache_read_tokens": 0,
             "cache_write_tokens": 5169, "input_tokens": 2, "llm_calls": 1}
    path = tmp_sessions_with_usage(
        monkeypatch, sid, provenance=harvest_live.USAGE_FROM_CHAT_SPANS,
        by_model={"global.anthropic.claude-opus-4-8": opus,
                  "us.anthropic.claude-haiku-4-5-20251001-v1:0": haiku})

    usage = HarvestedUsageSource([path]).usage_for(sid)

    # The identities are unchanged, and the counts now travel beside them.
    assert usage["models"] == ("global.anthropic.claude-opus-4-8",
                               "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert usage["by_model"] == {
        "global.anthropic.claude-opus-4-8": {
            "tokens": 90570, "input_tokens": 4, "output_tokens": 831,
            "cache_read_tokens": 60671, "cache_write_tokens": 29064, "llm_calls": 3},
        "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
            "tokens": 5399, "input_tokens": 2, "output_tokens": 228,
            "cache_read_tokens": 0, "cache_write_tokens": 5169, "llm_calls": 1},
    }


def test_a_per_model_term_the_record_does_not_carry_is_unmeasured_never_zero(monkeypatch):
    """Same rule as the top-level terms: a model whose output tokens went unrecorded did not emit
    none, and a 0 there would read as a measurement in a spend chart."""
    from improve.observe.build_run import HarvestedUsageSource
    sid = "a8c489e7-17c0-4973-9371-80a81294084d"
    # The default fixture's split carries `total_tokens` alone.
    path = tmp_sessions_with_usage(monkeypatch, sid,
                                   provenance=harvest_live.USAGE_FROM_CHAT_SPANS)
    split = HarvestedUsageSource([path]).usage_for(sid)["by_model"]

    assert split["global.anthropic.claude-opus-4-8"] == {
        "tokens": 1234, "input_tokens": UNMEASURED, "output_tokens": UNMEASURED,
        "cache_read_tokens": UNMEASURED, "cache_write_tokens": UNMEASURED,
        "llm_calls": UNMEASURED}


def test_a_session_with_no_usage_names_no_model_rather_than_zeroing_every_model(monkeypatch):
    """A run that recorded nothing did not spend zero on each model — it does not say which models
    it did not use, which is why the empty form is EMPTY and not a zeroed row per model.

    Both refusal paths must behave alike: no usage block at all, and a usage block whose provenance
    this reader does not trust. The second one matters more — the one un-provenanced usage file in
    existence is 2x wrong, and its per-model split is wrong by the same factor.
    """
    from improve.observe.build_run import HarvestedUsageSource
    sid = "a8c489e7-17c0-4973-9371-80a81294084d"
    trusted = tmp_sessions_with_usage(monkeypatch, sid,
                                      provenance=harvest_live.USAGE_FROM_CHAT_SPANS)
    source = HarvestedUsageSource([trusted])
    assert source.usage_for(sid)["by_model"]          # not vacuous: the trusted read DOES carry it

    absent = source.usage_for("00000000-0000-0000-0000-000000000000")
    assert absent["by_model"] == {} and absent["models"] == ()

    untrusted = tmp_sessions_with_usage(monkeypatch, sid, provenance="reaudit.py")
    refused = HarvestedUsageSource([untrusted]).usage_for(sid)
    assert refused["by_model"] == {} and refused["tokens"] is UNMEASURED


def tmp_sessions_with_usage(monkeypatch, sid: str, provenance: str, by_model=None) -> str:
    """A one-session sessions.json carrying a usage block, written to a temp file.

    ``by_model`` replaces the per-model split when a test needs more than one model, or a term the
    record leaves out; the default is the single-model shape the harvest normally writes.
    """
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "sessions.json")
    if by_model is None:
        by_model = {"global.anthropic.claude-opus-4-8": {"total_tokens": 1234}}
    with open(path, "w") as f:
        json.dump({sid: {"spans": [], "window": None, "usage": {
            "total_tokens": 1234, "output_tokens": 34, "cache_read_tokens": 1000,
            "cache_write_tokens": 198, "input_tokens": 2, "llm_calls": 1,
            "byModel": by_model,
            "provenance": provenance,
        }}}, f)
    return path


def test_the_live_harvest_classifies_every_session_and_none_as_unclassified(
        tmp_path, monkeypatch, capsys):
    """The load-bearing property of this seam, and the one failure mode it has already hit.

    `improve.classify` reads NON-tool spans: `chat` and `execute_event_loop_cycle` decide whether
    a model ran, `mcp tools/list` identifies gateway warm-up, `POST /invocations` is the root.
    An `unclassified` run gets NO evaluators by design, so a span source that dropped those
    names would put every cloud session in that bucket and the producer would mine, propose and
    publish NOTHING - with no error anywhere. That is not hypothetical: the committed test
    fixture kept only tool spans for exactly this reason and all six golden sessions came out
    `unclassified`, which no test caught because the other checks read only derived facets.

    So this asserts the classes, not just that spans arrived.
    """
    fix = _fixture()
    sess = fix["sessions"]
    out = str(tmp_path / "harvest")
    _run_harvest(monkeypatch, out, delta_s=_delta_to_now(fix), shift_plan_rows=True)
    _point_readers_at(monkeypatch, out)

    source = HarvestedSpanSource()
    classes = {}
    for label, sid in sess.items():
        steps = trajectory_for_session(sid, source=source)
        assert steps, f"{label}: the live harvest produced no spans"
        classes[label] = classify(Run(run_id=sid, trajectory=steps)).cls

    assert WorkloadClass.UNCLASSIFIED not in classes.values(), (
        f"a live-harvested session classified as unclassified, so the producer would emit "
        f"nothing for it: {classes}")

    # A session that only ever warmed up gateways is `tool_discovery`, not a failed conversation.
    assert classes["spans_only"] is WorkloadClass.TOOL_DISCOVERY
    # The plan-bearing sessions invoked a model, so they carry agent behaviour to grade.
    for label in ("plan_and_spans_inline_edges", "plan_and_spans_offloaded_edges",
                  "plan_with_real_edges"):
        assert classes[label] is WorkloadClass.CONVERSATIONAL, label

    # And the class really does decide the objective, which is the point of classifying at all.
    names, gating, _ = resolve_plan(Run(run_id=sess["spans_only"],
                                        trajectory=trajectory_for_session(sess["spans_only"],
                                                                          source=source)))
    assert "goal_judge" not in names and gating == ("ToolDiscoveryValidator",)

    print("\n[classify] " + ", ".join(f"{k}={v.value}" for k, v in sorted(classes.items())))
