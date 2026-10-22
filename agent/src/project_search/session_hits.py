"""Session_Hit_Store: delivered Hits that outlive a turn (Req 1.20, 2.5).

The per-turn Search_Ledger (`ledger.py`) and the runtime microVM are both short-lived, so verifying a
Citation against an earlier turn (Req 2.5) and accepting an earlier frame as a query image
(Req 1.20(c)) need Hits that persist for the whole chat session, keyed by the `project_id` each Hit
was returned under. Only the Main_Agent runtime reads or writes this store.

This module defines the store contract (`SessionHit`, `SessionHitStoreError`, `SessionHitStore`
Protocol) and the in-memory implementation (`MemorySessionHitStore`) used by unit tests and local
development. `DynamoSessionHitStore` (the deployed implementation on the plan-state table) lives
beside it and shares this ordering, cap and idempotence contract.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

from .ledger import SearchLedgerEntry

# `hits` / `frame_keys` return the newest this-many call items (one call item == one ok entry).
# Mirrors DynamoSessionHitStore's `Query(ScanIndexForward=False)` stopping after 100 call items;
# older Hits are not candidates, which errs toward verifying less (design Data Models).
MAX_CALL_ITEMS = 100


@dataclass(frozen=True)
class SessionHit:
    hit: dict                       # the full, untruncated Hit exactly as delivered (ShapedResult.delivered)
    project_id: str                 # the ledger entry's project_id
    turn_id: str
    tool_use_id: str
    index: int                      # position in the delivered list
    recency: tuple[int, int, int]   # (turn_seq, call_index, -index); larger is more recent (Req 3.4 tie-break)


class SessionHitStoreError(Exception):
    """Raised when the backing store cannot be read or written."""


class SessionHitStore(Protocol):
    def append(self, session_id: str, turn_id: str, entries: Sequence[SearchLedgerEntry]) -> None:
        """Persist every Hit of every entry with ok=True, tagged with the entry's project_id. turn_seq is the
        append time in epoch ms (turns of one session are sequential); call_index is the entry's position in
        `entries`. Idempotent per (turn_id, tool_use_id): a repeated append writes nothing new.
        Raises SessionHitStoreError."""
        ...

    def hits(self, session_id: str, project_id: str) -> list[SessionHit]:
        """Every stored Hit of the session returned under project_id, most recent first. Hits returned under
        another Project are never included. Raises SessionHitStoreError."""
        ...

    def frame_keys(self, session_id: str, project_id: str) -> frozenset[str]:
        """The frame_s3_key of every stored Hit under project_id (Component 2, source (c))."""
        ...


@dataclass
class _CallItem:
    """One ok entry's delivered Hits, with the sort coordinates shared by every Hit of the call."""
    turn_id: str
    tool_use_id: str
    project_id: str
    turn_seq: int
    call_index: int
    hits: list[SessionHit]

    @property
    def order(self) -> tuple[int, int]:
        return (self.turn_seq, self.call_index)


