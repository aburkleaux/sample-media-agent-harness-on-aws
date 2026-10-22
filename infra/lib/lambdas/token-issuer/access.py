"""Project membership check for the token issuer (design Component 3 "Python twin", Data Models).

``check_access(table, sub, project_id)`` is the Python twin of ``checkAccess`` in
``ui/server/projects/access.ts``. ``issue_handler`` runs it before signing a first Project_Token and
``refresh_handler`` runs it before every Token_Refresh (Req 4.1, 4.8, 4.21).

``table`` is a boto3 DynamoDB **resource** ``Table`` (``boto3.resource("dynamodb", ...).Table(name)``),
not a low-level client. The Table carries both the table name and the client it was built with, so the
caller decides the endpoint: Lambda passes the default endpoint and ``PROJECTS_TABLE``; local dev passes
``endpoint_url`` pointing at a moto server. ``open_table`` builds one from those arguments. The read goes
through ``table.meta.client.batch_get_item``, which on a resource-built client takes and returns plain
Python values (no ``{"S": ...}`` wrappers), because a Table has no ``batch_get_item`` of its own.

Semantics, identical to the TS function:

- A malformed ``project_id`` (not ``^p[0-9a-f]{32}$``) or an empty ``sub`` is not-found with no read.
- Otherwise exactly one ``BatchGetItem`` with ``ConsistentRead=True`` for ``PROJECT#<pid>/META`` and
  ``PROJECT#<pid>/MEMBER#<sub>``. ``UnprocessedKeys`` are re-requested once.
- ok only when META exists, is not an id tombstone, has ``status = active``, and the member item exists.
  Every denial is the same ``AccessResult(ok=False)`` (Req 1.17).

A read that DynamoDB could not complete (keys still unprocessed after the one retry, or a client error)
raises instead of answering not-found. Not-found ends the user's access (``access_ended`` on refresh), so
a throttle must surface as a failure, not as a denial.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tokens import is_project_id

__all__ = [
    "AccessCheckIncomplete",
    "AccessResult",
    "NOT_FOUND",
    "check_access",
    "is_project_id",
    "member_key",
    "meta_key",
    "open_table",
]

META_SK = "META"
MEMBER_SK_PREFIX = "MEMBER#"
PROJECT_PK_PREFIX = "PROJECT#"
ROLES = frozenset({"owner", "member"})


@dataclass(frozen=True)
class AccessResult:
    """``ok=True`` carries the META item and the member's role; every denial is ``NOT_FOUND``."""

    ok: bool
    project: Mapping[str, Any] | None = field(default=None, compare=False)
    role: str | None = None


NOT_FOUND = AccessResult(ok=False)


class AccessCheckIncomplete(Exception):
    """DynamoDB left keys unprocessed after the one retry; the check has no answer."""


def meta_key(project_id: str) -> dict[str, str]:
    return {"PK": f"{PROJECT_PK_PREFIX}{project_id}", "SK": META_SK}


def member_key(project_id: str, sub: str) -> dict[str, str]:
    return {"PK": f"{PROJECT_PK_PREFIX}{project_id}", "SK": f"{MEMBER_SK_PREFIX}{sub}"}


def open_table(table_name: str, *, endpoint_url: str | None = None, region_name: str | None = None) -> Any:
    """A resource ``Table`` for ``table_name``; ``endpoint_url`` overrides the DynamoDB endpoint (moto)."""
    import boto3

    if not isinstance(table_name, str) or not table_name:
        raise ValueError("open_table needs a non-empty table name (PROJECTS_TABLE)")
    resource = boto3.resource("dynamodb", endpoint_url=endpoint_url or None, region_name=region_name)
    return resource.Table(table_name)


def _batch_get(table: Any, keys: list[dict[str, str]]) -> list[Mapping[str, Any]]:
    client = table.meta.client
    name = table.name
    request = {name: {"Keys": keys, "ConsistentRead": True}}
    resp = client.batch_get_item(RequestItems=request)
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
    """Return ok with the META item and role, or ``NOT_FOUND``. See the module docstring."""
    if not is_project_id(project_id) or not isinstance(sub, str) or not sub:
        return NOT_FOUND
    meta_k = meta_key(project_id)
    member_k = member_key(project_id, sub)
    meta: Mapping[str, Any] | None = None
    member: Mapping[str, Any] | None = None
    for item in _batch_get(table, [meta_k, member_k]):
        if item.get("PK") != meta_k["PK"]:
            continue
        if item.get("SK") == META_SK:
            meta = item
        elif item.get("SK") == member_k["SK"]:
            member = item
    if meta is None or member is None:
        return NOT_FOUND
    if meta.get("tombstone") or meta.get("status") != "active":
        return NOT_FOUND
    role = member.get("role")
    return AccessResult(ok=True, project=meta, role=role if role in ROLES else None)
