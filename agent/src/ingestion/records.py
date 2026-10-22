"""Document_Record serialization (Req 5.21, 5.22) and its content-addressed key (design Component 10).

One ``json.dumps(doc, ensure_ascii=True, separators=(",", ":"), sort_keys=True)`` per line, joined by ``\\n``, no
trailing line feed. ASCII escaping keeps U+0085/U+2028/U+2029 out of the bytes, because M2C's
``prepare_documents`` reads manifests with ``str.splitlines()``, which breaks on them. Parsing splits on ``\\n``
only and rejects a blank line.
"""
from __future__ import annotations

import hashlib
import json
from typing import Sequence

from projects import schema


def serialize(docs: Sequence[dict]) -> str:
    return "\n".join(json.dumps(d, ensure_ascii=True, separators=(",", ":"), sort_keys=True) for d in docs)


def parse(record: str) -> list[dict]:
    if record == "":
        return []
    out: list[dict] = []
    for i, line in enumerate(record.split("\n"), start=1):
        if not line:
            raise ValueError(f"Document_Record line {i} is empty")
        doc = json.loads(line)
        if not isinstance(doc, dict):
            raise ValueError(f"Document_Record line {i} is not a JSON object")
        out.append(doc)
    return out


def digest(record: str) -> str:
    return hashlib.sha256(record.encode("ascii")).hexdigest()


def record_key(pid: str, asset_id: str, record_digest: str) -> str:
    """``projects/<pid>/<assetId>/documents/<sha256>.jsonl``: identical builds share one object."""
    return f"{schema.project_prefix(pid)}{asset_id}/documents/{record_digest}.jsonl"
