"""Build a ``HookEngine`` + ``HarnessHookProvider`` for an agent runtime.

Combines harness-provided defaults with enabled user-defined hook assets loaded
from the AssetIndex. All asset loading is defensive: if the index is
unavailable or a row is malformed, we log and continue with whatever loaded
(the built-in defaults always apply).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .defaults import LOCKED_BUILTINS, default_hook_specs
from .engine import HookEngine, HookSpec
from .provider import HarnessHookProvider

logger = logging.getLogger(__name__)

__all__ = ["build_engine", "build_provider", "jobs_identity", "LOCKED_BUILTINS"]

# ``LOCKED_BUILTINS`` lives in ``.defaults`` (stdlib-only) so the Capability API's ListHooks can read
# it without importing this module, whose ``.provider`` import pulls in ``render``.


def build_engine(hook_assets: Optional[list[Any]] = None) -> HookEngine:
    """Build an engine from built-in defaults + enabled user hook assets.

    Name-override semantics let users customize or DISABLE a built-in without
    any separate override store:
      * A user hook whose ``name`` matches a built-in REPLACES that built-in.
        - If the user hook is enabled → its (possibly edited) spec is used.
        - If the user hook is disabled → the built-in is effectively turned
          OFF (default removed, user's disabled spec not added).
      * User hooks with unique names are added alongside the defaults.

    A user hook whose ``name`` is in :data:`LOCKED_BUILTINS` is dropped first, so a locked
    built-in can be neither replaced nor disabled.
    """
    user_specs: list[HookSpec] = []
    for asset in hook_assets or []:
        try:
            spec = HookSpec.from_hook_asset(asset)
        except Exception:
            logger.exception("hooks.loader: failed to convert a hook asset; skipping")
            continue
        if spec.name in LOCKED_BUILTINS:
            # A user may not override or disable a locked built-in. Drop the spec before it
            # can shadow the default or compute an override.
            logger.warning(
                "hooks.loader: ignoring user hook named %r; it is a locked built-in", spec.name
            )
            continue
        user_specs.append(spec)
    overridden = {s.name for s in user_specs}
    # Built-in defaults, minus any the user has overridden by name.
    specs: list[HookSpec] = [d for d in default_hook_specs() if d.name not in overridden]
    # Enabled user hooks (including enabled overrides of a built-in).
    specs.extend(s for s in user_specs if s.enabled)
    return HookEngine(specs)


def jobs_identity() -> dict:
    """Identity resolver: pull session/actor from jobs.context if bound."""
    try:
        from jobs.context import current_actor_id, current_session_id
        return {
            "session": {"id": current_session_id()},
            "actor": {"id": current_actor_id()},
        }
    except Exception:
        return {}


def build_provider(hook_assets: Optional[list[Any]] = None) -> HarnessHookProvider:
    """Convenience: build an engine + wrap it in a provider with jobs identity."""
    return HarnessHookProvider(build_engine(hook_assets), identity=jobs_identity)


async def load_actor_hook_specs(actor_id: Optional[str], session_id: str = "") -> list:
    """Best-effort load of an actor's enabled hook assets from the AssetIndex.

    Used by runtimes (e.g. the task agent) that don't already have a bound
    LabService. Returns ``[]`` on ANY failure — missing ``ASSET_INDEX_TABLE``
    env, no actor, import/DDB error — so hook loading never breaks a turn. The
    caller passes the result to ``build_provider`` (which always applies the
    built-in defaults regardless).
    """
    import os

    table_name = os.environ.get("ASSET_INDEX_TABLE", "")
    if not actor_id or not table_name:
        return []
    try:
        import uuid as _uuid

        import boto3
        from lab.asset_index import AssetIndex
        from lab.context import LabRequestContext
        from lab.hook_crud import HookCRUD

        region = os.environ.get("AWS_REGION", "us-east-1")
        table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        index = AssetIndex(ddb_table=table, actor_id=actor_id)
        crud = HookCRUD(index=index)
        ctx = LabRequestContext(
            actor_id=actor_id, session_id=session_id or "", request_id=str(_uuid.uuid4())
        )
        return await crud.list(ctx)
    except Exception:
        logger.debug("load_actor_hook_specs failed for actor %s", actor_id, exc_info=True)
        return []
