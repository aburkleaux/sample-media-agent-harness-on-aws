"""Read several result files in one call.

Reading a fan-out's findings used to cost one tool call per file to presign and
another to fetch it. A four-job plan spent 7 presign calls plus 4 fetches — 11
model round-trips to read 4 JSON files — and every round-trip re-sends the whole
prompt. One measured turn ran 50 steps and ~8 minutes this way. This does the
same work in ONE call, resolving and fetching concurrently.

``s3://`` refs are resolved through the gateway's own download-url tool rather
than direct S3 IAM, keeping this least-privilege. The tool is discovered by
CAPABILITY — any connected gateway tool whose name carries ``download_url`` —
and its input property is read from its published ``inputSchema``, so no
federated tool name or parameter is hardcoded here. The Render Agent resolves
s3:// the same way; consolidating the two copies onto one shared helper is a
follow-up, deliberately not done while the render path is under test.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from strands import tool

from security import url_guard

logger = logging.getLogger(__name__)

# Per-file and whole-response caps. A result file is normally a few KB of JSON,
# but nothing guarantees it: an unbounded read would blow the context window and
# cost far more than the round-trips this tool saves.
MAX_BYTES_PER_FILE = 262_144
MAX_TOTAL_BYTES = 1_048_576
MAX_REFS = 25
FETCH_TIMEOUT_S = 10.0
MAX_WORKERS = 8

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+")

# A presign response can embed diagnostic links (a validation error carries
# errors.pydantic.dev). Scraping one of those instead of the object URL fails
# silently and confusingly, so they are excluded by host.
_NON_ARTIFACT_HOSTS = ("errors.pydantic.dev", "docs.", "schema.org", "json-schema.org")


def _presign_tools() -> list:
    """Connected gateway tools that turn an object reference into an http URL."""
    try:
        import mcp_manager
    except Exception:  # noqa: BLE001 - absent in unit tests / dev
        return []
    found = []
    for gw in getattr(mcp_manager, "_gateways", {}).values():
        if not getattr(gw, "is_connected", False):
            continue
        try:
            tools = gw.get_all_tools()
        except Exception:  # noqa: BLE001 - one gateway must not break the rest
            continue
        for t in tools:
            name = getattr(t, "tool_name", "") or getattr(t, "name", "") or ""
            if "download_url" in name:
                found.append((gw, t, name))
    return found


def _arg_name(tool_obj) -> Optional[str]:
    """The tool's input property, read from its published schema.

    Never guessed: a federated schema can be renamed by its owner, and guessing
    would break the call with an unhelpful validation error.
    """
    spec = getattr(tool_obj, "tool_spec", None) or {}
    schema = (spec.get("inputSchema") or {})
    if "json" in schema:
        schema = schema["json"] or {}
    props = schema.get("properties") or {}
    if not props:
        return None
    required = [r for r in (schema.get("required") or []) if r in props]
    strings = [k for k, v in props.items()
               if isinstance(v, dict) and v.get("type") in (None, "string")]
    for candidate in (required or strings):
        if candidate in props:
            return candidate
    return None


def _presign_one(gw, tool_obj, arg: str, uri: str) -> Optional[str]:
    """Resolve one s3:// URI to a fetchable http URL. None if unresolvable."""
    client = getattr(gw, "_client", None)
    name = getattr(tool_obj, "tool_name", "") or getattr(tool_obj, "name", "")
    if client is None or not name:
        return None
    try:
        result = client.call_tool_sync(f"read-files-{abs(hash(uri)) % 10**8}", name, {arg: uri})
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_result_files: presign failed for %s: %s", uri, exc)
        return None
    blob = json.dumps(result, default=str)
    candidates = [
        u.rstrip('\\"') for u in _URL_IN_TEXT.findall(blob)
        if not any(bad in u for bad in _NON_ARTIFACT_HOSTS)
    ]
    if not candidates:
        logger.warning("read_result_files: no URL in presign response for %s", uri)
        return None
    # Prefer a URL that still carries the object's filename.
    tail = uri.rsplit("/", 1)[-1].split("?", 1)[0]
    for u in candidates:
        if tail and tail in u:
            return u
    return candidates[0]


def _presign_many(uris: list[str]) -> dict:
    """Resolve s3:// URIs concurrently -> {uri: url-or-None}."""
    unique = list(dict.fromkeys(uris))
    if not unique:
        return {}
    available = _presign_tools()
    if not available:
        logger.warning("read_result_files: no gateway download-url tool available")
        return {u: None for u in unique}
    gw, tool_obj, name = available[0]
    arg = _arg_name(tool_obj)
    if not arg:
        logger.warning("read_result_files: could not read %s input arg from its schema", name)
        return {u: None for u in unique}
    if len(unique) == 1:
        return {unique[0]: _presign_one(gw, tool_obj, arg, unique[0])}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(unique))) as pool:
        return dict(zip(unique, pool.map(lambda u: _presign_one(gw, tool_obj, arg, u), unique)))


