"""DynamoDB reader/writer for the Improve store, served by the Capability API.

Three item kinds in one table (PK ``actorId`` / SK ``sk``), exactly as the Step-2
``ui/server`` proxy and ``improve.seed_snapshot`` established:

* **The global bundle** at ``(actorId="__system__", sk="SNAPSHOT")``. The mined
  suggestions are platform/harness-code — identical for every user — so the
  finished wire bundle ``{suggestions, components, patterns}`` is stored once,
  written offline by ``improve.seed_snapshot`` (the ``improve-worker`` Lambda
  takes that over later). This module never writes it.
* **A per-actor bundle** at ``(actorId=<sub>, sk="SNAPSHOT")`` — the same sort key on
  the actor's own partition, holding the evidence that may not travel on a shared
  item: that actor's run rows with the judge's reason, the facet evidence behind
  the suggestions their runs support, and their waste findings with the tool
  sequence named (``improve.dto.actor_bundles``). OPTIONAL by design and usually
  absent: the producer is a daily cron over a bounded harvest window, so most
  actors have nothing in any given run, and an actor with no conversational
  session never has one at all. A read that finds none serves the global bundle
  alone — never a blank surface. This module never writes it either.
* **Per-actor dismiss overlay rows** at ``(actorId=<sub>, sk="SUGG#<id>")``,
  written by :meth:`ImproveStore.put_dismissal` from an authenticated
  ``POST /control-plane/improve/suggestions/:id/dismiss`` — the OI-5 human-label
  substrate. The actor is the verified token's subject, never the request body.

The last two share a partition, so :meth:`ImproveStore.get_actor_partition` reads
them in ONE query rather than adding a round trip: the key condition drops the
``begins_with`` and keeps ``actorId = :a``, and each row is then classified by its
``sk`` against the same two constants that name it. An unrecognised ``sk`` is
ignored, so a future row kind cannot masquerade as either.

Style follows ``capability/sources.py``: a typed :class:`ImproveStoreUnavailable`
rather than a raw exception string reaching the browser, and **no fallback** — a
failed read is an error, not silently-empty data.

The constants below MUST match ``improve.seed_snapshot`` (which writes SNAPSHOT)
and the Step-2 proxy in ``ui/server/app.ts`` (which wrote the ``SUGG#`` rows), or
a read finds nothing / a write lands under the wrong key.

Import-safe for the Capability API isolation boundary
(``tests/test_capability_api_isolation.py``): boto3 is never imported at module
scope. The DynamoDB resource ``Table`` is handed in by ``Deps.improve_store`` and
this module only calls methods on it — the same resource-layer contract
``seed_snapshot`` uses, so numbers come back as ``Decimal`` (decoded to JSON by
``capability_api.improve_dto.decode_decimals`` before the route returns).
"""

from __future__ import annotations

from typing import Any, Optional

#: The reserved global partition. Must match ``seed_snapshot.SYSTEM_ACTOR``, which
#: records why the reservation is enforced by the producer rather than assumed
#: from the shape of an actor id — 6 of 14 actors in the staging memory are not
#: UUIDs.
SYSTEM_ACTOR: str = "__system__"

#: Sort key of the single global bundle. Must match ``seed_snapshot.SNAPSHOT_SK``.
SNAPSHOT_SK: str = "SNAPSHOT"

#: Sort-key prefix of a per-actor dismiss overlay row (``SUGG#<suggestionId>``).
#: Must match the Step-2 proxy's overlay writes in ``ui/server/app.ts``.
SUGG_PREFIX: str = "SUGG#"


