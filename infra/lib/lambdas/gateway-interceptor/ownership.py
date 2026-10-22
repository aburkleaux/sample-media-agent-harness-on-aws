"""Ownership of user-registered gateway targets, read from the registry.

Single source of truth. The agent writes one row per target when a user
registers an MCP server; this module and the proxy's catalog reader both read
that row. Nothing derives ownership from a name, so there is no algorithm to
keep in sync across languages.

Why a lookup is affordable on the gateway hot path
--------------------------------------------------
The general objection to per-call reads stands for *entitlement*, which varies
per request. Ownership changes only when someone registers or removes a server,
so it caches well:

- **Steady state** -- Lambda execution contexts persist between invocations, so
  an in-process map with a short TTL costs roughly one read per minute, not one
  per call.
- **Unknown target** -- a miss triggers exactly one fresh read before any deny
  decision. That bounds staleness to a single read per newly-registered target,
  so a user's new tool works on its first call rather than after a TTL. Without
  this, registration would appear broken for up to the cache lifetime.
- **Negative results are cached too**, with a shorter TTL, so a probe against a
  nonexistent target cannot turn into a read per call.
- **Read failure denies.** An unavailable registry must not mean unrestricted
  access.

The cost sits against a call that already crosses the network to a third-party
MCP server, so a few milliseconds on a cache miss is not the dominant term.
"""

from __future__ import annotations

import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()

# Partition holding one row per user-registered target.
OWNER_PK = "TARGET#OWNER"

TABLE_ENV = "GATEWAY_REGISTRY_TABLE"

# Positive entries change only on registration/removal, so a minute is ample.
_TTL_SECONDS = float(os.environ.get("OWNERSHIP_CACHE_TTL", "60"))
# Negative entries are shorter-lived: a target that does not exist yet may be
# about to, and we would rather re-read than deny a freshly created target for a
# full minute.
_NEGATIVE_TTL_SECONDS = float(os.environ.get("OWNERSHIP_NEGATIVE_TTL", "10"))

# target name -> (owner actor id or "", expires_at)
_cache: dict[str, tuple[str, float]] = {}

_table = None


def _get_table():
    """Lazily bind the DynamoDB table so import never performs I/O."""
    global _table
    if _table is None:
        name = os.environ.get(TABLE_ENV, "")
        if not name:
            return None
        _table = boto3.resource("dynamodb").Table(name)
    return _table


def reset_cache() -> None:
    """Drop cached entries. For tests; not used at runtime."""
    _cache.clear()


def _now() -> float:
    return time.monotonic()


def owner_of_target(target: str) -> str:
    """Return the actor id that owns ``target``, or "" if unknown.

    "" covers three distinct cases that all lead callers to deny: no such
    target, no registry configured, and a failed read. They are deliberately
    indistinguishable to the caller so no path can accidentally treat one of
    them as permission.
    """
    if not target:
        return ""

    cached = _cache.get(target)
    if cached is not None and cached[1] > _now():
        return cached[0]

    table = _get_table()
    if table is None:
        logger.error(
            "ownership: %s is not set; cannot resolve target ownership", TABLE_ENV
        )
        return ""

    try:
        resp = table.get_item(Key={"PK": OWNER_PK, "SK": target})
    except ClientError as err:
        # Do NOT cache a failure as a negative result: that would turn a
        # transient registry outage into a minute of denials for a target we
        # would otherwise have allowed.
        logger.error("ownership: read failed for %s: %s", target, err)
        return ""

    item = resp.get("Item") or {}
    owner = item.get("ownerActorId") or ""
    owner = owner if isinstance(owner, str) else ""
    ttl = _TTL_SECONDS if owner else _NEGATIVE_TTL_SECONDS
    _cache[target] = (owner, _now() + ttl)
    return owner


def actor_owns_target(actor_id: str, target: str) -> bool:
    """Whether ``actor_id`` is the recorded owner of ``target``."""
    if not actor_id or not target:
        return False
    return owner_of_target(target) == actor_id
