#!/usr/bin/env python3
"""Put real `traceId` evidence into the committed improve fixtures, from `aws/spans` (READ-ONLY).

Two jobs, one subject — the span field that links a plan sub-session to the request that spawned
it. Both write real harvested values; neither invents one.

  tests/fixtures/trace_parent_sessions.json     NEW. Real requests that fanned out into several
                                                sub-sessions, as raw Insights rows.
  tests/fixtures/improve_harvest_live_aws.json  BACKFILL of `traceId` onto the span rows already
                                                there. That fixture is the producer/reader seam's
                                                fake AWS, and it was lifted from a harvest taken
                                                before this field was projected — so without the
                                                backfill the fake answers with a field the real
                                                query now asks for and the seam could not tell a
                                                producer that projects it from one that does not.
                                                Every row is matched on (session, timestamp, span
                                                name) and the script refuses to write if any row
                                                fails to match, so no value is guessed.

REQ-017 requires the offline improve tests to run on REAL harvested data. The fact under test
is that a plan sub-session and the request that spawned it share an OTEL **trace**, which is
what `observe.span_trajectory.parents_by_session` joins on — so the fixture has to carry real
`traceId` values for a real request that fanned out into several sub-sessions.

**No existing fixture can carry it.** `plan_state_golden.json` covers the golden set, which holds
no sub-session at all; its `exemplars` block has one `plan_node` session but no sibling and no
parent, so it cannot show two siblings agreeing on a key. `improve_harvest_live_aws.json` holds
four conversations and no sub-session. And neither was harvested with `traceId` projected, because
until now nothing read it: it is a TOP-LEVEL span field, not an attribute, which is why every
audit of `attributes.*` in this store missed it.

Groups are SHAPE-selected, not pinned by id, for the reason `refresh_improve_fixtures.py` gives:
an id list rots, and here it rots fast — `aws/spans` retention is 30 days, so the sessions that
demonstrate a fan-out today are gone next month while the shape is permanent. The script takes the
two requests with the most sub-sessions in the window and records which ones it chose.

Two groups rather than one, because a de-duplication has two ways to be wrong: it can fail to
merge siblings, and it can merge two different requests. One group proves the first, two prove the
second.

Row selection, which is a subset and says so:

  sub-sessions   EVERY span, so the reconstructed trajectory is the real one.
  the parent     the FIRST span of each trace it appears in. The join reads only the
                 (session, trace) association, and a busy request carries hundreds of spans over
                 dozens of traces — measured, one parent here has 403 spans over 6 traces and
                 another 1,655 over 27 — which would inflate this file by an order of magnitude
                 without changing one assertion. Keeping every TRACE (not every span) is what
                 matters, and it also preserves the fact that a request spans many of them.

STRICTLY READ-ONLY against AWS: `logs:StartQuery` / `logs:GetQueryResults`. No writes.
Staging (100000000002) only.

  cd agent && AWS_PROFILE=megs-staging uv run python scripts/refresh_trace_fixtures.py

Carries session id, trace id, span name, status, service and durationNano. Deliberately excludes
status messages, tool arguments and tool results: arguments/results are sensitive (OURS-4) and
none of the three is needed to join a trace to a parent.

RETENTION BITES BOTH WAYS. `aws/spans` keeps 30 days, so re-running this after the seam fixture's
rows have aged out cannot recover their trace ids and the backfill will refuse rather than write
partial data. That is the intended failure: the committed fixture is the durable copy.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "tests", "fixtures", "trace_parent_sessions.json")
SEAM = os.path.join(HERE, "..", "tests", "fixtures", "improve_harvest_live_aws.json")
LOG_GROUP = "aws/spans"
ACCOUNT = "100000000002"

sys.path.insert(0, os.path.join(HERE, "..", "src"))
from improve.harvest_live import TRACE_FIELD  # noqa: E402
from improve.observe.span_trajectory import PLAN_SESSION_PREFIX  # noqa: E402

# How many fanned-out requests to commit. Two: one shows siblings merging, two show that separate
# requests do not. A third would add bytes and no new claim.
GROUPS = 2

# How far back to look for them. 14 days is the whole of `aws/spans` retention that is reliably
# present (the store keeps 30, but a window that long routinely straddles a schema change), and it
# is the window the trace linkage was measured over.
LOOKBACK_DAYS = 14

# Row ceiling, named once and threaded into BOTH the query text and the API parameter. They must
# agree: Insights truncates at whichever is lower and says nothing, so two literals that drifted
# apart would silently drop a session's spans — and a sub-session missing from the fixture is a
# sibling that cannot be shown to merge.
ROW_LIMIT = 10000

# Group ceiling for the `stats` discovery query. Groups, not records, so this is a different bound
# from ROW_LIMIT and deliberately not the same literal.
GROUP_LIMIT = 20000

# Which (trace, session) pairs exist. `stats` returns GROUPS, so this is immune to the row cap that
# forces the real harvester to subdivide its windows.
PAIRS_QUERY = (
    f'fields {TRACE_FIELD} as trace, attributes.session.id as sid '
    '| filter attributes.aws.local.service like /mediaworks_harness/ '
    '| filter ispresent(trace) and ispresent(sid) '
    f'| stats count() as n by trace, sid | limit {GROUP_LIMIT}'
)

# The spans themselves. Exactly `harvest_live.harvest_spans`' projection minus `status.message`,
# plus a session filter so the read cannot hit the row cap the way a whole-day window does.
ROWS_QUERY = (
    'fields @timestamp, attributes.session.id as sid, '
    'attributes.aws.local.service as svc, name, '
    f'status.code as status, durationNano, {TRACE_FIELD} as trace '
    '| filter {filt} '
    f'| sort @timestamp asc | limit {ROW_LIMIT}'
)


def _query(logs, query: str, start: int, end: int) -> list:
    qid = logs.start_query(logGroupName=LOG_GROUP, startTime=start, endTime=end,
                           queryString=query, limit=ROW_LIMIT)["queryId"]
    for _ in range(180):
        time.sleep(1.5)
        res = logs.get_query_results(queryId=qid)
        status = res.get("status")
        if status == "Complete":
            return [{f["field"]: f.get("value") for f in row} for row in res.get("results", [])]
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Insights query ended {status}")
    raise TimeoutError("Insights query did not complete")


def _groups(pairs: list) -> list:
    """``[(parent, [children])]`` biggest first, from the same rule the reader joins on.

    Deliberately re-derived here rather than imported: this script has to be able to report that
    the exactness property HELD on the window it read (one candidate parent per trace, no
    sub-session in two traces), and a generator that borrowed the reader's answer could not.
    """
    by_trace = defaultdict(set)
    for row in pairs:
        by_trace[row["trace"]].add(row["sid"])
    children_of = defaultdict(set)
    ambiguous = 0
    orphan_traces = 0
    for members in by_trace.values():
        children = {s for s in members if s.startswith(PLAN_SESSION_PREFIX)}
        candidates = members - children
        if not children:
            continue
        if len(candidates) > 1:
            ambiguous += 1
            continue
        if not candidates:
            orphan_traces += 1
            continue
        children_of[next(iter(candidates))] |= children
    subs = {s for members in by_trace.values() for s in members
            if s.startswith(PLAN_SESSION_PREFIX)}
    linked = {c for kids in children_of.values() for c in kids}
    print(f"  {len(by_trace)} traces, {len(subs)} sub-session(s): {len(linked)} linked to exactly "
          f"one parent, {len(subs) - len(linked)} not; {ambiguous} trace(s) with more than one "
          f"candidate parent, {orphan_traces} with none")
    if ambiguous:
        raise SystemExit("a trace named more than one candidate parent — the join is no longer "
                         "exact on this window; re-measure before committing a fixture")
    return sorted(((p, sorted(k)) for p, k in children_of.items()),
                  key=lambda pk: (-len(pk[1]), pk[0]))


def _backfill_seam_fixture(logs) -> None:
    """Add the real ``trace`` to the seam fixture's existing span rows, or refuse.

    Matched on (session id, ``@timestamp``, span name), which is unique per row in this store.
    Nothing is invented and nothing is dropped: a single unmatched row aborts the write, because a
    fixture where some rows carry a trace and others do not would make the reader's "this harvest
    predates the projection" branch fire for reasons that have nothing to do with the producer.
    """
    with open(SEAM) as f:
        fixture = json.load(f)
    rows = fixture["insights_span_rows"]
    sids = sorted({r["sid"] for r in rows})
    stamps = [datetime.strptime(r["@timestamp"], "%Y-%m-%d %H:%M:%S.%f").replace(
        tzinfo=timezone.utc).timestamp() for r in rows]
    filt = " or ".join(f'sid = "{s}"' for s in sids)
    live = _query(logs, ROWS_QUERY.format(filt=filt), int(min(stamps)) - 60, int(max(stamps)) + 60)
    found = {(r.get("sid"), r.get("@timestamp"), r.get("name") or ""): r.get("trace")
             for r in live}
    missing = [r for r in rows
               if not found.get((r["sid"], r["@timestamp"], r.get("name") or ""))]
    if missing:
        raise SystemExit(
            f"{len(missing)} of {len(rows)} seam-fixture row(s) have no trace in the log group "
            f"any more (retention is 30 days; the oldest row here is "
            f"{min(r['@timestamp'] for r in rows)}) — refusing to write a partial backfill")
    for row in rows:
        row["trace"] = found[(row["sid"], row["@timestamp"], row.get("name") or "")]
    # `_provenance` here is one prose string, not the dict the other fixtures use. Appended to
    # rather than restructured, and only once, so re-running is idempotent.
    note = (f" `trace` was added to every span row after the fact by "
            f"scripts/refresh_trace_fixtures.py, joined on (session, @timestamp, span name) "
            f"against the same log group: the rows were lifted before the harvester projected "
            f"{TRACE_FIELD}, which is a TOP-LEVEL span field and not an attribute, so no earlier "
            f"harvest could have carried it.")
    existing = str(fixture.get("_provenance") or "")
    if "refresh_trace_fixtures.py" not in existing:
        fixture["_provenance"] = existing + note
    with open(SEAM, "w") as f:
        # Key order left alone (no `sort_keys`), unlike the file this script CREATES. This one
        # already exists and is hand-maintained, so sorting it would reformat 1,800 lines around a
        # 49-line change and bury the only part of it that differs.
        json.dump(fixture, f, indent=1)
    traces = len({r["trace"] for r in rows})
    print(f"  backfilled {len(rows)} row(s) across {len(sids)} session(s) and {traces} trace(s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    a = ap.parse_args()

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    who = session.client("sts").get_caller_identity()
    if who["Account"] != ACCOUNT:
        raise SystemExit(f"refusing to run against account {who['Account']} — staging only")
    logs = session.client("logs")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    print(f"[pairs] {start.isoformat()} .. {end.isoformat()}")
    groups = _groups(_query(logs, PAIRS_QUERY, int(start.timestamp()), int(end.timestamp())))
    chosen = groups[:GROUPS]
    if len(chosen) < GROUPS or any(len(kids) < 2 for _, kids in chosen):
        raise SystemExit(f"need {GROUPS} requests with at least 2 sub-sessions each; the window "
                         f"offers {[len(k) for _, k in groups[:GROUPS]]}")

    out = {}
    for rank, (parent, kids) in enumerate(chosen):
        sids = [parent, *kids]
        filt = " or ".join(f'sid = "{s}"' for s in sids)
        # The children's epochs bound the window; pad it so the parent's own spans, which open
        # before the first child and close after the last, are inside it.
        epochs = [int(k.split("-")[2]) for k in kids]
        win_start = min(epochs) - 3600
        win_end = max(epochs) + 3 * 3600
        rows = _query(logs, ROWS_QUERY.format(filt=filt), win_start, win_end)
        if len(rows) >= ROW_LIMIT:
            raise SystemExit(f"{parent}: hit the {ROW_LIMIT}-row ceiling — narrow the window")
        kept = []
        seen_parent_traces = set()
        for row in sorted(rows, key=lambda r: r.get("@timestamp") or ""):
            sid = row.get("sid") or ""
            if sid == parent:
                if row.get("trace") in seen_parent_traces:
                    continue
                seen_parent_traces.add(row.get("trace"))
            kept.append({"@timestamp": row.get("@timestamp"), "sid": sid,
                         "svc": row.get("svc"), "name": row.get("name") or "",
                         "status": row.get("status") or "UNSET",
                         "durationNano": row.get("durationNano"),
                         "trace": row.get("trace")})
        out[f"fanout_{rank + 1}"] = {
            "parent_session": parent,
            "sub_sessions": kids,
            "why": (f"one request that fanned out into {len(kids)} plan sub-session(s); the "
                    f"parent appears in {len(seen_parent_traces)} trace(s)"),
            "window": {"start": datetime.fromtimestamp(win_start, timezone.utc).isoformat(),
                       "end": datetime.fromtimestamp(win_end, timezone.utc).isoformat()},
            "insights_span_rows": kept,
        }
        print(f"  fanout_{rank + 1}: parent {parent} + {len(kids)} sub-session(s), "
              f"{len(kept)} of {len(rows)} span row(s) kept "
              f"({len(seen_parent_traces)} parent trace(s))")

    meta = {"_provenance": {
        "source": "CloudWatch Logs Insights over the `aws/spans` log group, staging "
                  f"({ACCOUNT}, us-west-2), read-only StartQuery/GetQueryResults",
        "queries": {"pairs": PAIRS_QUERY, "rows": ROWS_QUERY.format(filt="<session-id filter>")},
        "sessions": f"the {GROUPS} requests with the most plan sub-sessions in the last "
                    f"{LOOKBACK_DAYS} days, chosen by shape (not by id, which retention rots)",
        "fields_included": "session id, traceId, span name, status.code, aws.local.service, "
                           "durationNano — the harvester's own projection",
        "row_selection": "every span of each sub-session; for the parent, the first span of each "
                         "trace it appears in (the join reads only the session/trace association)",
        "deliberately_excluded": "status messages, goal text, tool arguments, tool results "
                                 "(args+results are sensitive — OURS-4)",
        "why_committed": "REQ-017 needs real fixtures, and no other committed fixture holds a "
                         "sub-session with a sibling, a parent, or a traceId at all",
        "regenerate": "cd agent && AWS_PROFILE=megs-staging uv run python "
                      "scripts/refresh_trace_fixtures.py",
    }}
    with open(OUT, "w") as f:
        json.dump({**meta, "groups": out}, f, indent=1, sort_keys=True)
    print(f"wrote {os.path.relpath(OUT)}: {len(out)} fanned-out request(s)")

    print(f"[seam] backfilling {TRACE_FIELD} onto {os.path.relpath(SEAM)}")
    _backfill_seam_fixture(logs)


if __name__ == "__main__":
    main()
