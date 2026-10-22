"""The project-derived part of a graph: built from the Project's own DynamoDB rows, never invented.

``project_rows`` is pure (rows in, rows out), so the writer can diff the result against what is stored and
skip unchanged tables. Every row it returns has ``sample = False``; the demo sub-graph (``demo.py``) is the
only source of ``sample = True`` rows.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from . import ids
from .schema import TABLES, edge_table, node_table

Rows = dict[str, list[dict]]


def props(**kw: Any) -> str:
    """Deterministic JSON for the ``props`` column (stable bytes → stable content hash)."""
    return json.dumps({k: v for k, v in kw.items() if v is not None}, sort_keys=True, default=str,
                      separators=(",", ":"))


def node(nid: str, name: str, *, sample: bool = False, kind: str | None = None, status: str | None = None,
         **extra: Any) -> dict:
    return {"id": nid, "name": name[:200], "sample": sample, "kind": kind, "status": status, "props": props(**extra)}


def edge(rel: str, src: str, dst: str, *, sample: bool = False, **extra: Any) -> dict:
    return {"id": ids.edge_id(rel, src, dst), "src": src, "dst": dst, "sample": sample, "props": props(**extra)}


def empty_rows() -> Rows:
    return {t: [] for t in TABLES}


def project_rows(meta: Mapping[str, Any], members: Iterable[Mapping[str, Any]],
                 assets: Iterable[Mapping[str, Any]], jobs: Iterable[Mapping[str, Any]] = ()) -> Rows:
    """Project, its members (``User`` + ``OWNS``/``MEMBER_OF``), tags, every non-removed asset, and the
    asset lineage (``DERIVED_FROM`` / ``PRODUCED_BY`` / ``OUTPUT_OF`` / ``RAN_IN``)."""
    pid = str(meta["projectId"])
    out = empty_rows()
    p = ids.project_node(pid)
    out[node_table("Project")].append(node(
        p, str(meta.get("name") or pid), kind="personal" if meta.get("personal") else "shared",
        description=meta.get("description") or None, createdAt=meta.get("createdAt")))

    owner = meta.get("ownerSub")
    for m in members:
        sub = m.get("sub") or str(m.get("SK", "")).removeprefix("MEMBER#")
        if not sub:
            continue
        u = ids.user_node(sub)
        role = m.get("role") or ("owner" if sub == owner else "member")
        out[node_table("User")].append(node(u, str(m.get("display") or sub), kind=role))
        out[edge_table("MEMBER_OF")].append(edge("MEMBER_OF", u, p, role=role, addedAt=m.get("addedAt")))
        if sub == owner:
            out[edge_table("OWNS")].append(edge("OWNS", u, p))

    seen_tags: set[str] = set()
    for tag in meta.get("tags") or []:
        if not isinstance(tag, str) or not tag.strip():
            continue
        t = ids.tag_node(tag)
        if t in seen_tags:
            continue
        seen_tags.add(t)
        out[node_table("Tag")].append(node(t, tag.strip()))
        out[edge_table("TAGGED")].append(edge("TAGGED", p, t))

    live = [a for a in assets if isinstance(a.get("assetId"), str) and a.get("status") != "removed"]
    for a in live:
        aid, status = a["assetId"], a.get("status")
        n = ids.asset_node(aid)
        out[node_table("Asset")].append(node(
            n, str(a.get("displayName") or aid), kind=a.get("kind"), status=status,
            assetId=aid, contentType=a.get("contentType"), registeredAt=a.get("registeredAt"),
            addedBy=a.get("addedByDisplay")))
        out[edge_table("HAS_ASSET")].append(edge("HAS_ASSET", p, n))
    _lineage_rows(out, live, jobs)
    for rows in out.values():
        rows.sort(key=lambda r: r["id"])
    return out


def session_title(folder: Any, session_id: str) -> str:
    """A session's display name from the folder its assets live in (asset-lineage R3.3).

    ``Chats/<title> · <YYYY-MM-DD>`` (``Sessions/…`` before 2026-10; ``…/<Job label>`` for a job's outputs) is the only
    place the session's title reaches the projects table, so the graph reads it from there rather than
    inventing one or reaching into the sessions table. Anything unparseable falls back to the short id.
    """
    short = f"Session {session_id[:8]}"
    root = next((r for r in ("Chats/", "Sessions/") if isinstance(folder, str) and folder.startswith(r)), None)
    if root is None:
        return short
    first = folder[len(root):].split("/", 1)[0].strip()
    title = first.rsplit(" · ", 1)[0].strip() if " · " in first else first
    return title or short


def _session_of(asset: Mapping[str, Any]) -> str:
    """The session that produced an asset: its own ``sessionId``, else its ``producer``'s."""
    sid = asset.get("sessionId")
    if isinstance(sid, str) and sid:
        return sid
    producer = asset.get("producer")
    if isinstance(producer, Mapping):
        sid = producer.get("sessionId")
        if isinstance(sid, str) and sid:
            return sid
    return ""


def _job_of(asset: Mapping[str, Any]) -> str:
    jid = asset.get("jobId")
    if isinstance(jid, str) and jid:
        return jid
    producer = asset.get("producer")
    if isinstance(producer, Mapping):
        jid = producer.get("jobId")
        if isinstance(jid, str) and jid:
            return jid
    return ""


def _lineage_rows(out: Rows, live: list[Mapping[str, Any]], jobs: Iterable[Mapping[str, Any]]) -> None:
    """Session / Job nodes and the lineage edges of the project's live assets (R6, Phase 3).

    An edge is only written between endpoints that EXIST in this graph: a ``derivedFrom`` entry naming an
    asset that was removed (or belongs to another project) is dropped rather than left dangling, because
    every reader resolves an edge's endpoints through the node index.
    """
    asset_nodes = {ids.asset_node(a["assetId"]) for a in live}
    sessions: dict[str, str] = {}   # session id -> display name
    job_rows = {str(j.get("jobId")): j for j in jobs if isinstance(j.get("jobId"), str) and j.get("jobId")}
    job_sessions: dict[str, str] = {}

    for a in live:
        n = ids.asset_node(a["assetId"])
        sid = _session_of(a)
        if sid:
            sessions.setdefault(sid, session_title(a.get("folder"), sid))
            out[edge_table("PRODUCED_BY")].append(
                edge("PRODUCED_BY", n, ids.session_node(sid), role=a.get("role") or None))
        jid = _job_of(a)
        if jid:
            job_rows.setdefault(jid, {"jobId": jid})
            out[edge_table("OUTPUT_OF")].append(edge("OUTPUT_OF", n, ids.job_node(jid)))
            if sid:
                job_sessions.setdefault(jid, sid)
        for src in a.get("derivedFrom") or []:
            if not isinstance(src, str):
                continue
            dst = ids.asset_node(src)
            if dst in asset_nodes and dst != n:
                out[edge_table("DERIVED_FROM")].append(edge("DERIVED_FROM", n, dst))

    for jid, row in job_rows.items():
        sid = str(row.get("sessionId") or "") or job_sessions.get(jid, "")
        tool = row.get("tool")
        out[node_table("Job")].append(node(
            ids.job_node(jid), str(row.get("label") or tool or jid), kind=tool if isinstance(tool, str) else None,
            jobId=jid, tool=tool, outputPrefix=row.get("outputPrefix"), createdAt=row.get("createdAt")))
        if sid:
            sessions.setdefault(sid, f"Session {sid[:8]}")
            out[edge_table("RAN_IN")].append(edge("RAN_IN", ids.job_node(jid), ids.session_node(sid)))

    for sid, name in sessions.items():
        out[node_table("Session")].append(node(ids.session_node(sid), name, kind="session", sessionId=sid))
