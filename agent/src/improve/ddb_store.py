"""DynamoDB-backed suggestion store — the "step 2" swap ``store.SuggestionStore`` promised.

``SuggestionStore`` is a JSON file, and its own docstring says so: *"local scratch until step 2
replaces it with DynamoDB. The interface is the part meant to survive — ``upsert_all`` /
``all_suggestions`` map onto a conditional PutItem and a Query."* In a Lambda the file store is
worse than useless — ``/tmp`` evaporates between invocations, so ``firstSeenAt``,
``reproducing`` and any human ``status`` (the whole reason the store exists — a finding must not
vanish when its evidence ages out) would reset every run.

This is that swap. It keeps the ``SuggestionStore`` interface byte-for-byte
(``upsert_all(suggestions, *, seen_at)`` / ``all_suggestions(include_dismissed=...)`` / ``save`` /
``__len__``) so ``mine_local``'s ``--emit-wire`` logic — replicated in ``improve.aws_lambda`` — is
unchanged. The transport is different; the semantics are not.

Storage shape — ONE producer-private item, deliberately invisible to the consumer
--------------------------------------------------------------------------------
    (actorId="__system__", sk="SUGGESTION_STORE")  ->  {version, suggestions: {id: row}}

This is NOT the SNAPSHOT the Capability API serves. The consumer
(``capability.improve_store.ImproveStore``) only ever (a) point-gets ``(__system__,
"SNAPSHOT")`` and (b) queries ``begins_with(sk, "SUGG#")`` on REAL actor partitions. This item
is under ``__system__`` with an ``sk`` that is neither ``SNAPSHOT`` nor a ``SUGG#`` prefix, so
no consumer read can reach it. It is the producer's cross-run scratch; the finished bundle is
written separately to ``SNAPSHOT`` by the worker.

Why a single item and not a row-per-suggestion: the file store is a single JSON blob and
``upsert_all`` reasons over the WHOLE set at once (every stored row absent from a batch is
marked ``reproducing=False``). One item preserves those exact semantics with one GetItem and one
PutItem, and suggestion counts are dozens — nowhere near the 400 KB item ceiling. If that ceiling
is ever approached, this is the class to shard, behind the same interface.

boto3 is not imported here — the ``table`` (a resource-layer ``Table``) is handed in by the
caller, same contract as ``capability.improve_store`` and ``seed_snapshot``. Numbers come back
from the resource layer as ``Decimal``; ``_floatize`` on load restores the plain-``float``
contract ``SuggestionStore`` callers expect, and ``_decimalize`` on save satisfies the resource
layer (which rejects ``float``).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

# Must match `seed_snapshot.SYSTEM_ACTOR` / `capability.improve_store.SYSTEM_ACTOR`.
SYSTEM_ACTOR = "__system__"
# The producer-private sort key. NOT "SNAPSHOT" (the served bundle) and NOT a "SUGG#" prefix
# (per-actor overlay rows) — so no consumer read path can reach it.
STORE_SK = "SUGGESTION_STORE"

# Fields a pipeline run must NOT overwrite — identical to `store.SuggestionStore._PRESERVED`, and
# see there for why `dismissReason` is no longer one of them.
# `status` is the human's decision; `firstSeenAt` is history.
_PRESERVED = ("status", "firstSeenAt")


def _decimalize(obj: Any) -> Any:
    """Recursively convert ``float`` -> ``Decimal`` so the resource layer accepts the item.

    The DynamoDB resource layer raises ``TypeError: Float types are not supported`` on a bare
    ``float``; every score (0.983, ...) must be a ``Decimal``. ``str(float)`` first avoids the
    binary-float noise ``Decimal(float)`` would introduce.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_decimalize(v) for v in obj]
    return obj


def _floatize(obj: Any) -> Any:
    """Recursively convert ``Decimal`` -> ``int``/``float`` restoring the in-memory contract.

    ``SuggestionStore`` callers (``upsert_all``, ``all_suggestions``, and downstream ``to_wire``)
    expect plain numbers, and ``json.dump`` cannot serialize ``Decimal``. An integral value comes
    back as ``int`` so an id-like or count field is not turned into ``3.0``.
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _floatize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_floatize(v) for v in obj]
    return obj


class DdbSuggestionStore:
    """Cross-run suggestion store over one DynamoDB item. Interface-compatible with
    ``store.SuggestionStore`` so the emit-wire logic does not change."""

    def __init__(self, table: Any) -> None:
        self._table = table
        self._rows: dict = {}
        self._load()

    # ---- persistence --------------------------------------------------------
    def _load(self) -> None:
        resp = self._table.get_item(Key={"actorId": SYSTEM_ACTOR, "sk": STORE_SK})
        item = resp.get("Item") or {}
        # `_floatize` so every downstream reader sees the same plain-number rows the file store
        # produced; an absent item is an empty store (first run), not an error.
        self._rows = _floatize(item.get("suggestions") or {})

    def save(self) -> None:
        """Write the whole set back as the single producer-private item.

        A full PutItem, not per-row updates: `upsert_all` has already reasoned over the entire
        set (marking absent rows non-reproducing), so the in-memory `_rows` IS the new truth.
        """
        self._table.put_item(Item={
            "actorId": SYSTEM_ACTOR,
            "sk": STORE_SK,
            "version": 1,
            "suggestions": _decimalize(self._rows),
        })

    # ---- writes (verbatim semantics of SuggestionStore.upsert_all) ----------
    def upsert_all(self, suggestions: Iterable[dict], *, seen_at: str) -> dict:
        """Record this run's observations; return a summary of what changed.

        Every stored suggestion NOT in this batch is marked ``reproducing=False`` and kept —
        absence from one run is information, not grounds for deletion. A pipeline run refreshes
        the evidence and never a human decision (``_PRESERVED``).
        """
        batch = {s["suggestionId"]: s for s in suggestions}
        added, updated = [], []

        for suggestion_id, observed in batch.items():
            existing = self._rows.get(suggestion_id)
            row = dict(observed)
            row["lastSeenAt"] = seen_at
            row["reproducing"] = True
            if existing is None:
                row["firstSeenAt"] = seen_at
                row.setdefault("status", "open")
                added.append(suggestion_id)
            else:
                for field in _PRESERVED:
                    if field in existing:
                        row[field] = existing[field]
                row.setdefault("firstSeenAt", seen_at)
                row.setdefault("status", "open")
                updated.append(suggestion_id)
            self._rows[suggestion_id] = row

        quiet = [sid for sid in self._rows if sid not in batch]
        for suggestion_id in quiet:
            self._rows[suggestion_id]["reproducing"] = False

        return {"added": added, "updated": updated, "quiet": quiet, "total": len(self._rows)}

    # ---- reads --------------------------------------------------------------
    def all_suggestions(self, include_dismissed: bool = False) -> list[dict]:
        """Every stored suggestion, newest first. No time filter — the findings must not have a
        window, or the disappearance this store exists to prevent returns wearing a filter."""
        rows = [
            dict(row) for row in self._rows.values()
            if include_dismissed or row.get("status") != "dismissed"
        ]
        return sorted(rows, key=lambda r: r.get("lastSeenAt") or "", reverse=True)

    def __len__(self) -> int:
        return len(self._rows)


__all__ = ["DdbSuggestionStore", "SYSTEM_ACTOR", "STORE_SK", "_decimalize", "_floatize"]
