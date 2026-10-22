"""The import inbox: files copied to ``s3://<ingest>/projects/<pid>/import/…`` become project assets by themselves.

A customer copies a shoot with their own tools (``aws s3 sync``, DataSync, Snowball to the same path). The sweep
(:mod:`ingestion.handlers.import_sweep`, once a minute, and Asset_Service ``rescan_import`` on demand) lists the
inbox and, for each object it has not handled yet, applies :func:`ingestion.import_rules.classify`:

* ``ingest`` / ``keep`` — registered IN PLACE (``source: s3``, no copy; the harness never deletes it), filed in the
  folder its path names (``import/Day1/Drone/x.mov`` → ``Day1/Drone``), added by the project's owner. ``ingest``
  rows queue for indexing like an upload; ``keep`` rows are stored, never queued.
* ``attention`` — recorded, listed under Needs attention, retried by a rescan (a limit may have been raised).
* ``ignore`` — recorded, nothing else.

Why a sweep and not an S3 event: the ingest bucket is shared with M2C (it is M2C's IngestBucket) and is imported,
not created, by the stacks that deploy staging and prod. Turning on its notifications would REPLACE the bucket's
whole notification configuration. Listing a prefix needs no change to the bucket at all.

Every handled key gets one ``IMPORTKEY#<sha256(key)>`` row (state, reason, size, etag, assetId), which makes the sweep
idempotent and means a file the user later removes is not imported again. ``IMPORT#STATE`` holds the watermark: a
periodic sweep only considers objects modified since it (with a margin); a rescan considers every object.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from projects import schema

from .folders import folder_or_error
from .import_rules import classify
from .limits import DEFAULT_UPLOAD_LIMITS
from .store import AssetStore, ProjectNotActive, RegistrationConflict

logger = logging.getLogger(__name__)

IMPORT_SEGMENT = "import"
IMPORT_KEY_SK_PREFIX = "IMPORTKEY#"
IMPORT_STATE_SK = "IMPORT#STATE"
# A periodic sweep re-reads objects modified this long before the watermark: S3 listing is strongly consistent,
# but an upload's LastModified is its START, so a long multipart upload can land "in the past".
WATERMARK_MARGIN = timedelta(hours=2)
# One sweep handles at most this many new objects per project (the rest wait for the next minute).
MAX_PER_SWEEP = 500

__all__ = ["import_prefix", "import_folder", "sweep_project", "import_status", "IMPORT_KEY_SK_PREFIX"]


def import_prefix(pid: str) -> str:
    return f"{schema.project_prefix(pid)}{IMPORT_SEGMENT}/"


def import_folder(pid: str, key: str) -> Optional[str]:
    """The project folder an inbox key files into: its directory under ``import/`` (``None`` = the root)."""
    rel = key[len(import_prefix(pid)):] if key.startswith(import_prefix(pid)) else ""
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if not parent:
        return None
    folder, err = folder_or_error(parent)
    return folder if folder and not err else None


def _key_row_key(pid: str, key: str) -> dict:
    return {"PK": schema.project_pk(pid), "SK": IMPORT_KEY_SK_PREFIX + hashlib.sha256(key.encode()).hexdigest()}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _list(s3: Any, bucket: str, prefix: str) -> Iterable[dict]:
    token: Optional[str] = None
    while True:
        kw: dict = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []) or []:
            if o.get("Key") and not o["Key"].endswith("/"):
                yield o
        if not resp.get("IsTruncated"):
            return
        token = resp.get("NextContinuationToken")
        if not token:
            return


def _known(store: AssetStore, pid: str, keys: list[str]) -> dict[str, dict]:
    """The ``IMPORTKEY#`` rows of ``keys`` (key -> row)."""
    out: dict[str, dict] = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        for row in store._batch_get([_key_row_key(pid, k) for k in chunk]):
            if isinstance(row.get("key"), str):
                out[row["key"]] = row
    return out


