"""RebuildTick Lambda: one step of a Collection_Rebuild (Req 10.6–10.12; design Component 14).

``CollectionRebuild`` is the same loop as ``AssetIngestion``: ``Tick → Choice(done?) → Wait(wait_s) → Tick``.
Input ``{pid, seq}``. State lives on META ``rebuild {seq, phase, pending, failures, notBefore, failure?}``:

(b) ``waiting``      every in-flight indexing job of the Project is terminal, or 1 h past submission;
(c) ``deleting``     ``delete_collection(pid)``, not-found = success (600 s call timeout);
(d) ``creating``     ``create_collection(pid, 4 scopes)``; ``incarnation + 1``; ``phase = resubmitting`` — this
                     releases the submissions IngestTick was holding;
(e) ``resubmitting`` every ``indexed`` asset's stored Document_Record is resubmitted to the new incarnation with
                     ``refresh: false`` through the same :class:`~ingestion.indexer.IndexRun`, up to
                     ``rebuildConcurrency`` assets at a time. Progress is ``rebuildRun`` on each ASSET row;
                     Ingestion_Status is never touched. An asset no longer ``indexed`` is skipped;
done                 ``ready``, and the coalesced follow-up starts if one is pending.

A step failing 3 times ≥ 30 s apart sets ``rebuild_failed`` with ``rebuild.failure = {step, reason}``.
A rebuild whose Project was deleted, or that a newer rebuild superseded, just stops.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from boto3.dynamodb.conditions import Key

from projects import schema

from .. import chunking, collection, records, reasons, rebuild
from ..indexer import Done, Failed, Poll, Submit, chunk_arguments, STATUS_TOOL, SUBMIT_TOOL
from ..m2c import M2CClient, M2CConfig, M2CError
from ..pipeline_store import Conflict, PipelineStore, from_ddb, to_ddb
from ..store import _iso_now
from .tick import _index_run_from, _index_run_to, IndexRun

log = logging.getLogger()
log.setLevel(logging.INFO)

WAIT_LIMIT_S = 3600
STEP_FAILURES_TO_FAIL = 3
RETRY_GAP_S = 30


@dataclass
class RebuildDeps:
    store: PipelineStore
    m2c: Any                       # normal calls (60 s)
    m2c_admin: Any                 # delete_collection (600 s)
    s3: Any
    ingest_bucket: str
    sfn: Any = None
    sm_arn: str = ""
    concurrency: int = 8
    clock: Callable[[], float] = time.time


def _out(event: dict, done: bool, wait_s: int = 15) -> dict:
    return {"pid": event["pid"], "seq": int(event["seq"]), "done": done, "wait_s": int(wait_s)}


class RebuildTick:
    def __init__(self, deps: RebuildDeps):
        self.d = deps
        self.t = deps.store.table

    # ------------------------------------------------------------------------------- META
    def _set(self, pid: str, seq: int, sets: dict, *, state: Optional[str] = None) -> bool:
        """SET ``rebuild.<k>`` (and optionally collectionState), conditioned on this rebuild still being current."""
        names = {"#rb": "rebuild", "#cs": "collectionState"}
        values: dict = {":seq": seq, ":rbs": "rebuilding"}
        parts = []
        for i, (k, v) in enumerate(sets.items()):
            names[f"#k{i}"] = k
            values[f":v{i}"] = to_ddb(v)
            parts.append(f"#rb.#k{i} = :v{i}")
        if state:
            values[":st"] = state
            parts.append("#cs = :st")
        try:
            self.t.update_item(Key=schema.meta_key(pid), UpdateExpression="SET " + ", ".join(parts),
                               ConditionExpression="#rb.seq = :seq AND #cs = :rbs",
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)
            return True
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def _step_failed(self, event: dict, pid: str, seq: int, rb: dict, step: str, err: Exception, now: float) -> dict:
        n = int(rb.get("failures") or 0) + 1
        if n >= STEP_FAILURES_TO_FAIL:
            reason = reasons.compose(step, str(err))
            self._set(pid, seq, {"failure": {"step": step, "reason": reason, "at": _iso_now(self.d.clock)},
                                 "failures": n}, state="rebuild_failed")
            log.warning("rebuild %s of %s failed: %s", seq, pid, reason)
            return _out(event, True)
        self._set(pid, seq, {"failures": n, "notBefore": now + RETRY_GAP_S, "lastError": str(err)[:500]})
        return _out(event, False, RETRY_GAP_S)

    def _advance(self, pid: str, seq: int, phase: str, extra: Optional[dict] = None) -> None:
        self._set(pid, seq, {"phase": phase, "failures": 0, "notBefore": 0, **(extra or {})})

    # ------------------------------------------------------------------------------- entry
    def run(self, event: dict) -> dict:
        pid, seq = str(event["pid"]), int(event["seq"])
        now = self.d.clock()
        meta = from_ddb(self.d.store.get_meta(pid) or {})
        rb = meta.get("rebuild") or {}
        if meta.get("status") != "active" or meta.get("collectionState") != "rebuilding" \
                or int(rb.get("seq") or -1) != seq:
            return _out(event, True)
        phase = rb.get("phase") or "waiting"
        if event.get("abort"):  # the state machine's Catch after its retries: record it, never leave it stuck
            step = {"waiting": "rebuild wait", "deleting": "rebuild delete", "creating": "rebuild create"}.get(
                phase, "rebuild resubmit")
            self._set(pid, seq, {"failure": {"step": step, "at": _iso_now(self.d.clock), "reason": reasons.compose(
                step, f"internal error: {str(event.get('error') or '')[:300]}")}}, state="rebuild_failed")
            return _out(event, True)
        if now < float(rb.get("notBefore") or 0):
            return _out(event, False, max(15, int(float(rb["notBefore"]) - now) + 1))
        if phase == "waiting":
            return self._waiting(event, pid, seq, rb, now)
        if phase == "deleting":
            try:
                collection.delete(self.d.m2c_admin, pid)
            except M2CError as exc:
                return self._step_failed(event, pid, seq, rb, "rebuild delete", exc, now)
            self._advance(pid, seq, "creating")
            return _out(event, False, 15)
        if phase == "creating":
            try:
                collection.ensure(self.d.m2c, pid, str(meta.get("name") or pid))
            except M2CError as exc:
                return self._step_failed(event, pid, seq, rb, "rebuild create", exc, now)
            inc = int(meta.get("incarnation") or 0) + 1
            self.t.update_item(Key=schema.meta_key(pid), UpdateExpression="SET incarnation = :inc, #rb.phase = :ph, "
                               "#rb.failures = :z", ConditionExpression="#rb.seq = :seq AND #rb.phase = :creating",
                               ExpressionAttributeNames={"#rb": "rebuild"},
                               ExpressionAttributeValues={":inc": inc, ":ph": "resubmitting", ":z": 0,
                                                          ":seq": seq, ":creating": "creating"})
            return _out(event, False, 15)
        if phase == "resubmitting":
            return self._resubmitting(event, pid, seq, rb, meta, now)
        return _out(event, True)

    # ------------------------------------------------------------------------------ (b) wait
    def _waiting(self, event: dict, pid: str, seq: int, rb: dict, now: float) -> dict:
        busy = 0
        for row in self.d.store.assets_in(pid, ("indexing",)):
            run = (from_ddb(row.get("index") or {}).get("run") or {})
            job, sub = run.get("job_id"), run.get("submittedAt")
            if not job or sub is None or now - float(sub) > WAIT_LIMIT_S:
                continue
            try:
                st = self.d.m2c.call_tool(STATUS_TOOL, {"job_id": job}).get("status")
            except M2CError:
                st = None
            if st not in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
                busy += 1
        if busy:
            return _out(event, False, 30)
        self._advance(pid, seq, "deleting")
        return _out(event, False, 15)

    # -------------------------------------------------------------------------- (e) resubmit
    def _indexed(self, pid: str) -> list[dict]:
        out: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX), "ConsistentRead": True}
        while True:
            resp = self.t.query(**kwargs)
            out += [from_ddb(it) for it in resp.get("Items", []) if it.get("status") == "indexed"]
            if not resp.get("LastEvaluatedKey"):
                return sorted(out, key=lambda r: str(r["assetId"]))
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def _resubmitting(self, event: dict, pid: str, seq: int, rb: dict, meta: dict, now: float) -> dict:
        inc = str(int(meta.get("incarnation") or 1))
        pending = [r for r in self._indexed(pid) if not ((r.get("rebuildRun") or {}).get("seq") == seq
                                                         and (r.get("rebuildRun") or {}).get("done"))]
        if not pending:
            return self._finish(event, pid, seq, now)
        submitted = False
        for row in pending[: max(1, self.d.concurrency)]:
            try:
                res = self._asset_step(pid, seq, inc, row, now)
            except Conflict:
                continue  # the asset moved (removed, retried): the next pass re-reads it
            if isinstance(res, Failed):
                return self._step_failed(event, pid, seq, rb, "rebuild resubmit",
                                         M2CError(f"{row.get('displayName')}: {res.message}"), now)
            submitted = submitted or res == "submitted"
        return _out(event, False, 15 if submitted else 20)

    def _asset_step(self, pid: str, seq: int, inc: str, row: dict, now: float) -> Any:
        doc = row.get("doc") or {}
        body = self.d.s3.get_object(Bucket=self.d.ingest_bucket, Key=doc["recordKey"])["Body"].read()
        docs = records.parse(body.decode("ascii"))
        rr = row.get("rebuildRun") or {}
        if rr.get("seq") != seq:
            rr = {"seq": seq, **_index_run_to(IndexRun(n_chunks=chunking.chunk_count(len(docs))))}
        run = _index_run_from(rr)
        parts = chunking.chunks(docs, False)  # a fresh incarnation: no row to replace (Req 6.4)
        act = run.next(now)
        result: Any = None
        try:
            if isinstance(act, Submit):
                res = self.d.m2c.call_tool(SUBMIT_TOOL, chunk_arguments(pid, parts[act.chunk],
                                                                       str(row.get("displayName") or "")))
                if not res.get("job_id"):
                    raise M2CError(f"index_content returned no job id: {json.dumps(res)[:300]}")
                run = run.submitted(res["job_id"])
                result = "submitted"
            elif isinstance(act, Poll):
                run = run.status(self.d.m2c.call_tool(STATUS_TOOL, {"job_id": act.job_id}), now)
        except M2CError as exc:
            run = run.call_failed(exc, now)
        outcome = run.next(now)
        if isinstance(outcome, Failed):
            return outcome
        attrs: dict = {"rebuildRun": {**_index_run_to(run), "seq": seq, "done": isinstance(outcome, Done)}}
        if isinstance(outcome, Done):
            index = dict(row.get("index") or {})
            by_inc = dict(index.get("byIncarnation") or {})
            scopes = sorted({d["doc_type"] for d in docs})
            by_inc[inc] = {"accepted": True, "scopesAccepted": scopes,
                           "lastCompleted": {"sha256": doc["sha256"], "scopes": scopes}}
            keep = sorted(by_inc, key=int)[-2:]
            attrs["index"] = {**index, "acceptedIncarnation": int(inc), "byIncarnation": {k: by_inc[k] for k in keep}}
        self.d.store.patch(pid, row, attrs)
        return result

    # ------------------------------------------------------------------------------- done
    def _finish(self, event: dict, pid: str, seq: int, now: float) -> dict:
        meta = from_ddb(self.d.store.get_meta(pid) or {})
        pending = bool((meta.get("rebuild") or {}).get("pending"))
        if not self._set(pid, seq, {"phase": "done", "pending": False, "finishedAt": _iso_now(self.d.clock)},
                         state="ready"):
            return _out(event, True)
        log.info("rebuild %s of %s done", seq, pid)
        if pending and self.d.sfn is not None:  # the coalesced follow-up starts after `ready` (Req 10.9)
            rebuild.request_rebuild(self.t, self.d.sfn, self.d.sm_arn, pid, reason="coalesced follow-up",
                                    now_iso=_iso_now(self.d.clock))
        return _out(event, True)


# ------------------------------------------------------------------------------------ Lambda wiring
_TICK: Optional[RebuildTick] = None


def from_env() -> RebuildTick:
    import boto3

    cfg = M2CConfig.from_env()
    raw = json.loads(os.environ.get("INGESTION_CONFIG") or "{}")
    return RebuildTick(RebuildDeps(
        store=PipelineStore.open(os.environ["PROJECTS_TABLE"]), m2c=M2CClient(cfg),
        m2c_admin=M2CClient(cfg, timeout_s=600), s3=boto3.client("s3"), ingest_bucket=os.environ["INGEST_BUCKET"],
        sfn=boto3.client("stepfunctions"), sm_arn=os.environ.get("COLLECTION_REBUILD_SFN_ARN", ""),
        concurrency=int(raw.get("rebuildConcurrency") or 8)))


def handler(event: dict, _context: Any = None) -> dict:
    global _TICK
    _TICK = _TICK or from_env()
    out = _TICK.run(event)
    log.info("rebuild tick %s", out)
    return out
