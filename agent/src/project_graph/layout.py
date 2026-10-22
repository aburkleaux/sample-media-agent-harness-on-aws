"""Where a Project's graph lives in S3, and the ``_meta.json`` snapshot manifest.

    s3://<ingest>/projects/<pid>/graph/_meta.json
    s3://<ingest>/projects/<pid>/graph/v1/nodes/<Label>.lance
    s3://<ingest>/projects/<pid>/graph/v1/edges/<TYPE>.lance

``_meta.json`` is written LAST by the single writer and names the exact Lance version of every table, so a
reader that opens each table *at that version* sees one consistent snapshot even while a later sync is
committing. Its ETag is the reader's cache key: one conditional GET replaces 21 dataset opens.
The ``graph/`` subtree sits inside the Project prefix on purpose — project purge already deletes every
object version under ``projects/<pid>/``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from projects import schema as pschema

from .schema import LAYOUT, SCHEMA_VERSION

STORAGE_VERSION = "2.1"  # lance-graph 0.5.4 embeds lance 1.0.1, which cannot decode format 2.2


def graph_prefix(pid: str) -> str:
    return pschema.project_prefix(pid) + "graph/"


def meta_key(pid: str) -> str:
    return graph_prefix(pid) + "_meta.json"


def table_uri(root: str, pid: str, table: str) -> str:
    """``root`` is ``s3://<bucket>`` in Lambda, or a local directory in tests."""
    return f"{root.rstrip('/')}/{graph_prefix(pid)}{LAYOUT}/{table}.lance"


@dataclass
class TableState:
    version: int
    rows: int
    fragments: int
    hash: str  # content hash of the project-derived rows last written (skip-if-unchanged)


@dataclass
class MetaDoc:
    generation: int = 0
    updatedAt: str = ""
    tables: dict[str, TableState] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    demo: dict[str, Any] = field(default_factory=dict)
    schemaVersion: int = SCHEMA_VERSION
    layout: str = LAYOUT
    storageVersion: str = STORAGE_VERSION
    versions: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes | str) -> "MetaDoc":
        d = json.loads(raw)
        tables = {k: TableState(**v) for k, v in (d.pop("tables", None) or {}).items()}
        known = {f for f in cls.__dataclass_fields__}
        return cls(tables=tables, **{k: v for k, v in d.items() if k in known})


class MetaConflict(Exception):
    """``_meta.json`` changed since it was read (S3 returned 412): reload and redo."""


def _code(err: Exception) -> str:
    return str(getattr(err, "response", {}).get("Error", {}).get("Code", ""))


def read_meta(s3: Any, bucket: str, pid: str, if_none_match: Optional[str] = None
              ) -> tuple[Optional[MetaDoc], Optional[str], bool]:
    """``(doc, etag, not_modified)``. ``doc`` is ``None`` when the graph has never been written."""
    from botocore.exceptions import ClientError

    kw: dict = {"Bucket": bucket, "Key": meta_key(pid)}
    if if_none_match:
        kw["IfNoneMatch"] = if_none_match
    try:
        resp = s3.get_object(**kw)
    except ClientError as e:
        code = _code(e)
        if code in ("304", "NotModified"):
            return None, if_none_match, True
        if code in ("NoSuchKey", "404", "NotFound"):
            return None, None, False
        raise
    return MetaDoc.from_json(resp["Body"].read()), resp.get("ETag"), False


def write_meta(s3: Any, bucket: str, pid: str, doc: MetaDoc, expected_etag: Optional[str]) -> str:
    """Compare-and-swap: ``IfMatch`` the ETag read at the start, or ``IfNoneMatch: *`` for the first write."""
    from botocore.exceptions import ClientError

    kw: dict = {"Bucket": bucket, "Key": meta_key(pid), "Body": doc.to_json(),
                "ContentType": "application/json", "CacheControl": "no-store"}
    if expected_etag:
        kw["IfMatch"] = expected_etag
    else:
        kw["IfNoneMatch"] = "*"
    try:
        return s3.put_object(**kw).get("ETag", "")
    except ClientError as e:
        if _code(e) in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
            raise MetaConflict(str(e)) from e
        raise