def sweep_project(store: AssetStore, s3: Any, bucket: str, pid: str, *, full: bool = False,
                  limits: Optional[Mapping[str, int]] = None, clock=time.time) -> dict:
    """Handle every new object in the project's inbox. ``full``: every object (and re-check the attention ones)."""
    limits = dict(limits or DEFAULT_UPLOAD_LIMITS)
    meta = store.get_meta(pid) or {}
    if meta.get("status") != "active":
        return {"skipped": "project_not_active"}
    owner = str(meta.get("ownerSub") or "import-inbox")
    started = datetime.fromtimestamp(clock(), timezone.utc)
    state = store.table.get_item(Key={"PK": schema.project_pk(pid), "SK": IMPORT_STATE_SK}).get("Item") or {}
    since: Optional[datetime] = None
    if not full and isinstance(state.get("watermark"), str):
        since = datetime.fromisoformat(state["watermark"].replace("Z", "+00:00")) - WATERMARK_MARGIN

    objects = [o for o in _list(s3, bucket, import_prefix(pid))
               if since is None or o.get("LastModified") is None or o["LastModified"] >= since]
    known = _known(store, pid, [o["Key"] for o in objects])
    counts = {"ingested": 0, "kept": 0, "attention": 0, "ignored": 0, "already": 0, "deferred": 0}
    handled = 0
    for o in objects:
        key, size, etag = o["Key"], int(o.get("Size") or 0), str(o.get("ETag") or "")
        prev = known.get(key)
        if prev and prev.get("etag") == etag and not (full and prev.get("state") == "attention"):
            counts["already"] += 1
            continue
        if handled >= MAX_PER_SWEEP:
            counts["deferred"] += 1
            continue
        handled += 1
        verdict = classify(key, size, None, limits)
        row: dict = {**_key_row_key(pid, key), "key": key, "size": size, "etag": etag, "seenAt": _now()}
        action = verdict["action"]
        if action in ("ingest", "keep"):
            aid = f"{verdict['kind']}:{uuid.uuid4()}"
            try:
                store.register_asset(
                    pid=pid, asset_id=aid, source="s3", source_uri=f"s3://{bucket}/{key}", source_bucket=bucket,
                    source_key=key, source_size=size, content_type="application/octet-stream",
                    file_name=key.rsplit("/", 1)[-1], added_by=owner, added_by_display=owner,
                    folder=import_folder(pid, key),
                    # `ingest` follows the project's indexing switch like an upload; `keep` is stored, never queued.
                    queue_for_indexing=None if action == "ingest" else False,
                )
                row.update({"state": "registered" if action == "ingest" else "kept", "assetId": aid})
                counts["ingested" if action == "ingest" else "kept"] += 1
            except RegistrationConflict:
                row.update({"state": "registered"})   # the SRC guard: already an asset of this project
                counts["already"] += 1
            except ProjectNotActive:
                return {"skipped": "project_not_active", **counts}
        elif action == "attention":
            row.update({"state": "attention", "reason": verdict["reason"]})
            counts["attention"] += 1
        else:
            row.update({"state": "ignored", "reason": verdict.get("reason", "ignored")})
            counts["ignored"] += 1
        store.table.put_item(Item=row)
    if counts["deferred"] == 0:
        store.table.put_item(Item={"PK": schema.project_pk(pid), "SK": IMPORT_STATE_SK,
                                   "watermark": started.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                                   "lastSweepAt": _now()})
    return {"scanned": len(objects), **counts}


def import_status(store: AssetStore, pid: str, *, issues_limit: int = 50) -> dict:
    """``{counts: {registered, kept, attention, ignored}, attention: [{key, reason, size}]}`` from the IMPORTKEY rows."""
    from boto3.dynamodb.conditions import Key

    counts = {"registered": 0, "kept": 0, "attention": 0, "ignored": 0}
    attention: list[dict] = []
    kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                    & Key("SK").begins_with(IMPORT_KEY_SK_PREFIX)}
    while True:
        resp = store.table.query(**kwargs)
        for r in resp.get("Items", []):
            st = r.get("state")
            if st in counts:
                counts[st] += 1
            if st == "attention" and len(attention) < issues_limit:
                attention.append({"key": r.get("key"), "reason": r.get("reason"), "size": int(r.get("size") or 0)})
        if not resp.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return {"counts": counts, "attention": attention}
