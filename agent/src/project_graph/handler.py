"""Lambda entry points (one image, two functions):

- ``writer_handler`` — the projects-table stream (filtered to graph-enabled META INSERT/MODIFY), or an
  async ``{"op": "sync", "projectId": ...}`` from the reader. Reserved concurrency 1.
- ``reader_handler`` — ``{"op", "projectId", "args", "caller": "proxy"}`` from the ui proxy, which has
  already authenticated the user and checked project membership.

Env: ``PROJECTS_TABLE``, ``INGEST_BUCKET``, ``AWS_REGION``, ``GRAPH_WRITER_FN`` (reader only),
``GRAPH_QUERY_ENGINE`` (``auto`` | ``cypher`` | ``arrow``).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from projects import schema as pschema
from projects.ids import is_project_id

log = logging.getLogger()
log.setLevel(logging.INFO)


def storage_options() -> dict:
    return {"region": os.environ.get("AWS_REGION", "us-west-2"), "timeout": "30s", "connect_timeout": "5s",
            "client_max_retries": "5"}


def pids_from_stream(event: dict) -> list[tuple[str, str]]:
    """``[(pid, sequenceNumber)]`` for graph-enabled META INSERT/MODIFY records, first record per pid."""
    out: dict[str, str] = {}
    for rec in event.get("Records") or []:
        d = rec.get("dynamodb") or {}
        new = d.get("NewImage") or {}
        if rec.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        if (new.get("SK") or {}).get("S") != pschema.META_SK or (new.get("graph") or {}).get("BOOL") is not True:
            continue
        pk = (new.get("PK") or {}).get("S", "")
        pid = pk[len(pschema.PROJECT_PK_PREFIX):] if pk.startswith(pschema.PROJECT_PK_PREFIX) else ""
        if is_project_id(pid) and pid not in out:
            out[pid] = d.get("SequenceNumber", "")
    return list(out.items())


_WRITER = None


def _writer_deps():
    global _WRITER
    if _WRITER is None:
        import boto3

        from .writer import WriterDeps, library_versions

        bucket = os.environ["INGEST_BUCKET"]
        _WRITER = WriterDeps(table=boto3.resource("dynamodb").Table(os.environ["PROJECTS_TABLE"]),
                             s3=boto3.client("s3"), bucket=bucket, root=f"s3://{bucket}",
                             storage_options=storage_options(), versions=library_versions())
    return _WRITER


def writer_handler(event: dict, _context: Any = None) -> dict:
    from .writer import sync_project

    deps = _writer_deps()
    if (event or {}).get("op") == "sync":
        pid = event.get("projectId")
        if not is_project_id(pid):
            return {"result": "invalid_project"}
        return sync_project(pid, deps, force=bool(event.get("force")))
    failures = []
    for pid, seq in pids_from_stream(event or {}):
        try:
            sync_project(pid, deps)
        except Exception:  # noqa: BLE001 — report and let the mapping retry this record (bisected)
            log.exception("graph sync failed for %s", pid)
            if seq:
                failures.append({"itemIdentifier": seq})
    return {"batchItemFailures": failures}


_READER = None


def _reader():
    global _READER
    if _READER is None:
        import boto3

        from .reader import GraphReader

        bucket = os.environ["INGEST_BUCKET"]
        lam = boto3.client("lambda")
        writer_fn = os.environ.get("GRAPH_WRITER_FN", "")

        def request_sync(pid: str) -> None:
            if not writer_fn:
                return
            try:
                lam.invoke(FunctionName=writer_fn, InvocationType="Event",
                           Payload=json.dumps({"op": "sync", "projectId": pid}).encode())
            except Exception:  # noqa: BLE001 — best effort; the next read asks again
                log.exception("graph: could not request a sync for %s", pid)

        _READER = GraphReader(table=boto3.resource("dynamodb").Table(os.environ["PROJECTS_TABLE"]),
                              s3=boto3.client("s3"), bucket=bucket, root=f"s3://{bucket}",
                              storage_options=storage_options(), request_sync=request_sync,
                              engine=os.environ.get("GRAPH_QUERY_ENGINE", "auto"))
    return _READER


def reader_handler(event: dict, _context: Any = None) -> dict:
    event = event or {}
    op: Optional[str] = event.get("op")
    pid = event.get("projectId")
    if op == "health":   # no AWS clients: proves the image, the runtime and the native wheels load
        import lance_graph  # noqa: F401  (the one dependency that only exists on x86_64 glibc>=2.39)

        from .writer import library_versions
        return {"state": "ok", "versions": library_versions()}
    if not is_project_id(pid):
        return {"state": "error", "error": {"code": "invalid_project", "message": "malformed project id"}}
    try:
        return _reader().handle(op or "", pid, event.get("args") or {})
    except Exception as e:  # noqa: BLE001 — the proxy maps any non-ready state to a typed error
        log.exception("graph reader %s %s failed", op, pid)
        return {"state": "error", "error": {"code": "graph_read_failed", "message": type(e).__name__}}


_TOOLS = None


def tool_handler(event: dict, context: Any = None) -> dict:
    """AgentCore Gateway Lambda target ``project-graph`` on the project Search_Gateway.

    The gateway has already verified the Project_Token and its Cedar policy has matched ``project_id`` to the
    token's claim; ``event`` is the tool's arguments and the tool name arrives in the client context.
    """
    global _TOOLS
    from .tools import GraphTools, tool_name_from_context

    tool = tool_name_from_context(context)
    if _TOOLS is None:
        _TOOLS = GraphTools(_reader())
    try:
        out = _TOOLS.call(tool or "", event or {})
    except Exception as e:  # noqa: BLE001 — a tool error must reach the model as data, not a Lambda failure
        log.exception("graph tool %s failed", tool)
        out = {"ok": False, "error": {"code": "graph_tool_failed", "message": type(e).__name__}}
    log.info("graph tool %s ok=%s", tool, out.get("ok"))
    return out
