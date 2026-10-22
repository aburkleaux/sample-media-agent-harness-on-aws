"""State Store — DynamoDB wrapper for plan and node state.

Provides put_plan, put_node_status, get_plan_state, and update_node_status
with conditional writes and 24-hour TTL on all records.

Keys (table ``(session_id, node_id)``): a plan's row is ``PLAN#<plan_id>`` and its node rows
``<plan_id>#<node_id>``; each row also carries ``plan_id`` and the bare ``node`` id. Plans
written before that keep the legacy bare keys (``PLAN``, ``<node_id>``) until they finish, so
every writer here addresses a row in the format its plan already uses.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from planning.dag_schema import Plan

# 24 hours in seconds
TTL_SECONDS = 24 * 60 * 60


def plan_row_of(sort_key) -> str:
    """"PLAN" when this row is a plan's own row, else the node id.

    The sort key was the literal "PLAN" for a plan and a bare node id for a node, which made
    "PLAN" a FIXED key -- so a second plan in a session overwrote the first's row, nodes_json
    included. Qualified keys ("PLAN#<plan_id>", "<plan_id>#<node_id>") fix that, and both
    formats are read so a deploy does not strand plans already in flight.
    """
    text = str(sort_key or "")
    if text == "PLAN" or text.startswith("PLAN#"):
        return "PLAN"
    return text.split("#", 1)[1] if "#" in text else text


def plan_sk(plan_id: str) -> str:
    """Sort key of a plan's own row: ``PLAN#<plan_id>`` (legacy ``PLAN`` when no id)."""
    return f"PLAN#{plan_id}" if plan_id else "PLAN"


def node_sk(plan_id: str, node_id: str) -> str:
    """Sort key of a node row: ``<plan_id>#<node_id>`` (legacy bare id when no plan id)."""
    return f"{plan_id}#{node_id}" if plan_id else node_id


