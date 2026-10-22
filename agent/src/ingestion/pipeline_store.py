"""Pipeline writes to Project_Asset rows (design Components 7 and 8).

Every status change is one ``TransactWriteItems`` that keeps META's ``statusCounts`` and ``processingCount``
exact (Property 6), conditioned on the ``status`` and ``rowVersion`` the row was read with: a lost condition means
another writer moved the row (a removal, a Project deletion, a racing tick), so the caller re-reads instead of
overwriting. Non-status progress (step records, index state) is a single conditional ``UpdateItem``.

A slot is held by an asset in ``analyzing`` or ``indexing`` (``slotHeld`` on the row, ``processingCount`` on
META). The claim takes one; ``indexed`` and ``failed`` release it; ``remove`` releases a held one (store.py).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from projects import schema

from .store import AssetStore, _as_int, _is_txn_canceled, _iso_now

MAX_SLOTS = 5
SCHED_PK = "SCHED"
GSI = "assetsByRegistered"


class Conflict(Exception):
    """The row (or META) moved under us; re-read and decide again."""


def to_ddb(v: Any) -> Any:
    """Floats → Decimal (boto3's resource layer rejects float); recursive over dicts, lists, tuples."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_ddb(x) for x in v]
    return v


def from_ddb(v: Any) -> Any:
    """Decimal → int or float, recursively, so pure code sees plain numbers."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: from_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [from_ddb(x) for x in v]
    if isinstance(v, set):
        return {from_ddb(x) for x in v}
    return v


class PipelineStore(AssetStore):
    # ------------------------------------------------------------------ reads
    def queue(self, pid: str) -> list[tuple[int, str]]:
        """``(queuedAtMs, assetId)`` in start order: queued time, then ascending asset id (Req 4.5)."""
        out: list[tuple[int, str]] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.QUEUE_SK_PREFIX), "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            for it in resp.get("Items", []):
                parts = str(it.get("SK", "")).split("#", 2)
                if len(parts) == 3 and parts[1].isdigit():
                    out.append((int(parts[1]), parts[2]))
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def scheduled_projects(self) -> list[str]:
        """Projects whose META carries the sparse ``SCHED`` pair (queued or processing work)."""
        out: list[str] = []
        kwargs: dict = {"IndexName": GSI, "KeyConditionExpression": Key("assetListPk").eq(SCHED_PK)}
        while True:
            resp = self.table.query(**kwargs)
            out += [str(it["assetListSk"]) for it in resp.get("Items", []) if it.get("assetListSk")]
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def assets_in(self, pid: str, statuses: tuple[str, ...]) -> list[dict]:
        out: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX),
                        "FilterExpression": Attr("status").is_in(list(statuses)), "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            out += resp.get("Items", [])
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # ------------------------------------------------------------- dispatch
    def claim(self, pid: str, row: Mapping[str, Any]) -> dict:
        """queued → analyzing, take a slot, bump ``run.seq`` (design §7 step 2). Raises :class:`Conflict`.

        The META condition (``status = active AND processingCount < 5``) is what keeps a deleted Project from
        starting anything and the slot count exact under concurrent dispatchers.
        """
        asset_id = str(row["assetId"])
        queued_at = _as_int(row.get("queuedAt"))
        rv = row.get("rowVersion")
        prev = from_ddb(row.get("run") or {})
        seq = int(prev.get("seq") or 0) + 1
        now_iso = _iso_now(self._clock)
        run = {"seq": seq, "startedAt": now_iso, "startedAtMs": int(self._clock() * 1000)}
        ops: list[dict] = []
        if queued_at is not None:
            ops.append({"Delete": {"TableName": self.name, "Key": schema.queue_key(pid, queued_at, asset_id),
                                   "ConditionExpression": "attribute_exists(PK)"}})
        ops.append({"Update": {
            "TableName": self.name, "Key": schema.asset_key(pid, asset_id),
            "UpdateExpression": ("SET #st = :an, statusChangedAt = :now, #run = :run, slotHeld = :t, "
                                 "rowVersion = rowVersion + :one REMOVE queuedAt, failure"),
            "ConditionExpression": "#st = :q AND rowVersion = :rv",
            "ExpressionAttributeNames": {"#st": "status", "#run": "run"},
            "ExpressionAttributeValues": {":an": "analyzing", ":q": "queued", ":now": now_iso, ":run": run,
                                          ":t": True, ":one": 1, ":rv": rv},
        }})
        ops.append({"Update": {
            "TableName": self.name, "Key": schema.meta_key(pid),
            "UpdateExpression": ("SET statusCounts.queued = statusCounts.queued - :one, "
                                 "statusCounts.analyzing = statusCounts.analyzing + :one, "
                                 "processingCount = processingCount + :one, changeSeq = changeSeq + :one"),
            "ConditionExpression": "#st = :active AND processingCount < :max",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":one": 1, ":active": "active", ":max": MAX_SLOTS},
        }})
        try:
            self.client.transact_write_items(TransactItems=ops)
        except ClientError as exc:
            if _is_txn_canceled(exc):
                raise Conflict("claim lost") from exc
            raise
        after = self.get_asset(pid, asset_id)
        if after is None:
            raise Conflict("claimed row vanished")
        return after

    def record_execution(self, pid: str, asset_id: str, seq: int, arn: str) -> None:
        try:
            self.table.update_item(
                Key=schema.asset_key(pid, asset_id), UpdateExpression="SET #run.executionArn = :arn",
                ConditionExpression="#run.seq = :seq", ExpressionAttributeNames={"#run": "run"},
                ExpressionAttributeValues={":arn": arn, ":seq": seq})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def mark_scheduled(self, pid: str) -> None:
        self.table.update_item(Key=schema.meta_key(pid), UpdateExpression="SET assetListPk = :s, assetListSk = :p",
                               ConditionExpression="attribute_exists(SK)",
                               ExpressionAttributeValues={":s": SCHED_PK, ":p": pid})

    def clear_scheduled_if_idle(self, pid: str) -> bool:
        """Drop the ``SCHED`` pair when nothing is queued or processing. Conditional, so it never races a claim."""
        try:
            self.table.update_item(
                Key=schema.meta_key(pid), UpdateExpression="REMOVE assetListPk, assetListSk",
                ConditionExpression=("assetListPk = :s AND (attribute_not_exists(statusCounts) OR "
                                     "(statusCounts.queued = :z AND processingCount = :z))"),
                ExpressionAttributeValues={":s": SCHED_PK, ":z": 0})
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    # --------------------------------------------------------------- progress
    def patch(self, pid: str, row: Mapping[str, Any], attrs: Mapping[str, Any]) -> dict:
        """SET top-level ``attrs`` and bump ``rowVersion``, conditioned on the row's status and version."""
        names = {"#st": "status"}
        values: dict[str, Any] = {":rv": row.get("rowVersion"), ":st": row.get("status"), ":one": 1}
        sets = ["rowVersion = rowVersion + :one"]
        for i, (k, v) in enumerate(attrs.items()):
            names[f"#a{i}"] = k
            values[f":a{i}"] = to_ddb(v)
            sets.append(f"#a{i} = :a{i}")
        try:
            resp = self.table.update_item(
                Key=schema.asset_key(pid, str(row["assetId"])), UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="rowVersion = :rv AND #st = :st", ExpressionAttributeNames=names,
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise Conflict("row moved") from exc
            raise
        return resp.get("Attributes") or {}

    def transition(self, pid: str, row: Mapping[str, Any], to: str, attrs: Mapping[str, Any] = {},
                   *, failure: Optional[dict] = None) -> None:
        """Move the asset to ``to`` with counters exact. ``indexed``/``failed``/``stored`` release the slot (``stored``
        from a run: an audio asset with no speech, kept but not indexed)."""
        asset_id = str(row["assetId"])
        frm = str(row.get("status"))
        release = to in ("indexed", "failed", "stored") and row.get("slotHeld") is True
        now_iso = _iso_now(self._clock)
        now_ms = int(self._clock() * 1000)
        names = {"#st": "status"}
        values: dict[str, Any] = {":to": to, ":from": frm, ":rv": row.get("rowVersion"), ":now": now_iso, ":one": 1}
        sets = ["#st = :to", "statusChangedAt = :now", "rowVersion = rowVersion + :one"]
        for i, (k, v) in enumerate(attrs.items()):
            names[f"#a{i}"] = k
            values[f":a{i}"] = to_ddb(v)
            sets.append(f"#a{i} = :a{i}")
        if failure is not None:
            values.update({":fail": to_ddb({**failure, "at": now_iso}), ":fat": now_ms})
            sets += ["failure = :fail", "failedAt = :fat"]
        expr = "SET " + ", ".join(sets) + (" REMOVE slotHeld" if release else "")
        ops: list[dict] = [{"Update": {
            "TableName": self.name, "Key": schema.asset_key(pid, asset_id), "UpdateExpression": expr,
            "ConditionExpression": "#st = :from AND rowVersion = :rv", "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values}}]
        meta_set = ["statusCounts.#f = statusCounts.#f - :one", "statusCounts.#t = if_not_exists(statusCounts.#t, :z) + :one",
                    "changeSeq = changeSeq + :one"]
        if release:
            meta_set.append("processingCount = processingCount - :one")
        ops.append({"Update": {
            "TableName": self.name, "Key": schema.meta_key(pid), "UpdateExpression": "SET " + ", ".join(meta_set),
            "ConditionExpression": "attribute_exists(statusCounts)",
            "ExpressionAttributeNames": {"#f": frm, "#t": to},
            "ExpressionAttributeValues": {":one": 1, ":z": 0}}})
        if to == "failed":
            ops.append({"Put": {"TableName": self.name,
                                "Item": {**schema.failidx_key(pid, now_ms, asset_id), "assetId": asset_id}}})
        try:
            self.client.transact_write_items(TransactItems=ops)
        except ClientError as exc:
            if _is_txn_canceled(exc):
                raise Conflict("transition lost") from exc
            raise

    def set_collection_ready(self, pid: str) -> None:
        """``absent`` → ``ready``, incarnation 1 (Req 4.1). A lost condition means someone else did it."""
        try:
            self.table.update_item(
                Key=schema.meta_key(pid),
                UpdateExpression="SET collectionState = :ready, incarnation = :one, changeSeq = changeSeq + :one",
                ConditionExpression="collectionState = :absent",
                ExpressionAttributeValues={":ready": "ready", ":one": 1, ":absent": "absent"})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