class ImproveStoreUnavailable(RuntimeError):
    """The Improve store could not be read or written.

    Carries a stable ``kind`` (``error``) rather than a raw exception string,
    which can echo table internals; mirrors ``capability.sources.SourceUnavailable``.
    """

    def __init__(self, kind: str, detail: Optional[str] = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(kind if detail is None else f"{kind}: {detail}")


class ImproveStore:
    """Reader/writer over the Improve DynamoDB table.

    ``table`` is a boto3 DynamoDB resource ``Table`` (built lazily by
    ``Deps.improve_table``). Every method is synchronous — the async route wraps
    the calls in ``asyncio.to_thread`` so a boto3 round trip does not stall the
    always-on task's event loop.
    """

    def __init__(self, table: Any) -> None:
        self._table = table

    # -- reads ---------------------------------------------------------

    def get_snapshot(self) -> Optional[dict[str, Any]]:
        """The global SNAPSHOT item, or ``None`` when it has never been seeded.

        ``None`` is distinct from a populated bundle whose lists happen to be
        empty: the route turns the former into a 404 ("no data yet"), not an
        empty surface.
        """
        try:
            resp = self._table.get_item(
                Key={"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK}
            )
        except Exception as err:  # noqa: BLE001 — normalize to a typed 5xx
            raise ImproveStoreUnavailable("error", "snapshot read failed") from err
        item = resp.get("Item")
        return item if isinstance(item, dict) else None

    def get_actor_partition(
        self, actor_id: str
    ) -> tuple[Optional[dict[str, Any]], dict[str, dict[str, Any]]]:
        """``(this actor's bundle or None, their dismiss overlay keyed by suggestion id)``.

        ONE query for both, because both live on this actor's partition: the key
        condition is ``actorId = :a`` with no sort-key clause, and each row is
        classified by its ``sk`` — exactly ``SNAPSHOT_SK`` for the bundle,
        ``SUGG_PREFIX`` for an overlay row. An ``sk`` matching neither is ignored,
        so a schema surprise cannot masquerade as either. The raw-expression form
        is kept deliberately so boto3's condition helpers are not imported at
        module scope, keeping this module clear of the isolation-blocked stack.

        ``None`` for the bundle is the ORDINARY case, not an error: see the module
        docstring. The caller serves the global bundle alone.
        """
        bundle: Optional[dict[str, Any]] = None
        overlay: dict[str, dict[str, Any]] = {}
        start_key: Optional[dict[str, Any]] = None
        try:
            while True:
                kwargs: dict[str, Any] = {
                    "KeyConditionExpression": "actorId = :a",
                    "ExpressionAttributeValues": {":a": actor_id},
                }
                if start_key is not None:
                    kwargs["ExclusiveStartKey"] = start_key
                resp = self._table.query(**kwargs)
                for row in resp.get("Items", []) or []:
                    if not isinstance(row, dict):
                        continue
                    sk = str(row.get("sk") or "")
                    if sk == SNAPSHOT_SK:
                        bundle = row
                    elif sk.startswith(SUGG_PREFIX):
                        overlay[sk[len(SUGG_PREFIX):]] = row
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    break
        except ImproveStoreUnavailable:
            raise
        except Exception as err:  # noqa: BLE001 — normalize to a typed 5xx
            raise ImproveStoreUnavailable("error", "actor partition read failed") from err
        return bundle, overlay

    # -- writes --------------------------------------------------------

    def put_dismissal(
        self,
        actor_id: str,
        suggestion_id: str,
        dismissed_at: str,
        reason: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Upsert this actor's ``SUGG#`` dismissal row.

        ``reason``/``note`` are written only when the caller passed a non-empty
        value — absent ≠ empty, so a blank dismissal is not stored as a reasoned
        label. The route already normalizes empty strings to ``None``.
        """
        item: dict[str, Any] = {
            "actorId": actor_id,
            "sk": f"{SUGG_PREFIX}{suggestion_id}",
            "status": "dismissed",
            "dismissedAt": dismissed_at,
        }
        if reason:
            item["reason"] = reason
        if note:
            item["note"] = note
        try:
            self._table.put_item(Item=item)
        except Exception as err:  # noqa: BLE001 — normalize to a typed 5xx
            raise ImproveStoreUnavailable("error", "dismissal write failed") from err

    def delete_dismissal(self, actor_id: str, suggestion_id: str) -> None:
        """Delete this actor's ``SUGG#`` dismissal row (undo). Idempotent."""
        try:
            self._table.delete_item(
                Key={"actorId": actor_id, "sk": f"{SUGG_PREFIX}{suggestion_id}"}
            )
        except Exception as err:  # noqa: BLE001 — normalize to a typed 5xx
            raise ImproveStoreUnavailable("error", "dismissal delete failed") from err


__all__ = [
    "ImproveStore",
    "ImproveStoreUnavailable",
    "SNAPSHOT_SK",
    "SUGG_PREFIX",
    "SYSTEM_ACTOR",
]
