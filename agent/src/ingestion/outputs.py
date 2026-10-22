"""Reading M2C workflow outputs (Req 4.11, 4.22, 7.11; design Component 1, ``outputs.py``).

An output is selected by its documented label, never by position. A missing label or an unreadable object is a
:class:`OutputError` the reducer turns into ``failed`` naming the workflow and the label. Outputs are capped at
50 MiB; JSONL is split on ``\\n`` only.
"""
from __future__ import annotations

import json
from typing import Any

MAX_OUTPUT_BYTES = 50 * 1024 * 1024


class OutputError(Exception):
    def __init__(self, step: str, label: str, problem: str):
        self.step, self.label, self.problem = step, label, problem
        super().__init__(f"output {label!r} {problem}")


def split_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://") or "/" not in uri[5:]:
        raise ValueError(f"not an s3:// object URI: {uri[:200]}")
    bucket, key = uri[5:].split("/", 1)
    if not bucket or not key:
        raise ValueError(f"not an s3:// object URI: {uri[:200]}")
    return bucket, key


def output_uri(status: dict, label: str, step: str) -> str:
    for o in status.get("outputs") or []:
        if isinstance(o, dict) and o.get("label") == label and isinstance(o.get("uri"), str):
            return o["uri"]
    raise OutputError(step, label, "is missing from the workflow result")


def read_output(s3: Any, uri: str, step: str, label: str) -> Any:
    """GetObject + JSON/JSONL parse, with the size cap. ``s3`` is a boto3 S3 client."""
    try:
        bucket, key = split_uri(uri)
        obj = s3.get_object(Bucket=bucket, Key=key)
        size = int(obj.get("ContentLength") or 0)
        if size > MAX_OUTPUT_BYTES:
            raise OutputError(step, label, f"is {size} bytes, over the {MAX_OUTPUT_BYTES}-byte limit")
        raw = obj["Body"].read(MAX_OUTPUT_BYTES + 1)
        if len(raw) > MAX_OUTPUT_BYTES:
            raise OutputError(step, label, f"is over the {MAX_OUTPUT_BYTES}-byte limit")
        text = raw.decode("utf-8")
        if key.endswith(".jsonl"):
            return [json.loads(ln) for ln in text.split("\n") if ln.strip()]
        return json.loads(text)
    except OutputError:
        raise
    except Exception as exc:  # noqa: BLE001 — every read failure is the same outcome for the asset
        raise OutputError(step, label, f"is unreadable: {type(exc).__name__}: {exc}") from exc


def readable(s3: Any, uri: str) -> bool:
    """``HeadObject`` answers: the recorded output can be reused by a retry (Req 7.11)."""
    try:
        bucket, key = split_uri(uri)
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False
