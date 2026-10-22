"""The single writer: bring a Project's graph in line with its DynamoDB rows.

``sync_project`` is idempotent and cheap when nothing changed (one META read + one ``_meta.json`` GET), so
it can run on every projects-table stream record of a graph-enabled META. Per table it is ONE Lance commit:

    merge_insert("id").when_matched_update_all().when_not_matched_insert_all()
                      .when_not_matched_by_source_delete("sample = false")

which upserts the project-derived rows and deletes the ones that disappeared, while never touching demo
(``sample = true``) rows. Tables whose project-derived rows are unchanged (content hash) are not rewritten.
Small-file hygiene: compact when a table has more than ``compact_fragments`` fragments, then drop versions
older than ``cleanup_older_than_s`` (bucket versioning + the 30-day noncurrent expiry reclaim the rest).

Concurrency: the Lambda runs with reserved concurrency 1, and ``_meta.json`` is a compare-and-swap
(``IfMatch``), so even an unexpected second writer cannot publish a snapshot built from a stale read.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ingestion.store import graph_on, jsonable
from projects import schema as pschema

from . import demo, layout, starter
from .schema import TABLES, arrow_schema, is_node_table

log = logging.getLogger(__name__)

# The META attributes a graph is derived from. A change to anything else (pipeline bookkeeping) is a no-op.
SOURCE_FIELDS = ("name", "description", "tags", "personal", "ownerSub", "memberCount", "changeSeq",
                 "statusCounts", "activeAssetCount")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class WriterDeps:
    table: Any                    # boto3 DynamoDB resource Table (projects table)
    s3: Any                       # boto3 S3 client (for _meta.json)
    bucket: str                   # ingest bucket
    root: str                     # "s3://<bucket>" in Lambda; a local directory in tests
    storage_options: Optional[dict] = None
    now: Callable[[], str] = _utc_now
    compact_fragments: int = 16
    cleanup_older_than_s: int = 900
    demo_seed: int = demo.DEMO_SEED
    versions: dict = field(default_factory=dict)


def source_signature(meta: dict) -> dict:
    picked = {k: jsonable(meta.get(k)) for k in SOURCE_FIELDS}
    digest = hashlib.sha256(json.dumps(picked, sort_keys=True, default=str).encode()).hexdigest()
    return {"changeSeq": picked.get("changeSeq"), "hash": digest[:32]}


def rows_hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:32]


def library_versions() -> dict:
    out = {}
    for pkg in ("pylance", "pyarrow", "lance-graph"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            pass
    return out


def _query_partition(table: Any, pid: str, sk_prefix: str) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    out: list[dict] = []
    kw: dict = {"KeyConditionExpression": Key("PK").eq(pschema.project_pk(pid)) & Key("SK").begins_with(sk_prefix),
                "ConsistentRead": True}
    while True:
        resp = table.query(**kw)
        out += resp.get("Items", [])
        if not resp.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _get_meta(table: Any, pid: str) -> Optional[dict]:
    return table.get_item(Key=pschema.meta_key(pid), ConsistentRead=True).get("Item")


def _live(meta: Optional[dict]) -> bool:
    return bool(meta) and not pschema.is_tombstone(meta) and meta.get("status") == "active"


def _open(uri: str, storage_options: Optional[dict], version: Optional[int] = None):
    import lance

    try:
        return lance.dataset(uri, version=version, storage_options=storage_options)
    except (ValueError, FileNotFoundError, OSError) as e:
        if "not found" in str(e).lower() or "does not exist" in str(e).lower() or isinstance(e, FileNotFoundError):
            return None
        raise


def _write_table(deps: WriterDeps, pid: str, table: str, real: list[dict], demo_rows: list[dict]) -> layout.TableState:
    import lance
    import pyarrow as pa

    uri = layout.table_uri(deps.root, pid, table)
    sch = arrow_schema(table)
    ds = _open(uri, deps.storage_options)
    if ds is None:
        data = pa.Table.from_pylist(real + demo_rows, schema=sch)
        ds = lance.write_dataset(data, uri, schema=sch, mode="create", data_storage_version=layout.STORAGE_VERSION,
                                 storage_options=deps.storage_options)
    else:
        (ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all()
         .when_not_matched_by_source_delete("sample = false")
         .execute(pa.Table.from_pylist(real, schema=sch)))
        if demo_rows:
            ds = _open(uri, deps.storage_options)
            ds.merge_insert("id").when_not_matched_insert_all().execute(pa.Table.from_pylist(demo_rows, schema=sch))
        ds = _open(uri, deps.storage_options)
        if len(ds.get_fragments()) > deps.compact_fragments:
            ds.optimize.compact_files()
            ds = _open(uri, deps.storage_options)
        ds.cleanup_old_versions(older_than=timedelta(seconds=deps.cleanup_older_than_s))
    return layout.TableState(version=ds.version, rows=ds.count_rows(), fragments=len(ds.get_fragments()),
                             hash=rows_hash(real))


def _delete_graph(deps: WriterDeps, pid: str) -> int:
    """Remove ``projects/<pid>/graph/`` (current objects) after a delete raced the write."""
    n = 0
    paginator = deps.s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=deps.bucket, Prefix=layout.graph_prefix(pid)):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            deps.s3.delete_objects(Bucket=deps.bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
            n += len(keys[i:i + 1000])
    return n


def sync_project(pid: str, deps: WriterDeps, *, force: bool = False, _attempt: int = 0) -> dict:
    t0 = time.perf_counter()
    meta = _get_meta(deps.table, pid)
    if not _live(meta):
        return {"projectId": pid, "result": "inactive"}
    if not graph_on(meta):
        return {"projectId": pid, "result": "off"}

    doc, etag, _ = layout.read_meta(deps.s3, deps.bucket, pid)
    source = source_signature(meta)
    demo_needed = not (doc and doc.demo.get("seeded"))
    if doc and not force and not demo_needed and doc.source == source:
        return {"projectId": pid, "result": "unchanged", "generation": doc.generation}

    members = _query_partition(deps.table, pid, pschema.MEMBER_SK_PREFIX)
    assets = _query_partition(deps.table, pid, pschema.ASSET_SK_PREFIX)
    # Job records (asset-lineage R4.1) give the `Job` nodes their label/tool and tie them to a session.
    # They are deliberately not part of `source_signature`: a job record is written together with the
    # outputs it reports, and every asset registration bumps META `changeSeq`, so the sync that picks up
    # those assets picks the job up with them.
    jobs = _query_partition(deps.table, pid, pschema.JOB_SK_PREFIX)
    desired = starter.project_rows(meta, members, assets, jobs)
    sample = demo.demo_rows(pid, deps.demo_seed) if demo_needed else {t: [] for t in TABLES}

    states: dict[str, layout.TableState] = {}
    written = 0
    for t in TABLES:
        prev = doc.tables.get(t) if doc else None
        if prev and not sample[t] and prev.hash == rows_hash(desired[t]) and not force:
            states[t] = prev
            continue
        states[t] = _write_table(deps, pid, t, desired[t], sample[t])
        written += 1

    new = layout.MetaDoc(
        generation=(doc.generation if doc else 0) + 1, updatedAt=deps.now(), tables=states,
        counts={"nodes": sum(s.rows for t, s in states.items() if is_node_table(t)),
                "edges": sum(s.rows for t, s in states.items() if not is_node_table(t))},
        source=source,
        demo={"seeded": True, "seed": deps.demo_seed} if (demo_needed or (doc and doc.demo.get("seeded"))) else {},
        versions=deps.versions or library_versions())
    try:
        layout.write_meta(deps.s3, deps.bucket, pid, new, etag)
    except layout.MetaConflict:
        if _attempt >= 2:
            raise
        log.warning("graph %s: _meta.json changed underneath the sync; redoing", pid)
        return sync_project(pid, deps, force=force, _attempt=_attempt + 1)

    if not _live(_get_meta(deps.table, pid)):   # deleted while we wrote: don't resurrect objects after purge
        removed = _delete_graph(deps, pid)
        return {"projectId": pid, "result": "deleted_during_sync", "removedObjects": removed}
    out = {"projectId": pid, "result": "synced", "generation": new.generation, "tablesWritten": written,
           "counts": new.counts, "ms": round((time.perf_counter() - t0) * 1000)}
    log.info("graph sync %s", json.dumps(out))
    return out