class StateStore:
    """DynamoDB wrapper for plan and node execution state."""

    def __init__(self, table_name: str, region: str):
        self._resource = boto3.resource("dynamodb", region_name=region)
        self._table = self._resource.Table(table_name)

    def put_plan(self, session_id: str, plan: Plan) -> None:
        """Write plan record with SK='PLAN'."""
        now = datetime.now(timezone.utc).isoformat()
        ttl = int(time.time()) + TTL_SECONDS

        self._table.put_item(Item={
            "session_id": session_id,
            "node_id": plan_sk(plan.plan_id),
            "plan_id": plan.plan_id,
            "node": "PLAN",
            "goal": plan.goal,
            "nodes_json": json.dumps([n.to_dict() for n in plan.nodes]),
            "status": "pending",
            "updated_at": now,
            "ttl": ttl,
        })

    def _existing_node_row(self, session_id: str, node_id: str, plan_id: str) -> tuple:
        """``(sort_key, existing_item)`` for a node row, in whichever format the plan uses.

        A plan's rows all use ONE format, chosen when its PLAN row was written: qualified
        (``<plan_id>#<node_id>``) for new plans, bare ``<node_id>`` for plans already in flight
        when the qualified keys shipped. The qualified row is tried first; a bare row is used
        only when it exists and is not stamped with another plan, so a legacy plan keeps writing
        where its chainer reads. Neither present → the qualified key (a new row).
        """
        if not plan_id:
            return node_id, self._get(session_id, node_id)
        qualified = node_sk(plan_id, node_id)
        found = self._get(session_id, qualified)
        if found:
            return qualified, found
        legacy = self._get(session_id, node_id)
        if legacy and legacy.get("plan_id", plan_id) == plan_id:
            return node_id, legacy
        return qualified, {}

    def _get(self, session_id: str, sort_key: str) -> dict:
        try:
            return self._table.get_item(
                Key={"session_id": session_id, "node_id": sort_key}
            ).get("Item", {}) or {}
        except Exception:  # noqa: BLE001 - a lookup must never block the status write
            return {}

    def put_node_status(self, session_id: str, node_id: str, status: str,
                        plan_id: str = "", unless_status: tuple = (), **attrs) -> bool:
        """Write/update node status record. Returns False when ``unless_status`` blocked the write.

        plan_id qualifies the sort key. Node ids are positional (t1, t2, t3) so they repeat
        across plans in a session and used to collide on (session_id, node_id) -- one plan's
        row overwriting another's, and a stale terminal row satisfying the wrong plan's
        completion check. The row is written in the plan's own key format
        (``_existing_node_row``), so a legacy plan in flight keeps its bare keys.

        ``unless_status``: skip the write when the row's CURRENT status is one of these. A plan
        node's own end-of-run write uses it so it can never regress a row that moved on while the
        node was still running (its job finished, the bridge marked it ``results_ready``, the
        chainer fetched and completed it). Unguarded, that write put the row back to
        ``processing`` after the job's one terminal event had passed, and the plan hung forever
        (session c3447913, plan-meridian-reel-recovery).
        """
        now = datetime.now(timezone.utc).isoformat()
        ttl = int(time.time()) + TTL_SECONDS
        sort_key, existing = self._existing_node_row(session_id, node_id, plan_id)

        item: dict = {
            "session_id": session_id,
            "node_id": sort_key,
            "status": status,
            "updated_at": now,
            "ttl": ttl,
        }
        if plan_id:
            item["plan_id"] = plan_id
            item["node"] = node_id
        # Merge any extra attributes (depends_on, result_s3_key, output_type, error, retries, etc.)
        for key, value in attrs.items():
            item[key] = value

        # Preserve the retry-accounting fields the Plan Chainer writes on this SAME row
        # (`_record_retry` SETs `retries` + `last_retry_error`). This is a full-item put, so
        # without carrying them forward a routine status rewrite here (e.g. running ->
        # processing/results_ready when the node re-runs on retry) WIPED them — resetting the
        # count so MAX_NODE_RETRIES never accumulated and the error-signature stop had nothing
        # to compare against. That is the 45x "retry 1/1" loop on session 6c69afb4 (sg1_video):
        # every re-dispatch reset the counter. Only carried when the caller didn't set them.
        for _f in ("retries", "last_retry_error"):
            if _f not in item and _f in existing:
                item[_f] = existing[_f]

        if unless_status:
            names = {"#st": "status"}
            values = {f":u{i}": s for i, s in enumerate(unless_status)}
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression=(
                        "attribute_not_exists(#st) OR NOT (#st IN (" + ", ".join(values) + "))"
                    ),
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
            except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
                return False
            return True
        self._table.put_item(Item=item)
        return True

    def get_plan_state(self, session_id: str, plan_id: str = "") -> dict:
        """Get one plan's state: its PLAN record and its node records.

        Returns a dict with 'plan' (the PLAN record) and 'nodes' (list of node records).
        ``plan_id`` selects the plan: ``PLAN#<plan_id>`` plus its ``<plan_id>#*`` node rows.
        Without it, or when that plan has no qualified row (a legacy plan in flight), the
        session is read whole: the newest ``PLAN#*`` row by ``updated_at`` (no ``plan_id``
        given), else the legacy ``PLAN`` row with the bare node rows.
        """
        if plan_id:
            plan_record = self._get(session_id, plan_sk(plan_id))
            if plan_record:
                return {"plan": plan_record,
                        "nodes": self._query(session_id, begins_with=f"{plan_id}#")}
        items = self._query(session_id)
        plans = [i for i in items if plan_row_of(i.get("node_id")) == "PLAN"]
        qualified = sorted((p for p in plans if str(p.get("node_id")).startswith("PLAN#")),
                           key=lambda p: str(p.get("updated_at", "")), reverse=True)
        if qualified and not plan_id:
            prefix = str(qualified[0]["node_id"])[len("PLAN#"):] + "#"
            return {"plan": qualified[0],
                    "nodes": [i for i in items if str(i.get("node_id")).startswith(prefix)]}
        legacy = next((p for p in plans if p.get("node_id") == "PLAN"), None)
        nodes = [i for i in items
                 if plan_row_of(i.get("node_id")) != "PLAN" and "#" not in str(i.get("node_id"))]
        return {"plan": legacy, "nodes": nodes}

    def _query(self, session_id: str, begins_with: str = "") -> list:
        cond = Key("session_id").eq(session_id)
        if begins_with:
            cond = cond & Key("node_id").begins_with(begins_with)
        items: list = []
        kwargs: dict = {"KeyConditionExpression": cond}
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def update_node_status(
        self,
        session_id: str,
        node_id: str,
        new_status: str,
        expected_status: str,
        plan_id: str = "",
        **attrs,
    ) -> bool:
        """Conditional status update. Returns False if condition fails.

        Uses ConditionExpression to ensure the current status matches
        expected_status before applying the update, preventing concurrent overwrites.
        ``plan_id`` addresses the row in its plan's key format (see ``put_node_status``).
        """
        now = datetime.now(timezone.utc).isoformat()
        sort_key = self._existing_node_row(session_id, node_id, plan_id)[0] if plan_id else node_id

        update_expr = "SET #st = :new_status, updated_at = :now"
        expr_names = {"#st": "status"}
        expr_values: dict = {
            ":new_status": new_status,
            ":now": now,
            ":expected": expected_status,
        }

        for key, value in attrs.items():
            safe_key = f"#{key}"
            update_expr += f", {safe_key} = :{key}"
            expr_names[safe_key] = key
            expr_values[f":{key}"] = value

        try:
            self._table.update_item(
                Key={"session_id": session_id, "node_id": sort_key},
                UpdateExpression=update_expr,
                ConditionExpression="#st = :expected",
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
            return True
        except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
            return False
