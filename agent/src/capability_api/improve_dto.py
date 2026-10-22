"""Wire-shaping for the Improve read route — pure stdlib, deliberately so.

Three helpers, free of any boto3, ``improve``-package, or model import so they
clear the Capability API isolation boundary (``tests/test_capability_api_isolation.py``):

* :func:`merge_overlay` — lay this actor's dismiss overlay on top of the global
  bundle. A faithful port of the Step-2 proxy merge (``ui/server/app.ts``). The
  direction is strictly **bundle → overlay**: iterate the bundle's suggestions
  and look each one up, and NEVER fabricate a suggestion from an overlay-only id.
  That makes a stale ``SUGG#`` row — one whose suggestion is no longer mined —
  harmless: it simply matches nothing.

* :func:`with_actor_bundle` — attach this actor's own evidence under one key,
  when they have any. OPTIONAL and usually absent, which is why it is an attach
  rather than a merge: the producer is a daily cron over a bounded harvest window
  and an actor with no conversational session in it never gets an item at all, so
  the global bundle alone must be a complete answer. Absent means the key is
  simply not there — never an empty object, which a consumer would read as
  "measured, and you have nothing".

* :func:`decode_decimals` — the boto3 resource layer returns every number as a
  ``Decimal`` (the SNAPSHOT was seeded with ``parse_float=Decimal``), which
  ``json`` / FastAPI's ``JSONResponse`` cannot serialize. Convert on the way out;
  an integral ``Decimal`` becomes ``int`` so a version or count renders as ``4``,
  not ``4.0``.

There is no runtime call to ``improve.dto`` here: the SNAPSHOT already holds the
finished wire bundle, so the route's only job is this per-actor merge. (When the
richer read DTOs land they will import ``improve/contract.py`` and pull the
``improve`` package into the image — that is a separate, later change.)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

#: The only overlay status the merge acts on. The Step-2 proxy wrote exactly this
#: value on dismiss and deleted the row on undo, so an overlay row is either
#: "dismissed" or absent; anything else is treated as absent (open).
DISMISSED: str = "dismissed"

#: Field naming the suggestion an overlay row targets, in the wire bundle.
SUGGESTION_ID_KEY: str = "suggestionId"

#: Where this actor's own evidence hangs on the served payload. A separate key rather than fields
#: merged into the global bundle: the two come from different DynamoDB items with different
#: audiences, and a reader that cannot see which is which cannot tell a platform-wide number from
#: their own.
ACTOR_KEY: str = "actor"

#: Provenance the actor bundle's ITEM carries beside its bundle, lifted onto the served object.
#: Without it a bundle from an earlier producer run is indistinguishable from a current one — the
#: global bundle refreshes daily and an actor's only refreshes when they had a session in the
#: window, so the two dates legitimately differ and the difference has to be visible.
ACTOR_CREATED_AT_KEY: str = "createdAt"


def merge_overlay(
    bundle: Mapping[str, Any], overlay_by_id: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a copy of ``bundle`` with dismissed suggestions marked.

    ``overlay_by_id`` is keyed by suggestion id (see
    ``ImproveStore.get_overlay``). Only a suggestion already present in the
    bundle can be flipped to ``dismissed``; an overlay id with no matching
    suggestion is ignored.
    """
    suggestions = bundle.get("suggestions")
    if not isinstance(suggestions, list):
        # Nothing to merge (or a malformed bundle); hand it back untouched rather
        # than inventing a suggestions list the source never had.
        return dict(bundle)
    merged: list[Any] = []
    for suggestion in suggestions:
        if not isinstance(suggestion, Mapping):
            merged.append(suggestion)
            continue
        overlay = overlay_by_id.get(suggestion.get(SUGGESTION_ID_KEY))
        if isinstance(overlay, Mapping) and overlay.get("status") == DISMISSED:
            merged.append({**suggestion, "status": DISMISSED})
        else:
            merged.append(suggestion)
    return {**bundle, "suggestions": merged}


def with_actor_bundle(
    bundle: Mapping[str, Any], actor_item: Any
) -> dict[str, Any]:
    """Return a copy of ``bundle`` with this actor's own evidence under :data:`ACTOR_KEY`.

    ``actor_item`` is the raw ``(actorId, "SNAPSHOT")`` row from the caller's partition, or
    ``None``. Anything that is not a row carrying a bundle — absent, malformed, or an item written
    before the bundle attribute existed — leaves the payload untouched, so the surface serves the
    global answer and degrades to "we have nothing of yours" rather than to a blank page. That is
    the ordinary state for most actors on most days, not an error path.

    The item's ``createdAt`` is lifted INTO the attached object rather than left on the row, so a
    consumer holding only the served payload can still see how old this actor's evidence is. It is
    omitted when the row does not carry one — absent, not an empty string.
    """
    if not isinstance(actor_item, Mapping):
        return dict(bundle)
    inner = actor_item.get("bundle")
    if not isinstance(inner, Mapping):
        return dict(bundle)
    attached = dict(inner)
    created_at = actor_item.get(ACTOR_CREATED_AT_KEY)
    if isinstance(created_at, str) and created_at:
        attached[ACTOR_CREATED_AT_KEY] = created_at
    return {**bundle, ACTOR_KEY: attached}


def decode_decimals(obj: Any) -> Any:
    """Recursively convert ``Decimal`` to ``int``/``float`` for JSON encoding.

    Integral values become ``int`` (a version/count is not a float); everything
    else becomes ``float``. Dicts and lists are rebuilt; other types pass through.
    """
    if isinstance(obj, Decimal):
        integral = int(obj)
        return integral if obj == integral else float(obj)
    if isinstance(obj, dict):
        return {key: decode_decimals(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [decode_decimals(value) for value in obj]
    return obj


__all__ = [
    "ACTOR_CREATED_AT_KEY",
    "ACTOR_KEY",
    "DISMISSED",
    "SUGGESTION_ID_KEY",
    "decode_decimals",
    "merge_overlay",
    "with_actor_bundle",
]