class MemorySessionHitStore:
    """In-memory `SessionHitStore` for tests and local development.

    Keyed like the plan-state table (session, then call item), with the same ordering, 100-call-item
    cap and (turn_id, tool_use_id) idempotence as `DynamoSessionHitStore`. Thread-safe: `append` runs
    on the turn-end worker thread while reads may run on the request thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, list[_CallItem]] = {}
        # (turn_id, tool_use_id) already appended, per session — gives idempotence.
        self._seen: dict[str, set[tuple[str, str]]] = {}
        # turn_seq memoized per (session_id, turn_id) so a repeated append reuses the same key.
        self._turn_seq: dict[str, dict[str, int]] = {}

    def append(self, session_id: str, turn_id: str, entries: Sequence[SearchLedgerEntry]) -> None:
        try:
            with self._lock:
                seen = self._seen.setdefault(session_id, set())
                turn_seqs = self._turn_seq.setdefault(session_id, {})
                if turn_id not in turn_seqs:
                    turn_seqs[turn_id] = time.time_ns() // 1_000_000  # epoch ms, memoized per turn
                turn_seq = turn_seqs[turn_id]
                calls = self._calls.setdefault(session_id, [])
                for call_index, entry in enumerate(entries):
                    if not getattr(entry, "ok", False):
                        continue
                    dedup = (turn_id, entry.tool_use_id)
                    if dedup in seen:
                        continue  # idempotent per (turn_id, tool_use_id)
                    seen.add(dedup)
                    session_hits = [
                        SessionHit(
                            hit=hit,
                            project_id=entry.project_id,
                            turn_id=entry.turn_id,
                            tool_use_id=entry.tool_use_id,
                            index=index,
                            recency=(turn_seq, call_index, -index),
                        )
                        for index, hit in enumerate(entry.hits)
                    ]
                    calls.append(
                        _CallItem(
                            turn_id=entry.turn_id,
                            tool_use_id=entry.tool_use_id,
                            project_id=entry.project_id,
                            turn_seq=turn_seq,
                            call_index=call_index,
                            hits=session_hits,
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — contract is a single error type
            raise SessionHitStoreError(str(exc)) from exc

    def _recent_calls(self, session_id: str, project_id: str) -> list[_CallItem]:
        """This Project's call items for the session, newest first, capped at MAX_CALL_ITEMS."""
        with self._lock:
            matching = [c for c in self._calls.get(session_id, ()) if c.project_id == project_id]
        matching.sort(key=lambda c: c.order, reverse=True)
        return matching[:MAX_CALL_ITEMS]

    def hits(self, session_id: str, project_id: str) -> list[SessionHit]:
        try:
            out: list[SessionHit] = []
            for call in self._recent_calls(session_id, project_id):
                out.extend(call.hits)
            out.sort(key=lambda sh: sh.recency, reverse=True)
            return out
        except SessionHitStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SessionHitStoreError(str(exc)) from exc

    def frame_keys(self, session_id: str, project_id: str) -> frozenset[str]:
        try:
            keys: set[str] = set()
            for call in self._recent_calls(session_id, project_id):
                for sh in call.hits:
                    key = sh.hit.get("frame_s3_key") if isinstance(sh.hit, dict) else None
                    if key:
                        keys.add(key)
            return frozenset(keys)
        except SessionHitStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SessionHitStoreError(str(exc)) from exc


# ---------------------------------------------------------------------------
# DynamoSessionHitStore — the deployed implementation on the plan-state table.
# ---------------------------------------------------------------------------

import gzip
import json
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Env var naming the table. `SESSION_HITS_TABLE` wins so local dev can point at the
#: `mediaworks-harness-plan-state-local` moto table; otherwise the deployed `PLAN_STATE_TABLE`.
SESSION_HITS_TABLE_ENV = "SESSION_HITS_TABLE"
PLAN_STATE_TABLE_ENV = "PLAN_STATE_TABLE"
#: Loopback-only moto endpoint for local development (ignored in deployed envs).
SESSION_HITS_DDB_ENDPOINT_ENV = "SESSION_HITS_DDB_ENDPOINT"

