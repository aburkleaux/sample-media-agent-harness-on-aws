"""Project-deletion purge (projects-access Req 1.23–1.26, ingestion Req 10.20–10.22; design Component 15).

Two handlers in one module:

- :func:`trigger_handler` — the projects-table stream, filtered to a META ``MODIFY`` from ``status = active`` to
  ``status = deleted``, plus a daily sweep (a filtered Scan for deleted, not-yet-tombstoned META older than 1 h,
  because stream records expire after 24 h). Both start ``ProjectPurge`` named ``purge-<pid>``, so a repeat
  start is a no-op.
- :func:`handler` — PurgeTick, one step of the purge loop (``Tick → Choice → Wait``), state in
  ``META.purge {state, step, failures, notBefore, error?}``:

  ``wait``        in-flight indexing jobs terminal or 1 h old (AssetIngestion and CollectionRebuild runs stop on
                  their own: every tick ends when META is not ``active``);
  ``collection``  ``delete_collection(pid)``, not-found = success;
  ``uploads``     abort every incomplete multipart upload under ``projects/<pid>/``;
  ``objects``     delete every object version and delete marker under ``projects/<pid>/``, until a list returns
                  neither;
  ``records``     delete every item of the Project's partition except META, then replace META with the
                  tombstone (condition ``status = deleted``).

Only this role holds ``s3:DeleteObjectVersion``. Objects registered in place from other buckets and every
``artifacts/`` object are outside ``projects/<pid>/`` and are never listed. A step failing 3 times ≥ 30 s
apart writes ``purge = {state: delete_failed, step, error}`` and the loop ends; the next sweep does not retry a
failed purge automatically (an operator re-runs it).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from projects import schema
from projects.ids import is_project_id

from .. import collection, reasons
from ..indexer import STATUS_TOOL
from ..m2c import M2CClient, M2CConfig, M2CError
from ..pipeline_store import from_ddb, to_ddb
from ..store import _iso_now

log = logging.getLogger()
log.setLevel(logging.INFO)

STEPS = ("wait", "collection", "uploads", "objects", "records")
STEP_NAMES = {"wait": "purge wait", "collection": "purge collection", "uploads": "purge uploads",
              "objects": "purge objects", "records": "purge records"}
FAILURES_TO_FAIL = 3
RETRY_GAP_S = 30
WAIT_LIMIT_S = 3600
SWEEP_MIN_AGE_S = 3600
BUDGET_S = 600  # work per tick, under the 900 s Lambda timeout


def execution_name(pid: str) -> str:
    return f"purge-{pid}"


@dataclass
class PurgeDeps:
    table: Any
    s3: Any
    m2c_admin: Any
    ingest_bucket: str
    clock: Callable[[], float] = time.time


def _out(event: dict, done: bool, wait_s: int = 15) -> dict:
    return {"pid": event["pid"], "done": done, "wait_s": int(wait_s)}


class PurgeTick:
    def __init__(self, deps: PurgeDeps):
        self.d = deps
        self.t = deps.table

    def _meta(self, pid: str) -> dict:
        return from_ddb(self.t.get_item(Key=schema.meta_key(pid), ConsistentRead=True).get("Item") or {})

    def _set_purge(self, pid: str, purge: dict) -> None:
        self.t.update_item(Key=schema.meta_key(pid), UpdateExpression="SET #pg = :p",
                           ConditionExpression="#st = :deleted", ExpressionAttributeNames={"#st": "status", "#pg": "purge"},
                           ExpressionAttributeValues={":p": to_ddb(purge), ":deleted": "deleted"})

    def run(self, event: dict) -> dict:
        pid = str(event.get("pid") or "")
        if not is_project_id(pid):
            return _out({"pid": pid}, True)
        now = self.d.clock()
        meta = self._meta(pid)
        if meta.get("tombstone") or meta.get("status") != "deleted":
            return _out(event, True)  # purged already, or not a deleted Project (never touch a live one)
        purge = dict(meta.get("purge") or {"state": "running", "step": "wait", "failures": 0})
        if purge.get("state") == "delete_failed":
            return _out(event, True)
        step = purge.get("step") or "wait"
        if event.get("abort"):  # the state machine's Catch after its retries
            self._set_purge(pid, {**purge, "state": "delete_failed", "error": reasons.compose(
                STEP_NAMES.get(step, "purge wait"), f"internal error: {str(event.get('error') or '')[:300]}")})
            log.error("ProjectPurgeFailed pid=%s step=%s (aborted)", pid, step)
            return _out(event, True)
        if now < float(purge.get("notBefore") or 0):
            return _out(event, False, RETRY_GAP_S)
        try:
            finished = getattr(self, f"_{step}")(pid, meta, now)
        except (M2CError, ClientError) as exc:
            n = int(purge.get("failures") or 0) + 1
            if n >= FAILURES_TO_FAIL:
                self._set_purge(pid, {**purge, "state": "delete_failed", "failures": n,
                                      "error": reasons.compose(STEP_NAMES[step], str(exc))})
                log.error("ProjectPurgeFailed pid=%s step=%s", pid, step)
                return _out(event, True)
            self._set_purge(pid, {**purge, "failures": n, "notBefore": now + RETRY_GAP_S})
            return _out(event, False, RETRY_GAP_S)
        if not finished:
            return _out(event, False, 15 if step != "wait" else 30)
        if step == "records":
            return _out(event, True)  # META is the tombstone now
        nxt = STEPS[STEPS.index(step) + 1]
        self._set_purge(pid, {"state": "running", "step": nxt, "failures": 0})
        return _out(event, False, 1)

    # ------------------------------------------------------------------------------- steps
    def _wait(self, pid: str, meta: dict, now: float) -> bool:
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX),
                        "FilterExpression": Attr("status").eq("indexing"), "ConsistentRead": True}
        while True:
            resp = self.t.query(**kwargs)
            for row in resp.get("Items", []):
                run = (from_ddb(row.get("index") or {}).get("run") or {})
                job, sub = run.get("job_id"), run.get("submittedAt")
                if job and sub is not None and now - float(sub) <= WAIT_LIMIT_S:
                    st = self.d.m2c_admin.call_tool(STATUS_TOOL, {"job_id": job}).get("status")
                    if st not in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
                        return False
            if not resp.get("LastEvaluatedKey"):
                return True
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def _collection(self, pid: str, meta: dict, now: float) -> bool:
        if (meta.get("collectionState") or "absent") != "absent" or int(meta.get("incarnation") or 0) > 0:
            collection.delete(self.d.m2c_admin, pid)
        return True

    def _prefix(self, pid: str) -> str:
        p = schema.project_prefix(pid)
        assert p.startswith("projects/p") and p.endswith("/") and len(p) == len("projects/") + 34
        return p

    def _uploads(self, pid: str, meta: dict, now: float) -> bool:
        prefix = self._prefix(pid)
        while True:
            resp = self.d.s3.list_multipart_uploads(Bucket=self.d.ingest_bucket, Prefix=prefix)
            ups = resp.get("Uploads") or []
            for u in ups:
                if u["Key"].startswith(prefix):
                    self.d.s3.abort_multipart_upload(Bucket=self.d.ingest_bucket, Key=u["Key"], UploadId=u["UploadId"])
            if not ups:
                return True

    def _objects(self, pid: str, meta: dict, now: float) -> bool:
        prefix = self._prefix(pid)
        start = self.d.clock()
        while self.d.clock() - start < BUDGET_S:
            resp = self.d.s3.list_object_versions(Bucket=self.d.ingest_bucket, Prefix=prefix, MaxKeys=1000)
            batch = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                     for v in (resp.get("Versions") or []) + (resp.get("DeleteMarkers") or [])
                     if v["Key"].startswith(prefix)]
            if not batch:
                return True  # a final list returned neither versions nor delete markers
            out = self.d.s3.delete_objects(Bucket=self.d.ingest_bucket, Delete={"Objects": batch, "Quiet": True})
            if out.get("Errors"):
                e = out["Errors"][0]
                raise ClientError({"Error": {"Code": e.get("Code", "DeleteFailed"),
                                             "Message": f"{e.get('Key')}: {e.get('Message')}"}}, "DeleteObjects")
        return False

    def _records(self, pid: str, meta: dict, now: float) -> bool:
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid)),
                        "ProjectionExpression": "PK, SK", "ConsistentRead": True}
        with self.t.batch_writer() as bw:
            while True:
                resp = self.t.query(**kwargs)
                for it in resp.get("Items", []):
                    if it["SK"] != schema.META_SK:
                        bw.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
                if not resp.get("LastEvaluatedKey"):
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        tomb = schema.tombstone_item(pid, str(meta.get("deletedAt") or ""), _iso_now(self.d.clock))
        self.t.put_item(Item=tomb, ConditionExpression="#st = :deleted",
                        ExpressionAttributeNames={"#st": "status"}, ExpressionAttributeValues={":deleted": "deleted"})
        log.info("project %s purged", pid)
        return True


# ------------------------------------------------------------------------------------ trigger
def start_purge(sfn: Any, sm_arn: str, pid: str) -> bool:
    try:
        sfn.start_execution(stateMachineArn=sm_arn, name=execution_name(pid), input=json.dumps({"pid": pid}))
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            return False
        raise


def deleted_pids_from_stream(event: dict) -> list[str]:
    out: list[str] = []
    for rec in event.get("Records") or []:
        d = rec.get("dynamodb") or {}
        new, old = d.get("NewImage") or {}, d.get("OldImage") or {}
        if (rec.get("eventName") == "MODIFY" and (new.get("SK") or {}).get("S") == schema.META_SK
                and (new.get("status") or {}).get("S") == "deleted" and (old.get("status") or {}).get("S") == "active"):
            pk = (new.get("PK") or {}).get("S", "")
            pid = pk[len(schema.PROJECT_PK_PREFIX):] if pk.startswith(schema.PROJECT_PK_PREFIX) else ""
            if is_project_id(pid) and pid not in out:
                out.append(pid)
    return out


def sweep_pids(table: Any, now: float) -> list[str]:
    """Deleted, un-tombstoned, un-failed Projects deleted over 1 h ago (keys only; the table is small)."""
    from datetime import datetime

    out: list[str] = []
    kwargs: dict = {"FilterExpression": Attr("SK").eq(schema.META_SK) & Attr("status").eq("deleted")
                    & Attr("tombstone").not_exists(),
                    "ProjectionExpression": "PK, deletedAt, #pg", "ExpressionAttributeNames": {"#pg": "purge"}}
    while True:
        resp = table.scan(**kwargs)
        for it in resp.get("Items", []):
            if (it.get("purge") or {}).get("state") == "delete_failed":
                continue
            try:
                at = datetime.fromisoformat(str(it.get("deletedAt")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                at = 0
            pid = str(it["PK"])[len(schema.PROJECT_PK_PREFIX):]
            if now - at > SWEEP_MIN_AGE_S and is_project_id(pid):
                out.append(pid)
        if not resp.get("LastEvaluatedKey"):
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def trigger_handler(event: dict, _context: Any = None) -> dict:
    import boto3

    sfn = boto3.client("stepfunctions")
    arn = os.environ["PROJECT_PURGE_SFN_ARN"]
    if isinstance((event or {}).get("Records"), list):
        pids = deleted_pids_from_stream(event)
    else:
        pids = sweep_pids(boto3.resource("dynamodb").Table(os.environ["PROJECTS_TABLE"]), time.time())
    started = sum(start_purge(sfn, arn, p) for p in pids)
    log.info("purge trigger: %d candidate(s), %d started", len(pids), started)
    return {"candidates": len(pids), "started": started}


_TICK: Optional[PurgeTick] = None


def handler(event: dict, _context: Any = None) -> dict:
    global _TICK
    if _TICK is None:
        import boto3

        _TICK = PurgeTick(PurgeDeps(table=boto3.resource("dynamodb").Table(os.environ["PROJECTS_TABLE"]),
                                    s3=boto3.client("s3"), m2c_admin=M2CClient(M2CConfig.from_env(), timeout_s=600),
                                    ingest_bucket=os.environ["INGEST_BUCKET"]))
    out = _TICK.run(event)
    log.info("purge tick %s", out)
    return out
