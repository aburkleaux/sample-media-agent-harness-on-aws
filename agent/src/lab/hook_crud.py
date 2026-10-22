"""CRUD for declarative Hook assets.

Hooks are metadata-only assets (no S3 content), so this CRUD writes directly to
the AssetIndex working-copy partition — no ``S3UserFilesTool`` needed. Layout
mirrors the other categories:

    PK = USER#<actor>#OWNED
    SK = hook#<assetId>
    s3Prefix = users/<actor>/drafts/hooks/<assetId>/   (validated, never written)

Conditions are validated with :mod:`hooks.expr` before any write so an invalid
expression is rejected at author time (fail-closed).

Built-in hooks are NOT stored here — they live in :mod:`hooks.defaults` and are
surfaced read-only by the service layer.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from botocore.exceptions import ClientError

from .asset_index import AssetIndex, make_pk_user_owned, make_sk_owned
from .context import LabRequestContext
from .models import Hook

logger = logging.getLogger(__name__)

_CATEGORY = "hook"
# User-selectable action vocabulary. ``moments_render`` is deliberately absent: it is the action
# of the locked built-in ``render-moments`` (hooks.defaults.LOCKED_BUILTINS), so no user hook may
# name it. Keep it out of this set.
_VALID_ACTIONS = {"render", "notify", "dispatch"}
_VALID_EVENTS = {
    "after_tool_result", "before_tool", "session_start", "session_end",
}


def _validate_condition(condition: str) -> None:
    from hooks.expr import ExpressionError, validate
    try:
        validate(condition or "")
    except ExpressionError as exc:
        raise ValueError(f"Invalid hook condition: {exc}") from exc


def _validate_action(action_type: str) -> None:
    if action_type not in _VALID_ACTIONS:
        raise ValueError(
            f"Invalid action_type '{action_type}'; expected one of {sorted(_VALID_ACTIONS)}"
        )


class HookCRUD:
    """CRUD operations for user-defined Hook assets (working copies)."""

    def __init__(self, index: AssetIndex) -> None:
        self._index = index

    async def create(
        self,
        ctx: LabRequestContext,
        *,
        name: str,
        event: str,
        condition: str,
        action_type: str,
        action_params: Optional[dict] = None,
        priority: int = 100,
        enabled: bool = True,
    ) -> dict:
        if not name:
            raise ValueError("Hook name is required")
        _validate_action(action_type)
        _validate_condition(condition)

        asset_id = uuid.uuid4().hex[:12]
        now = int(time.time() * 1000)
        hook = Hook(
            asset_id=asset_id,
            category="hook",
            name=name,
            status="draft",
            scope="private",
            s3_prefix=f"users/{ctx.actor_id}/drafts/hooks/{asset_id}/",
            created_at=now,
            updated_at=now,
            event=event,
            condition=condition or "",
            action_type=action_type,
            action_params=dict(action_params or {}),
            priority=int(priority),
            enabled=bool(enabled),
            builtin=False,
        )
        await self._index.put(hook, owner_user_id=ctx.actor_id)
        return {"assetId": asset_id, "s3Prefix": hook.s3_prefix}

    async def read(self, ctx: LabRequestContext, asset_id: str) -> Optional[Hook]:
        record = await self._index.get(
            make_pk_user_owned(ctx.actor_id), make_sk_owned(_CATEGORY, asset_id)
        )
        return record if isinstance(record, Hook) else None

    async def list(self, ctx: LabRequestContext) -> list[Hook]:
        items, _ = await self._index.query_pk(
            make_pk_user_owned(ctx.actor_id), sk_prefix=f"{_CATEGORY}#", limit=200
        )
        return [h for h in items if isinstance(h, Hook)]

    async def update(
        self,
        ctx: LabRequestContext,
        asset_id: str,
        *,
        name: Optional[str] = None,
        event: Optional[str] = None,
        condition: Optional[str] = None,
        action_type: Optional[str] = None,
        action_params: Optional[dict] = None,
        priority: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[bool]:
        if condition is not None:
            _validate_condition(condition)
        if action_type is not None:
            _validate_action(action_type)

        sets: list[str] = ["updatedAt = :updatedAt"]
        names: dict[str, str] = {}
        values: dict[str, Any] = {":updatedAt": int(time.time() * 1000)}

        def _set(attr: str, alias: str, val: Any) -> None:
            names[f"#{alias}"] = attr
            values[f":{alias}"] = val
            sets.append(f"#{alias} = :{alias}")

        if name is not None:
            _set("name", "nm", name)
        if event is not None:
            _set("event", "ev", event)
        if condition is not None:
            _set("condition", "cd", condition)
        if priority is not None:
            _set("priority", "pr", int(priority))
        if enabled is not None:
            _set("enabled", "en", bool(enabled))
        if action_type is not None or action_params is not None:
            existing = await self.read(ctx, asset_id)
            a_type = action_type if action_type is not None else (existing.action_type if existing else "notify")
            a_params = action_params if action_params is not None else (existing.action_params if existing else {})
            _set("action", "ac", {"type": a_type, "params": dict(a_params or {})})

        try:
            await self._index.update(
                make_pk_user_owned(ctx.actor_id),
                make_sk_owned(_CATEGORY, asset_id),
                update_expression="SET " + ", ".join(sets),
                expression_attribute_names=names or None,
                expression_attribute_values=values,
                condition_expression="attribute_exists(PK)",
            )
            return True
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in ("ConditionalCheckFailedException", "TransactionCanceledException"):
                return None
            raise

    async def delete(self, ctx: LabRequestContext, asset_id: str) -> Optional[bool]:
        ops = [
            {
                "Delete": {
                    "TableName": self._index.table_name,
                    "Key": {"PK": make_pk_user_owned(ctx.actor_id),
                            "SK": make_sk_owned(_CATEGORY, asset_id)},
                    "ConditionExpression": "attribute_exists(PK)",
                }
            }
        ]
        try:
            await self._index.transact_write(ops)
            return True
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in ("ConditionalCheckFailedException", "TransactionCanceledException"):
                return None
            raise