#: Envs where the endpoint override is never honored (the agent runs with the real table).
_DEPLOYED_ENVS = frozenset({"sandbox", "staging", "prod", "source", "customer"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: PK namespace: a different partition from the session's plan rows (bare session id), so the plan
#: chainer's `Query(session_id = <sid>)` never reads a Hit item (design Data Models).
HITS_PK_PREFIX = "HITS#"
#: DynamoDB's hard item limit is 400 KB; stay under it with margin.
HITS_GZ_MAX_BYTES = 350_000
#: 30-day sliding expiry, independent of the 24-hour TTL plan rows use.
TTL_SECONDS = 30 * 24 * 60 * 60


def _loopback_endpoint(value: str | None, deploy_env: str) -> str | None:
    """`value` only when it is a loopback http(s) URL and we are not in a deployed env, else None.

    A stray `SESSION_HITS_DDB_ENDPOINT` in staging/prod must not redirect Hit writes off the real
    table; anything non-loopback (or any deployed env) is ignored and logged.
    """
    value = (value or "").strip()
    if not value:
        return None
    if deploy_env in _DEPLOYED_ENVS:
        logger.info("SESSION_HITS_DDB_ENDPOINT ignored in deploy_env=%s", deploy_env)
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        logger.info("SESSION_HITS_DDB_ENDPOINT ignored (unparseable)")
        return None
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        logger.info("SESSION_HITS_DDB_ENDPOINT ignored (non-loopback)")
        return None
    return value


class DynamoSessionHitStore:
    """`SessionHitStore` on the plan-state table (design Data Models, "Session_Hit_Store").

    One DynamoDB item per ok call: PK `HITS#<session_id>`, SK
    `<project_id>#<turn_seq:013d>#<call_index:02d>#<turn_id>#<tool_use_id>`. The full, untruncated
    delivered Hits of that call are stored gzipped in `hits_gz`; `frame_keys` is their non-empty
    `frame_s3_key` list so `frame_keys()` reads no `hits_gz`. Appends are idempotent per
    `(turn_id, tool_use_id)` via `PutItem` with `attribute_not_exists(node_id)` over a `turn_seq`
    memoized per turn. Reads `Query` the `HITS#<sid>` partition, `begins_with(node_id, "<pid>#")`,
    `ScanIndexForward=False`, `ConsistentRead=True`, stopping after the 100 most recent call items.

    Shares the ordering, 100-call-item cap and idempotence contract of `MemorySessionHitStore`.
    """

    def __init__(self, table_name: str, endpoint_url: str | None = None, *, region: str | None = None) -> None:
        if not table_name:
            raise SessionHitStoreError("DynamoSessionHitStore requires a table name")
        self._table_name = table_name
        self._endpoint_url = endpoint_url or None
        self._region = region or os.environ.get("AWS_REGION", "us-west-2")
        self._table = None  # lazy boto3 resource
        self._lock = threading.Lock()
        # turn_seq memoized per (session_id, turn_id) so a repeated append computes the same key.
        self._turn_seq: dict[str, dict[str, int]] = {}

    @classmethod
    def from_env(cls, environ=None) -> "DynamoSessionHitStore":
        """Build from env: table from `SESSION_HITS_TABLE` else `PLAN_STATE_TABLE`; endpoint from
        `SESSION_HITS_DDB_ENDPOINT` only for a loopback URL outside deployed envs."""
        env = os.environ if environ is None else environ
        table = (env.get(SESSION_HITS_TABLE_ENV) or env.get(PLAN_STATE_TABLE_ENV) or "").strip()
        if not table:
            raise SessionHitStoreError(
                f"neither {SESSION_HITS_TABLE_ENV} nor {PLAN_STATE_TABLE_ENV} is set"
            )
        deploy_env = (env.get("DEPLOY_ENV") or "").strip()
        endpoint = _loopback_endpoint(env.get(SESSION_HITS_DDB_ENDPOINT_ENV), deploy_env)
        return cls(table, endpoint_url=endpoint, region=env.get("AWS_REGION"))

    def _tbl(self):
        """Lazily build and cache the boto3 DynamoDB table resource."""
        if self._table is None:
            with self._lock:
                if self._table is None:
                    import boto3
                    kwargs = {"region_name": self._region}
                    if self._endpoint_url:
                        kwargs["endpoint_url"] = self._endpoint_url
                    self._table = boto3.resource("dynamodb", **kwargs).Table(self._table_name)
        return self._table

    def _turn_seq_for(self, session_id: str, turn_id: str) -> int:
        """Epoch ms of the turn's first append, memoized per (session_id, turn_id)."""
        with self._lock:
            seqs = self._turn_seq.setdefault(session_id, {})
            if turn_id not in seqs:
                seqs[turn_id] = time.time_ns() // 1_000_000
            return seqs[turn_id]

    @staticmethod
    def _node_id(project_id: str, turn_seq: int, call_index: int, turn_id: str, tool_use_id: str) -> str:
        # Zero-padded turn_seq (13 digits spans year ~2286 in ms) and call_index (2 digits) make the
        # lexical sort order equal the numeric (turn_seq, call_index) order, newest-first under
        # ScanIndexForward=False (design Data Models).
        return f"{project_id}#{turn_seq:013d}#{call_index:02d}#{turn_id}#{tool_use_id}"

    def append(self, session_id: str, turn_id: str, entries: Sequence[SearchLedgerEntry]) -> None:
        from botocore.exceptions import ClientError

        try:
            table = self._tbl()
            pk = HITS_PK_PREFIX + session_id
            turn_seq = self._turn_seq_for(session_id, turn_id)
            for call_index, entry in enumerate(entries):
                if not getattr(entry, "ok", False):
                    continue
                delivered = list(entry.hits)
                stored, dropped = self._fit(delivered, session_id, entry.turn_id, entry.tool_use_id)
                frame_keys = [
                    hit["frame_s3_key"]
                    for hit in stored
                    if isinstance(hit, dict) and isinstance(hit.get("frame_s3_key"), str) and hit.get("frame_s3_key")
                ]
                node_id = self._node_id(entry.project_id, turn_seq, call_index, entry.turn_id, entry.tool_use_id)
                item = {
                    "session_id": pk,
                    "node_id": node_id,
                    "turn_id": entry.turn_id,
                    "tool_use_id": entry.tool_use_id,
                    "project_id": entry.project_id,
                    "source": getattr(entry, "source", "search_tool"),
                    "turn_seq": turn_seq,
                    "call_index": call_index,
                    "hit_count": len(stored),
                    "dropped": dropped,
                    "hits_gz": self._gz(stored),
                    "frame_keys": frame_keys,
                    "ttl": turn_seq // 1000 + TTL_SECONDS,
                }
                try:
                    table.put_item(Item=item, ConditionExpression="attribute_not_exists(node_id)")
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                        continue  # already written — idempotent per (turn_id, tool_use_id)
                    raise
        except SessionHitStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 — contract is a single error type
            raise SessionHitStoreError(str(exc)) from exc

    @staticmethod
    def _gz(hits: list) -> bytes:
        return gzip.compress(json.dumps(hits, ensure_ascii=False).encode("utf-8"))

    @classmethod
    def _fit(cls, delivered: list, session_id: str, turn_id: str, tool_use_id: str) -> tuple[list, int]:
        """`delivered` trimmed from the end until its gzip fits HITS_GZ_MAX_BYTES; returns (stored, dropped).

        Dropped Hits are still used for this turn's verification and Moments from memory; only the
        durable copy is capped (design "Size bounds").
        """
        stored = list(delivered)
        if len(cls._gz(stored)) <= HITS_GZ_MAX_BYTES:
            return stored, 0
        while stored and len(cls._gz(stored)) > HITS_GZ_MAX_BYTES:
            stored.pop()
        dropped = len(delivered) - len(stored)
        logger.warning(
            "Session_Hit_Store size cap: dropped %d trailing Hit(s) for session=%s turn=%s tool_use=%s",
            dropped, session_id, turn_id, tool_use_id,
        )
        return stored, dropped

    def _recent_items(self, session_id: str, project_id: str, projection: str | None = None) -> list[dict]:
        """The project's call items for the session, newest first, capped at MAX_CALL_ITEMS."""
        from boto3.dynamodb.conditions import Key

        kwargs: dict = {
            "KeyConditionExpression": Key("session_id").eq(HITS_PK_PREFIX + session_id)
            & Key("node_id").begins_with(f"{project_id}#"),
            "ScanIndexForward": False,  # newest (highest sort key) first
            "ConsistentRead": True,
            "Limit": MAX_CALL_ITEMS,
        }
        if projection is not None:
            kwargs["ProjectionExpression"] = projection
        resp = self._tbl().query(**kwargs)
        return resp.get("Items", [])

    @staticmethod
    def _unwrap(gz) -> list:
        raw = getattr(gz, "value", gz)  # boto3 Binary → bytes
        if isinstance(raw, bytearray):
            raw = bytes(raw)
        return json.loads(gzip.decompress(raw).decode("utf-8"))

    def hits(self, session_id: str, project_id: str) -> list[SessionHit]:
        try:
            out: list[SessionHit] = []
            for item in self._recent_items(session_id, project_id):
                turn_seq = int(item["turn_seq"])
                call_index = int(item["call_index"])
                for index, hit in enumerate(self._unwrap(item["hits_gz"])):
                    out.append(
                        SessionHit(
                            hit=hit,
                            project_id=item["project_id"],
                            turn_id=item["turn_id"],
                            tool_use_id=item["tool_use_id"],
                            index=index,
                            recency=(turn_seq, call_index, -index),
                        )
                    )
            out.sort(key=lambda sh: sh.recency, reverse=True)
            return out
        except SessionHitStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SessionHitStoreError(str(exc)) from exc

    def frame_keys(self, session_id: str, project_id: str) -> frozenset[str]:
        try:
            keys: set[str] = set()
            for item in self._recent_items(session_id, project_id, projection="frame_keys"):
                for key in item.get("frame_keys", ()) or ():
                    if key:
                        keys.add(key)
            return frozenset(keys)
        except SessionHitStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SessionHitStoreError(str(exc)) from exc
