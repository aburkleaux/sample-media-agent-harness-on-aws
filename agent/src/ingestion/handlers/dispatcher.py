"""Dispatcher Lambda: start queued assets, at most 5 processing per Project (design Component 7, Req 4.5/4.6/4.19).

Triggers:

- the projects-table stream, filtered to ``INSERT`` of ``QUEUE#`` items (an asset entered ``queued``) — and of
  ``ASSET#video:`` rows, which it hands to the HeroFrame Lambda (async invoke) for a sampled thumbnail. Riding this
  mapping keeps the projects stream at two readers instead of adding a third (DynamoDB Streams throttles above ~2);
- an EventBridge rule every minute that sweeps the Projects whose META carries ``assetListPk = SCHED``.

A slot freed by ``IngestTick`` (indexed/failed) is filled in-process by the tick calling
:func:`dispatch_project` directly, so the "within 60 seconds" of Req 4.5 does not depend on the sweep.

For a Project: claim queued assets in start order while ``processingCount < 5`` (one transaction each), then
``StartExecution`` named ``<kind>-<uuid>-r<runSeq>`` with input ``{pid, assetId, runSeq}``. An identical name and
input returns the existing execution, so a duplicate event or a retried Lambda starts no second run. The sweep
also re-issues ``StartExecution`` for an ``analyzing`` asset claimed over 2 minutes ago with no recorded
execution (a crash between the claim and the start).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Optional

from botocore.exceptions import ClientError

from projects.ids import is_project_id

from ..pipeline_store import MAX_SLOTS, Conflict, PipelineStore, from_ddb

log = logging.getLogger()
log.setLevel(logging.INFO)

STUCK_START_S = 120
# Kinds IngestTick has a pipeline for. Another kind stays ``queued`` (not failed) until its wave lands.
PIPELINE_KINDS = frozenset({"video", "audio", "image", "document"})


def execution_name(asset_id: str, seq: int) -> str:
    kind, _, uid = asset_id.partition(":")
    return f"{kind}-{uid}-r{int(seq)}"


def execution_input(pid: str, asset_id: str, seq: int) -> str:
    return json.dumps({"pid": pid, "assetId": asset_id, "runSeq": int(seq)}, sort_keys=True)


VIDEO_ASSET_SK = "ASSET#video:"


def new_videos(records: list) -> list[tuple[str, str]]:
    """``(pid, assetId)`` of each ``INSERT`` of a video ASSET row that has no ``rendition`` yet."""
    out: list[tuple[str, str]] = []
    for rec in records:
        img = ((rec.get("dynamodb") or {}).get("NewImage") or {})
        pk = (img.get("PK") or {}).get("S", "")
        sk = (img.get("SK") or {}).get("S", "")
        if (rec.get("eventName") == "INSERT" and pk.startswith("PROJECT#") and sk.startswith(VIDEO_ASSET_SK)
                and "rendition" not in img):
            out.append((pk[len("PROJECT#"):], sk[len("ASSET#"):]))
    return out


class Dispatcher:
    def __init__(self, store: PipelineStore, sfn: Any, state_machine_arn: str,
                 clock: Callable[[], float] = time.time, lambda_client: Any = None,
                 hero_frame_fn: str = ""):
        self.store, self.sfn, self.arn, self.clock = store, sfn, state_machine_arn, clock
        self.lambda_client, self.hero_frame_fn = lambda_client, hero_frame_fn

    def _request_hero_frames(self, records: list) -> int:
        """Async-invoke HeroFrame once per Project for its new videos. Best effort: never fails the batch."""
        if not (self.hero_frame_fn and self.lambda_client):
            return 0
        by_pid: dict[str, list[str]] = {}
        for pid, aid in new_videos(records):
            by_pid.setdefault(pid, []).append(aid)
        sent = 0
        for pid, ids in by_pid.items():
            try:
                self.lambda_client.invoke(FunctionName=self.hero_frame_fn, InvocationType="Event",
                                          Payload=json.dumps({"pid": pid, "assetIds": ids}).encode())
                sent += len(ids)
            except Exception:  # noqa: BLE001 — a thumbnail must never block dispatching
                log.warning("hero frame request failed for %s", pid, exc_info=True)
        return sent

    def _start(self, pid: str, asset_id: str, seq: int) -> Optional[str]:
        try:
            resp = self.sfn.start_execution(stateMachineArn=self.arn, name=execution_name(asset_id, seq),
                                            input=execution_input(pid, asset_id, seq))
            arn = resp["executionArn"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                raise
            # Same name, different input cannot happen (both derive from the claim); treat as started.
            arn = f"{self.arn.replace(':stateMachine:', ':execution:')}:{execution_name(asset_id, seq)}"
        self.store.record_execution(pid, asset_id, seq, arn)
        return arn

    def dispatch_project(self, pid: str, *, sweep: bool = False) -> int:
        """Claim and start what fits. Returns how many runs started."""
        if not is_project_id(pid):
            return 0
        meta = self.store.get_meta(pid)
        if not meta or meta.get("status") != "active":
            return 0
        started = 0
        processing = int(from_ddb(meta.get("processingCount") or 0))
        for _queued_at, asset_id in self.store.queue(pid):
            if processing >= MAX_SLOTS:
                break
            row = self.store.get_asset(pid, asset_id)
            if row is None or row.get("status") != "queued":
                continue  # a removal or an earlier claim already moved it; its QUEUE item is going away too
            if row.get("kind") not in PIPELINE_KINDS:
                continue
            try:
                claimed = self.store.claim(pid, row)
            except Conflict:
                meta = self.store.get_meta(pid) or {}
                if meta.get("status") != "active" or int(from_ddb(meta.get("processingCount") or 0)) >= MAX_SLOTS:
                    break  # Req 4.6 / projects-access 1.23: full or deleted ends the pass
                continue
            processing += 1
            seq = int(from_ddb(claimed.get("run") or {}).get("seq") or 0)
            self._start(pid, asset_id, seq)
            started += 1
        if sweep:
            now_ms = int(self.clock() * 1000)
            for row in self.store.assets_in(pid, ("analyzing",)):
                run = from_ddb(row.get("run") or {})
                if not run.get("executionArn") and now_ms - int(run.get("startedAtMs") or 0) > STUCK_START_S * 1000:
                    log.info("re-issuing StartExecution for %s run %s", row.get("assetId"), run.get("seq"))
                    self._start(pid, str(row["assetId"]), int(run.get("seq") or 0))
            self.store.clear_scheduled_if_idle(pid)
        return started

    def handle(self, event: dict) -> dict:
        if isinstance(event.get("Records"), list):
            pids: list[str] = []
            for rec in event["Records"]:
                img = ((rec.get("dynamodb") or {}).get("NewImage") or {})
                pk = (img.get("PK") or {}).get("S", "")
                sk = (img.get("SK") or {}).get("S", "")
                if rec.get("eventName") == "INSERT" and sk.startswith("QUEUE#") and pk.startswith("PROJECT#"):
                    pid = pk[len("PROJECT#"):]
                    if pid not in pids:
                        pids.append(pid)
            heroes = self._request_hero_frames(event["Records"])
            started = sum(self.dispatch_project(p) for p in pids)
            out = {"projects": len(pids), "started": started}
            return {**out, "heroFrames": heroes} if heroes else out
        pids = self.store.scheduled_projects()
        started = 0
        for pid in pids:
            try:
                started += self.dispatch_project(pid, sweep=True)
            except Exception:  # noqa: BLE001 — one Project's failure must not stop the sweep
                log.exception("sweep failed for %s", pid)
        return {"projects": len(pids), "started": started}


_DISPATCHER: Optional[Dispatcher] = None


def from_env() -> Dispatcher:
    import boto3
    hero = os.environ.get("HERO_FRAME_FN", "")
    return Dispatcher(PipelineStore.open(os.environ["PROJECTS_TABLE"]), boto3.client("stepfunctions"),
                      os.environ["ASSET_INGESTION_SFN_ARN"],
                      lambda_client=boto3.client("lambda") if hero else None, hero_frame_fn=hero)


def handler(event: dict, _context: Any = None) -> dict:
    global _DISPATCHER
    _DISPATCHER = _DISPATCHER or from_env()
    out = _DISPATCHER.handle(event or {})
    log.info("dispatch %s", out)
    return out