def _read_one(url: str, limit: int) -> dict:
    """Fetch one URL and parse it if it is JSON. Never raises.

    Through :mod:`security.url_guard`: the URL is model-supplied, and the body is returned
    to the model, so an internal address here would be a read primitive (SSRF).
    """
    try:
        result = url_guard.fetch(url, max_bytes=limit, timeout=FETCH_TIMEOUT_S, truncate=True)
    except url_guard.BlockedURLError as exc:
        return {"ok": False, "error": f"fetch refused: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"fetch failed: {exc}"}
    raw, truncated = result.body, result.truncated
    out: dict = {"ok": True, "bytes": len(raw), "truncated": truncated}
    text = raw.decode("utf-8", "replace")
    if not truncated:
        try:
            out["json"] = json.loads(text)
            return out
        except ValueError:
            pass
    # Not JSON, or truncated so it cannot parse — return the text as read.
    out["text"] = text
    return out


def _read_s3_direct(uri: str, limit: int) -> dict:
    """Read an ``s3://`` object straight from S3 — the fallback when the gateway can't presign it.

    read_result_files normally turns an ``s3://`` ref into an http URL via a connected gateway's
    download-url tool. In a runtime with no such gateway connected (a plan node), that presign
    returns nothing and the file was reported 'unfetchable' — even though the task/render/main
    runtime roles are GRANTED read on the M2C proxy bucket (MediaWorkflowOutputRead) and the
    skills bucket. So fall back to a direct GetObject: a diarize transcript in the proxy bucket
    stays readable regardless of gateway connectivity. Never raises; mirrors _read_one's shape.
    """
    try:
        import boto3
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            return {"ok": False, "error": "could not resolve to a fetchable URL"}
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        raw = obj["Body"].read(limit + 1) or b""
    except Exception as exc:  # noqa: BLE001 - report as unfetchable, never raise
        return {"ok": False,
                "error": f"could not resolve to a fetchable URL (direct S3 read failed: {exc})"}
    truncated = len(raw) > limit
    raw = raw[:limit]
    out: dict = {"ok": True, "bytes": len(raw), "truncated": truncated}
    text = raw.decode("utf-8", "replace")
    if not truncated:
        try:
            out["json"] = json.loads(text)
            return out
        except ValueError:
            pass
    out["text"] = text
    return out


@tool
def read_result_files(refs: list[str], max_bytes_each: int = MAX_BYTES_PER_FILE) -> str:
    """Read several result/output files in ONE call, concurrently.

    Use this to read the actual contents of files a job produced, instead of
    presigning and fetching them one at a time. Accepts a mix of ``s3://`` URIs
    (as listed in a job's outputs) and plain http(s) URLs; ``s3://`` refs are
    resolved automatically.

    Args:
        refs: Object references to read — ``s3://bucket/key`` or ``https://...``.
        max_bytes_each: Per-file read cap in bytes (default 262144). Oversized
            files come back flagged ``truncated`` as text rather than parsed.

    Returns:
        JSON with one entry per ref: ``json`` (parsed) or ``text`` on success,
        plus ``bytes``/``truncated``; ``ok: false`` and ``error`` on failure.
        Order matches the input.
    """
    if isinstance(refs, str):
        refs = [refs]
    cleaned = [r.strip() for r in (refs or []) if isinstance(r, str) and r.strip()]
    if not cleaned:
        return json.dumps({"count": 0, "files": [], "error": "no refs supplied"})

    dropped = max(0, len(cleaned) - MAX_REFS)
    cleaned = cleaned[:MAX_REFS]
    limit = max(1, min(int(max_bytes_each or MAX_BYTES_PER_FILE), MAX_BYTES_PER_FILE))

    resolved = _presign_many([r for r in cleaned if r.startswith("s3://")])

    targets: list[tuple[str, Optional[str]]] = []
    for ref in cleaned:
        targets.append((ref, resolved.get(ref) if ref.startswith("s3://") else ref))

    fetchable = [(ref, url) for ref, url in targets if url]
    results: dict[str, dict] = {}
    for ref, url in targets:
        if url:
            continue  # presigned/http — fetched below
        if ref.startswith("s3://"):
            # No gateway download-url tool connected in THIS runtime to presign it — read the
            # object directly (the runtime role can read the proxy/skills buckets). This is the
            # fix for a plan node reporting the proxy bucket 'unfetchable'.
            results[ref] = _read_s3_direct(ref, limit)
        else:
            results[ref] = {"ok": False, "error": "could not resolve to a fetchable URL"}
    if fetchable:
        if len(fetchable) == 1:
            ref, url = fetchable[0]
            results[ref] = _read_one(url, limit)
        else:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(fetchable))) as pool:
                for (ref, _u), data in zip(
                    fetchable, pool.map(lambda p: _read_one(p[1], limit), fetchable)
                ):
                    results[ref] = data

    # Whole-response budget, applied in input order so earlier refs survive.
    files = []
    total = 0
    for ref in cleaned:
        entry = dict(results.get(ref) or {"ok": False, "error": "not read"})
        size = entry.get("bytes") or 0
        if total + size > MAX_TOTAL_BYTES:
            entry = {"ok": False,
                     "error": f"omitted: response budget {MAX_TOTAL_BYTES} bytes exhausted"}
            size = 0
        total += size
        entry["ref"] = ref
        files.append(entry)

    payload: dict = {"count": len(files), "files": files}
    if dropped:
        payload["note"] = f"{dropped} ref(s) beyond the {MAX_REFS} limit were not read"
    return json.dumps(payload, default=str)
