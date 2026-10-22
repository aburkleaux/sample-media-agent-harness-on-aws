"""ImportSweep Lambda: once a minute, every project's import inbox (``projects/<pid>/import/``) is swept.

See :mod:`ingestion.import_inbox`. Projects are found by listing the ``projects/`` prefix one level deep (the same
listing the bucket already answers), so a project with nothing in its inbox costs one empty list call. A sweep stops
starting new projects once it nears the Lambda's time limit; the next minute picks up where the work remains (the
per-project watermark and IMPORTKEY rows make every sweep idempotent).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from projects.ids import is_project_id

from ..import_inbox import sweep_project
from ..limits import limits_from_env
from ..store import AssetStore

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Stop starting new projects with this much of the Lambda's time left.
_RESERVE_MS = 60_000


def project_ids(s3: Any, bucket: str) -> list[str]:
    """Every ``projects/<pid>/`` prefix in the bucket that names a project id."""
    out: list[str] = []
    token = None
    while True:
        kw: dict = {"Bucket": bucket, "Prefix": "projects/", "Delimiter": "/"}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for p in resp.get("CommonPrefixes", []) or []:
            pid = str(p.get("Prefix", ""))[len("projects/"):].strip("/")
            if is_project_id(pid):
                out.append(pid)
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")
        if not token:
            return out


def handler(event: Any, context: Any) -> dict:
    import boto3

    bucket = os.environ["INGEST_BUCKET"]
    store = AssetStore(boto3.resource("dynamodb").Table(os.environ["PROJECTS_TABLE"]))
    s3 = boto3.client("s3")
    limits = limits_from_env()
    summary: dict = {"projects": 0, "ingested": 0, "attention": 0, "stoppedEarly": False}
    for pid in project_ids(s3, bucket):
        if context is not None and context.get_remaining_time_in_millis() < _RESERVE_MS:
            summary["stoppedEarly"] = True
            break
        try:
            r = sweep_project(store, s3, bucket, pid, limits=limits)
        except Exception:  # noqa: BLE001 — one project's failure must not stop the others
            logger.exception("import sweep failed for %s", pid)
            continue
        summary["projects"] += 1
        summary["ingested"] += int(r.get("ingested") or 0)
        summary["attention"] += int(r.get("attention") or 0)
        if r.get("scanned"):
            logger.info("import sweep %s: %s", pid, r)
    return summary
