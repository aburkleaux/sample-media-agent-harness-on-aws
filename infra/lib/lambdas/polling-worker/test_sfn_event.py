"""Step Functions completion-event path of the polling worker. Offline; no AWS calls.

Run with:
    agent/.venv/bin/python -m pytest infra/lib/lambdas/polling-worker/test_sfn_event.py -q

The event path must (a) reuse the poller's own cycle rather than a copy of it, and
(b) be idempotent with the scheduled poll: whichever runs second must not write a
second terminal transition or flip anything back.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-table")

import handler  # noqa: E402

ARN = ("arn:aws:states:us-west-2:100000000002:execution:"
       "m2cv6-0e0000000002-media-processing:6f1c0a8e-run")


def _iso(seconds_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat()


def _event(arn=ARN, status="SUCCEEDED"):
    return {
        "version": "0",
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "executionArn": arn,
            "stateMachineArn": arn.replace(":execution:", ":stateMachine:").rsplit(":", 1)[0],
            "status": status,
        },
    }


class Registry:
    """In-memory JobRegistry with the REAL guards: set_terminal only from an active status."""

    def __init__(self, rows):
        self.rows = {r["jobId"]: dict(r) for r in rows}
        self.terminal_writes = []
        self.get_calls = 0

    def get_job(self, job_id):
        self.get_calls += 1
        row = self.rows.get(job_id)
        return dict(row) if row else None

    def query_active_jobs(self):
        return [dict(r) for r in self.rows.values() if r.get("status") in ("PENDING", "RUNNING")]

    def set_terminal(self, job_id, *, status, error=None, has_renderable_artifacts=False,
                     artifact_urls=None, result_key=""):
        row = self.rows[job_id]
        if row.get("status") not in ("PENDING", "RUNNING"):
            return False
        row.update(status=status, lastError=error, artifactUrls=list(artifact_urls or []),
                   resultKey=result_key)
        self.terminal_writes.append((job_id, status))
        return True

    def set_active_status(self, job_id, *, status, expected_status):
        row = self.rows[job_id]
        if row.get("status") != expected_status:
            return False
        row["status"] = status
        return True

    def bump_error(self, job_id, message):
        row = self.rows[job_id]
        row["consecutiveErrors"] = row.get("consecutiveErrors", 0) + 1
        return row["consecutiveErrors"]


def _job(**over):
    row = {"jobId": ARN, "gatewayId": "__aws_sfn__", "statusTool": "get_job_status",
           "actorId": "actor-1", "status": "RUNNING", "startAt": _iso(120)}
    row.update(over)
    return row


def _sfn_succeeds(monkeypatch, calls):
    def describe(job_id):
        calls.append(job_id)
        return {"status": "SUCCEEDED", "error": None,
                "executionOutput": {"output": {"s3uri": "s3://proxy/runs/x/"}}}
    monkeypatch.setattr(handler, "_describe_sfn_execution", describe)
    monkeypatch.setattr(handler, "_publish_job_result", lambda *a, **k: "jobs/x/result.json")


def test_event_settles_the_job_through_the_poll_cycle(monkeypatch):
    calls = []
    _sfn_succeeds(monkeypatch, calls)
    cycled = []
    real_cycle = handler._poll_cycle
    monkeypatch.setattr(handler, "_poll_cycle",
                        lambda jobs, *, registry: cycled.append(list(jobs)) or real_cycle(cycled[-1], registry=registry))
    reg = Registry([_job()])
    out = handler.handle_execution_event(_event(), registry=reg)
    # Same code path as the poller: the shared cycle ran over exactly the one job.
    assert [[j["jobId"] for j in c] for c in cycled] == [[ARN]]
    assert out["event"] == "settled" and out["terminal"] == 1
    assert calls == [ARN]  # authoritative status re-read, event status not trusted
    assert reg.terminal_writes == [(ARN, "SUCCEEDED")]
    assert reg.rows[ARN]["resultKey"] == "jobs/x/result.json"


def test_event_then_poll_writes_one_terminal_transition(monkeypatch):
    calls = []
    _sfn_succeeds(monkeypatch, calls)
    reg = Registry([_job()])
    handler.handle_execution_event(_event(), registry=reg)
    # The scheduled tick that follows sees no active job: nothing to poll, no second write.
    counters = handler._poll_cycle(reg.query_active_jobs(), registry=reg)
    assert counters["polled"] == 0
    assert reg.terminal_writes == [(ARN, "SUCCEEDED")]
    assert calls == [ARN]


def test_poll_then_event_is_a_noop(monkeypatch):
    calls = []
    _sfn_succeeds(monkeypatch, calls)
    reg = Registry([_job()])
    handler._poll_cycle(reg.query_active_jobs(), registry=reg)
    out = handler.handle_execution_event(_event(), registry=reg)
    assert out == {"event": "ignored", "reason": "already_terminal"}
    assert reg.terminal_writes == [(ARN, "SUCCEEDED")]
    assert calls == [ARN]  # the late event made no DescribeExecution call


def test_concurrent_event_and_poll_on_a_stale_read_land_once(monkeypatch):
    """Both read the row while it was RUNNING; the registry guard admits one terminal write."""
    calls = []
    _sfn_succeeds(monkeypatch, calls)
    reg = Registry([_job()])
    stale = reg.query_active_jobs()  # the poller's GSI page, read before the event lands
    handler.handle_execution_event(_event(), registry=reg)
    handler._poll_cycle(stale, registry=reg)
    assert reg.terminal_writes == [(ARN, "SUCCEEDED")]
    assert reg.rows[ARN]["status"] == "SUCCEEDED"


def test_event_for_an_execution_that_is_not_a_job_is_ignored(monkeypatch):
    monkeypatch.setattr(handler, "_describe_sfn_execution",
                        lambda job_id: (_ for _ in ()).throw(AssertionError("must not describe")))
    reg = Registry([])
    other = "arn:aws:states:us-west-2:100000000002:execution:mediaworks-harness-asset-ingestion:abc"
    assert handler.handle_execution_event(_event(other), registry=reg) == {
        "event": "ignored", "reason": "not_a_job"}


def test_event_without_execution_arn_is_ignored():
    reg = Registry([_job()])
    ev = _event()
    ev["detail"].pop("executionArn")
    assert handler.handle_execution_event(ev, registry=reg)["reason"] == "no_execution_arn"
    assert reg.get_calls == 0


def test_failed_execution_records_the_cause(monkeypatch):
    monkeypatch.setattr(handler, "_describe_sfn_execution",
                        lambda job_id: {"status": "FAILED", "error": "States.TaskFailed: boom"})
    monkeypatch.setattr(handler, "_publish_job_result", lambda *a, **k: "")
    reg = Registry([_job()])
    handler.handle_execution_event(_event(status="FAILED"), registry=reg)
    assert reg.terminal_writes == [(ARN, "FAILED")]
    assert reg.rows[ARN]["lastError"] == "States.TaskFailed: boom"


def test_coordinator_rows_are_still_skipped(monkeypatch):
    monkeypatch.setattr(handler, "_invoke_status",
                        lambda job: (_ for _ in ()).throw(AssertionError("must not poll")))
    reg = Registry([_job(gatewayId="__coordinator__")])
    out = handler.handle_execution_event(_event(), registry=reg)
    assert out["skipped_coordinator"] == 1 and reg.terminal_writes == []


def test_handler_routes_sfn_events_and_keeps_the_schedule(monkeypatch):
    routed, swept = [], []
    reg = Registry([_job()])
    monkeypatch.setattr(handler, "_get_registry", lambda: reg)
    monkeypatch.setattr(handler, "handle_execution_event",
                        lambda event, *, registry: routed.append(event) or {"event": "settled"})
    monkeypatch.setattr(handler, "_poll_cycle",
                        lambda jobs, *, registry: swept.append(list(jobs)) or {"polled": 0})
    monkeypatch.setattr(handler, "_publish_metrics", lambda counters: None)
    handler.handler(_event(), None)
    assert len(routed) == 1 and swept == []
    # A scheduled-event payload still runs the full sweep.
    handler.handler({"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}, None)
    assert len(routed) == 1 and len(swept) == 1
