#!/usr/bin/env python3
"""Regenerate tests/fixtures/async_lifecycle_sessions.json from `aws/spans` (READ-ONLY).

REQ-017 requires the offline improve tests to run on REAL harvested data. The async-lifecycle
facets (`dispatch_unpaired_result_unobserved`, `session_ended_on_unretrieved_dispatch`) have to be
tested on the three sessions that are the ground truth for V-02a/b/c, and **none of them is in the
golden set**, so `plan_state_golden.json` cannot carry them:

  36fafeb7  FAIL  2 x dispatch_tool, ZERO retrieves, results returned INLINE (sync_mode)
  23a1fa24  PASS  11 x dispatch_tool over 3h, ZERO retrieves, results returned INLINE
  74b0a063  PASS  dispatch -> get_job -> get_job_result, then a LATE unpaired dispatch

They are also spread over a week, so no single `harvest_live` window contains all three (and
the two on 2026-08-27 fall in a 24h window that hits the 5000-row Insights cap, the truncation
`evidence_complete` exists to report). Each is therefore pulled by its own narrow,
session-filtered query.

Session ids ARE hardcoded here, unlike `refresh_improve_fixtures.py`'s shape-selected
exemplars, because these three sessions ARE the ground truth: V-02a names two of them and
V-02c the third. A shape-selected substitute would silently stop testing the shapes these
facets exist for.

STRICTLY READ-ONLY against AWS: `logs:StartQuery` / `logs:GetQueryResults`. No writes.
Staging (100000000002) only.

  cd agent && AWS_PROFILE=megs-staging uv run python scripts/refresh_async_lifecycle_fixture.py

Carries span name + status + duration_ms only. Deliberately excludes tool arguments, tool
results and status messages: arguments/results are sensitive (OURS-4) and none of the three is
needed by these tests.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "tests", "fixtures", "async_lifecycle_sessions.json")
LOG_GROUP = "aws/spans"

# prefix -> (why it is here, narrow window that contains the WHOLE run)
SESSIONS = {
    "36fafeb7": ("V-02a: dispatched twice, retrieved nothing, results returned inline; FAIL",
                 "2026-08-27T19:00:00Z", "2026-08-27T21:00:00Z"),
    "23a1fa24": ("V-02a: 11 dispatches over 3h, retrieved nothing, results returned inline; PASS",
                 "2026-08-21T19:00:00Z", "2026-08-22T00:00:00Z"),
    "74b0a063": ("V-02c: retrieved early (get_job/get_job_result) then left a LATE dispatch "
                 "unpaired at the session's end; PASS",
                 "2026-08-27T12:00:00Z", "2026-08-27T13:30:00Z"),
}

# Row ceiling, named once and threaded into BOTH the query text and the API parameter. They must
# agree: Insights truncates at whichever is lower and says nothing, so two literals that drifted
# apart would silently clip a session's tail — and the tail is exactly what the facets read.
ROW_LIMIT = 10000

# Same projection `harvest_live.harvest_spans` uses, plus a session filter so the query cannot
# hit the 5000-row cap the way a whole-day window does.
QUERY = ('fields @timestamp, attributes.session.id as sid, '
         'attributes.aws.local.service as svc, name, '
         'status.code as status, durationNano '
         '| filter sid like /^{prefix}/ '
         f'| sort @timestamp asc | limit {ROW_LIMIT}')


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _query(logs, prefix: str, start: str, end: str) -> list:
    qid = logs.start_query(logGroupName=LOG_GROUP, startTime=_epoch(start),
                           endTime=_epoch(end), queryString=QUERY.format(prefix=prefix),
                           limit=ROW_LIMIT)["queryId"]
    for _ in range(180):
        time.sleep(1.5)
        res = logs.get_query_results(queryId=qid)
        if res.get("status") == "Complete":
            rows = [{f["field"]: f.get("value") for f in row} for row in res.get("results", [])]
            # Fail loud rather than commit a clipped fixture: at the ceiling we cannot tell a
            # complete session from a truncated one, and a fixture missing its tail would
            # silently invert the very facets it exists to pin.
            if len(rows) >= ROW_LIMIT:
                raise SystemExit(f"{prefix}: hit the {ROW_LIMIT}-row ceiling — narrow the window")
            return rows
        if res.get("status") in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Insights query for {prefix} ended {res.get('status')}")
    raise TimeoutError(prefix)


def _ts_key(raw):
    """Parsed instant, or None. Insights `@timestamp` is offset-LESS UTC.

    Sorting on the raw string is wrong across the formats this system emits
    (`'...19.5Z' > '...19.5+00:00'` because `'Z' > '+'`), and step ORDER is exactly what the
    async-lifecycle facets read.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    a = ap.parse_args()

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    who = session.client("sts").get_caller_identity()
    if who["Account"] != "100000000002":
        raise SystemExit(f"refusing to run against account {who['Account']} — staging only")
    logs = session.client("logs")

    out = {}
    for prefix, (why, start, end) in sorted(SESSIONS.items()):
        rows = _query(logs, prefix, start, end)
        rows.sort(key=lambda r: (_ts_key(r.get("@timestamp")) is None,
                                 _ts_key(r.get("@timestamp")) or datetime.min.replace(
                                     tzinfo=timezone.utc)))
        sids = {r.get("sid") for r in rows if r.get("sid")}
        if len(sids) != 1:
            raise SystemExit(f"{prefix}: expected exactly one session id, got {sorted(sids)}")
        out[prefix] = {
            "session_id": sids.pop(),
            "why": why,
            "window": {"start": start, "end": end},
            "spans": [{"name": r.get("name") or "",
                       "status": r.get("status") or "UNSET",
                       "duration_ms": (int(r["durationNano"]) // 1_000_000)
                       if r.get("durationNano") else None} for r in rows],
        }
        print(f"  {prefix}: {len(out[prefix]['spans'])} spans  ({start} .. {end})")

    meta = {"_provenance": {
        "source": "CloudWatch Logs Insights over the `aws/spans` log group, staging "
                  "(100000000002, us-west-2), read-only StartQuery/GetQueryResults",
        "query": QUERY.format(prefix="<session-id prefix>"),
        "sessions": "the three ground-truth sessions for the async-lifecycle facets "
                    "(V-02a/b/c); NOT golden-set members",
        "fields_included": "ordered span name + status.code + durationNano/1e6, per session",
        "deliberately_excluded": "tool arguments, tool results, status messages, goal text, "
                                 "S3 URIs (args+results are sensitive — OURS-4; the messages "
                                 "are simply not needed here)",
        "why_committed": "REQ-017 needs real fixtures, and no single harvest window contains "
                         "all three sessions",
        "regenerate": "cd agent && AWS_PROFILE=megs-staging uv run python "
                      "scripts/refresh_async_lifecycle_fixture.py",
    }}
    with open(OUT, "w") as f:
        json.dump({**meta, "sessions": out}, f, indent=1, sort_keys=True)
    print(f"wrote {os.path.relpath(OUT)}: {len(out)} sessions")


if __name__ == "__main__":
    main()
