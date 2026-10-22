"""Report the files a code cell wrote to this session's output folders, in the cell's own tool result.

Why: a sandbox cell that uploads its output with an inline ``boto3 ... upload_file(...)`` and prints ``ok``
returns NO url, so the render hook (which reads tool results) had nothing to render — the agent then wrote the
CDN link into its reply by hand and the user had to ask for the tab (session e167d3f2: a ffmpeg trim). The
sandbox can only write under this session's roots (scoped credentials, asset-lineage Phase 3 (b)), so listing
those ``output/`` prefixes before and after the cell finds what it produced, however it produced it.

Best-effort throughout: any failure leaves the tool result exactly as the tool returned it.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

#: A cell that writes more than this many files gets the first N reported (the rest are still in the folder).
MAX_REPORTED = 20
_MAX_LISTED = 2000


def _bucket() -> str:
    return os.environ.get("SKILLS_S3_BUCKET", "")


def _domain() -> str:
    return os.environ.get("ARTIFACTS_DOMAIN", "")


def _s3() -> Any:
    import boto3  # noqa: PLC0415
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def output_roots() -> list[str]:
    """This turn's session ``output/`` prefixes (both layouts), or ``[]`` when no session is bound."""
    try:
        from jobs import context as jc  # noqa: PLC0415
        from projects.storage_layout import session_output_roots  # noqa: PLC0415
        sid = jc.current_session_id()
        if not sid:
            return []
        return session_output_roots(sid, jc.trusted_project_id())
    except Exception:  # noqa: BLE001 - no session bound
        return []


def snapshot(roots: list[str], client: Any = None) -> Optional[dict[str, str]]:
    """``{key: etag}`` under ``roots``, or ``None`` when it cannot be listed (then nothing is reported)."""
    bucket = _bucket()
    if not bucket or not roots:
        return None
    s3 = client or _s3()
    seen: dict[str, str] = {}
    try:
        for root in roots:
            token = None
            while True:
                kw = {"Bucket": bucket, "Prefix": root, **({"ContinuationToken": token} if token else {})}
                page = s3.list_objects_v2(**kw)
                for obj in page.get("Contents") or []:
                    seen[obj["Key"]] = str(obj.get("ETag", ""))
                token = page.get("NextContinuationToken")
                if not token or len(seen) >= _MAX_LISTED:
                    break
    except Exception as e:  # noqa: BLE001
        logger.warning("sandbox_outputs: list failed: %s", type(e).__name__)
        return None
    return seen


def written(before: Optional[dict[str, str]], after: Optional[dict[str, str]]) -> list[str]:
    """Keys new or rewritten between the two snapshots (sorted), excluding folder markers."""
    if before is None or after is None:
        return []
    return sorted(k for k, etag in after.items() if not k.endswith("/") and before.get(k) != etag)


def url_for(key: str) -> str:
    """The artifact identifier URL for ``key`` — the form every other output tool reports."""
    domain = _domain()
    if domain:
        return f"https://{domain}/{quote(key, safe='/')}"
    return f"s3://{_bucket()}/{key}"


def annotate(result: Any, keys: list[str]) -> Any:
    """``result`` with one text block listing ``keys`` as urls appended; unchanged when there is nothing to add."""
    if not keys or not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    shown = keys[:MAX_REPORTED]
    lines = ["Files this cell wrote to the session output folder:"] + [
        f"- {url_for(k)}" + ("  (inline: show it in your reply as ![caption](url); it opens no tab)"
                              if "/output/inline/" in k else "")
        for k in shown]
    if len(keys) > len(shown):
        lines.append(f"- … and {len(keys) - len(shown)} more")
    return {**result, "content": [*result["content"], {"text": "\n".join(lines)}]}
