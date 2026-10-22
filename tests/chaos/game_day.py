#!/usr/bin/env python
"""Chaos game-day harness (STAGING ONLY) — inject failures, verify recovery.

Validates the P0/P1 resilience controls live. Each experiment injects a fault
into a NON-production environment and asserts the expected recovery, then
cleans up. Refuses to run unless the caller's account equals GAME_DAY_ACCOUNT
(the account you intend to break), or --force.

Usage:
  GAME_DAY_ACCOUNT=<account-id> AWS_PROFILE=<profile> python tests/chaos/game_day.py stuck-job
  GAME_DAY_ACCOUNT=<account-id> AWS_PROFILE=<profile> python tests/chaos/game_day.py dlq-alarm
  python tests/chaos/game_day.py gateway-breaker            # local, no AWS
  GAME_DAY_ACCOUNT=<account-id> AWS_PROFILE=<profile> python tests/chaos/game_day.py all

Experiments:
  C1 stuck-job     — insert a 2h-old RUNNING job, invoke the polling worker,
                     assert it's swept to TIMED_OUT.
  C2 dlq-alarm     — send a message to the changefeed DLQ, assert depth > 0
                     (drives ChangefeedDlqDepthAlarm), then purge.
  C3 gateway-breaker — real GatewayConnection vs a dead endpoint: assert the
                     circuit opens after the threshold (local; no AWS).
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

REGION = "us-west-2"
# The account the experiments may break; never defaulted, so a stray run cannot hit prod.
GAME_DAY_ACCOUNT = os.environ.get("GAME_DAY_ACCOUNT", "")
JOB_TABLE = "mediaworks-harness-job-registry"
POLL_FN = "mediaworks-harness-job-polling-worker"
CHANGEFEED_DLQ = "mediaworks-harness-job-changefeed-dlq"


def _guard(session, force: bool) -> None:
    acct = session.client("sts").get_caller_identity()["Account"]
    if (not GAME_DAY_ACCOUNT or acct != GAME_DAY_ACCOUNT) and not force:
        sys.exit(f"REFUSING: account {acct} is not GAME_DAY_ACCOUNT ({GAME_DAY_ACCOUNT or 'unset'}). Use --force to override.")


def c1_stuck_job(session) -> bool:
    ddb = session.client("dynamodb")
    lam = session.client("lambda")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    job = f"chaos-stuck-{int(time.time())}"
    print(f"[C1] inserting stuck job {job} (startAt={old})")
    ddb.put_item(TableName=JOB_TABLE, Item={
        "jobId": {"S": job}, "actorId": {"S": "chaos"}, "sessionId": {"S": "chaos"},
        "status": {"S": "RUNNING"}, "pollPartition": {"S": "ACTIVE"},
        "startAt": {"S": old}, "updatedAt": {"S": old},
        "gatewayId": {"S": "__aws_sfn__"}, "statusTool": {"S": "x___get_job_status"},
        "resultsTool": {"S": ""}, "trioId": {"S": "chaos|x"}, "consecutiveErrors": {"N": "0"},
    })
    lam.invoke(FunctionName=POLL_FN, InvocationType="RequestResponse")
    item = ddb.get_item(TableName=JOB_TABLE, Key={"jobId": {"S": job}}).get("Item", {})
    status = item.get("status", {}).get("S")
    ddb.delete_item(TableName=JOB_TABLE, Key={"jobId": {"S": job}})
    ok = status == "TIMED_OUT"
    print(f"[C1] {'PASS' if ok else 'FAIL'}: job status={status} (expected TIMED_OUT); cleaned up")
    return ok


def c2_dlq_alarm(session) -> bool:
    sqs = session.client("sqs")
    url = sqs.get_queue_url(QueueName=CHANGEFEED_DLQ)["QueueUrl"]
    print(f"[C2] sending a chaos message to the changefeed DLQ")
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"chaos": True, "ts": time.time()}))
    time.sleep(3)
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
    depth = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
    print(f"[C2] DLQ depth={depth} (ChangefeedDlqDepthAlarm fires on depth>0 within its eval period)")
    sqs.purge_queue(QueueUrl=url)
    ok = depth > 0
    print(f"[C2] {'PASS' if ok else 'FAIL'}: depth>0={ok}; purged DLQ")
    return ok


def c3_gateway_breaker() -> bool:
    # Fast-fail config so the experiment runs in seconds.
    os.environ["GATEWAY_TOKEN_TIMEOUT"] = "2"
    os.environ["GATEWAY_TOKEN_MAX_ATTEMPTS"] = "1"
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agent", "src"))
    import mcp_manager as m
    conn = m.GatewayConnection("chaos", {
        "url": "http://127.0.0.1:1/mcp", "token_endpoint": "http://127.0.0.1:1/token",
        "client_id": "c", "client_secret": "s", "scope": "x",
    })
    fails = 0
    for _ in range(m._CB_FAILURE_THRESHOLD):
        try:
            conn.connect()
        except Exception:
            fails += 1
    # Breaker should now be open — next connect fails fast with "circuit open".
    fast_fail = False
    try:
        conn.connect()
    except Exception as e:
        fast_fail = "circuit open" in str(e)
    ok = fails == m._CB_FAILURE_THRESHOLD and fast_fail
    print(f"[C3] {'PASS' if ok else 'FAIL'}: {fails} real failures then circuit-open fast-fail={fast_fail}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=["stuck-job", "dlq-alarm", "gateway-breaker", "all"])
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    results = {}
    if args.experiment in ("gateway-breaker", "all"):
        results["C3 gateway-breaker"] = c3_gateway_breaker()
    if args.experiment in ("stuck-job", "dlq-alarm", "all"):
        import boto3
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        _guard(session, args.force)
        if args.experiment in ("stuck-job", "all"):
            results["C1 stuck-job"] = c1_stuck_job(session)
        if args.experiment in ("dlq-alarm", "all"):
            results["C2 dlq-alarm"] = c2_dlq_alarm(session)

    print("\n=== game-day results ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
