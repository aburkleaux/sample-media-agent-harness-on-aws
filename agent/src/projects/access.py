"""Project membership check — Python twin of ``ui/server/projects/access.ts`` (``checkAccess``).

media-harness-projects-access task 6.14 (design Interfaces provided to sibling specs → project-ingestion;
Req 1.17, 1.24). Same consistent-read semantics as the Token_Issuer's ``access.check_access`` (task 3.2),
so the ingestion spec's ``/upload`` and registration routes, and anything in the runtime that must decide
whether an Actor may use a Project, share ONE implementation.

``table`` is a boto3 DynamoDB **resource** ``Table`` (``boto3.resource("dynamodb").Table(name)``), not a
low-level client. The read goes through ``table.meta.client.batch_get_item``, which on a resource-built
client takes and returns plain Python values (no ``{"S": ...}`` wrappers).

Semantics, identical to the TS function:

- A malformed ``project_id`` (not ``^p[0-9a-f]{32}$``) or an empty ``sub`` is not-found with no read.
- Otherwise exactly one ``BatchGetItem`` with ``ConsistentRead=True`` for ``PROJECT#<pid>/META`` and
  ``PROJECT#<pid>/MEMBER#<sub>``. ``UnprocessedKeys`` are re-requested once.
- ok only when META exists, is not an id tombstone, has ``status = active``, and the member item exists.
  Every denial is the same ``NOT_FOUND`` result (Req 1.17).

A read DynamoDB could not complete (keys still unprocessed after the one retry, or a client error) raises
``AccessCheckIncomplete`` / the client error instead of answering not-found: not-found ends the user's
access, so a throttle must surface as a failure, not as a denial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from projects.ids import is_project_id
from projects.schema import META_SK, is_tombstone, member_key, meta_key

ROLES = frozenset({"owner", "member"})


@dataclass(frozen=True)
class AccessResult:
    """``ok=True`` carries the META item and the member's role; every denial is ``NOT_FOUND``."""

    ok: bool
    project: Optional[Mapping[str, Any]] = field(default=None, compare=False)
    role: Optional[str] = None


NOT_FOUND = AccessResult(ok=False)


class AccessCheckIncomplete(Exception):
    """DynamoDB left keys unprocessed after the one retry; the check has no answer."""


def _batch_get(table: Any, keys: list[dict[str, str]]) -> list[Mapping[str, Any]]:
    client = table.meta.client
    name = table.name
    resp = client.batch_get_item(RequestItems={name: {"Keys": keys, "ConsistentRead": True}})
    items = list(resp.get("Responses", {}).get(name, []))
    unprocessed = resp.get("UnprocessedKeys") or {}
    if unprocessed.get(name, {}).get("Keys"):
        # UnprocessedKeys echoes the request entry, ConsistentRead included; re-send it as given.
        resp = client.batch_get_item(RequestItems=unprocessed)
        items.extend(resp.get("Responses", {}).get(name, []))
        if (resp.get("UnprocessedKeys") or {}).get(name, {}).get("Keys"):
            raise AccessCheckIncomplete("BatchGetItem left keys unprocessed after one retry")
    return items


def check_access(table: Any, sub: object, project_id: object) -> AccessResult:
    """Whether ``sub`` may use ``project_id``. One consistent BatchGet; never writes. See the module docstring."""
    if not is_project_id(project_id) or not isinstance(sub, str) or not sub:
        return NOT_FOUND
    meta_k = meta_key(project_id)
    member_k = member_key(project_id, sub)
    meta: Optional[Mapping[str, Any]] = None
    member: Optional[Mapping[str, Any]] = None
    for item in _batch_get(table, [meta_k, member_k]):
        if item.get("PK") != meta_k["PK"]:
            continue
        if item.get("SK") == META_SK:
            meta = item
        elif item.get("SK") == member_k["SK"]:
            member = item
    if meta is None or member is None:
        return NOT_FOUND
    if is_tombstone(meta) or meta.get("status") != "active":
        return NOT_FOUND
    role = member.get("role")
    return AccessResult(ok=True, project=meta, role=role if role in ROLES else None)


__all__ = [
    "AccessCheckIncomplete",
    "AccessResult",
    "NOT_FOUND",
    "check_access",
    "is_project_id",
]
