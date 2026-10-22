"""Structural extraction of the SOURCE media a media job was invoked on.

One rule for every media analysis — face recognition, shot segmentation, speaker
diarization, audio-stem separation, and anything added later: the media a job runs on
is whatever ``s3://`` media-extension URI appears in its INPUT. No tool name is
special-cased.

Why it lives on its own: the source is known at the START call but the render happens
minutes later, on the ASYNC completion, in a different invocation/container. In-memory
state cannot bridge that gap, so the source is captured here at registration and stored
on the durable ``JobRecord`` (DynamoDB); the completion render then reads it back by
``job_id`` and draws the analysis over the same media (design option #1).

Kept as a leaf module (no imports from provider/registry) so both the job-tracking wrap
and the plan-node executor can capture without an import cycle.
"""
from __future__ import annotations

import re
from typing import Any

# Playable/analysable media a detection or timeline can be overlaid on. Mirrors
# hooks.provider._SOURCE_MEDIA_EXTS (the render hook's source recogniser); the two must
# stay in sync, which the tests assert.
SOURCE_MEDIA_EXTS: tuple[str, ...] = (
    ".mp4", ".mov", ".webm", ".mkv", ".m4v",
    ".wav", ".mp3", ".m4a", ".flac",
)

_S3_URI_RE = re.compile(r"s3://[^\s\"'<>)\]}\\]+", re.IGNORECASE)


def _is_media_s3(uri: str) -> bool:
    if not isinstance(uri, str) or not uri.lower().startswith("s3://"):
        return False
    clean = uri.split("?", 1)[0].rstrip(".,;").lower()
    return clean.endswith(SOURCE_MEDIA_EXTS)


def extract_source_media(value: Any) -> list[str]:
    """Every distinct ``s3://`` media URI reachable from an input value.

    Accepts a tool's input-args dict, a nested payload, or a free-text prompt (plan
    nodes embed the source URI in their prompt). Returns source media in first-seen
    order, deduped. Non-media ``s3://`` refs (e.g. an upstream ``.json`` deliverable a
    consumer node was handed) and non-``s3://`` values are ignored, so only the actual
    media-to-analyse is captured — never a produced artifact and never a config value.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(uri: str) -> None:
        cleaned = uri.rstrip(".,;")
        if _is_media_s3(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            s = v.strip()
            if s.lower().startswith("s3://") and " " not in s:
                _add(s)
            else:
                # A prompt/paragraph may EMBED one or more s3 URIs in prose.
                for match in _S3_URI_RE.findall(v):
                    _add(match)
        elif isinstance(v, dict):
            for sub in v.values():
                _walk(sub)
        elif isinstance(v, (list, tuple)):
            for sub in v:
                _walk(sub)

    _walk(value)
    return out


# The uploaded-media convention is ``artifacts/<uuid>/input/<file>`` — the segment is a
# session/actor UUID (see ui/server/uploadKey). A model that reproduces such a URI from context
# can corrupt that opaque UUID (e.g. doubling a char: ``bcddb89e4ce8`` -> ``bcddb89ee4ce8``), and
# the media job then 404s deep in the workflow (``probe_media_file`` HeadObject Not Found).
# Either session root (asset-lineage Phase 3 (a)): ``/projects/<pid>/sessions/<sid>/`` too.
_ARTIFACT_SEG_RE = re.compile(r"/(?:artifacts|projects/p[0-9a-f]{32}/sessions)/([^/]+)/", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# Fixed, non-id namespaces directly under ``artifacts/``. ``durable/`` holds the render mirror
# (``render/artifacts._durable_key``); flagging it warned on every chained analysis of a mirrored
# clip (session 764a71c0, task_agent ``_node_source_media``).
_NON_ID_ARTIFACT_SEGMENTS = frozenset({"durable"})


def malformed_media_s3uris(value: Any) -> list[str]:
    """Media SOURCE ``s3://`` URIs whose ``artifacts/<segment>/`` id is NOT a well-formed UUID.

    Correct-by-construction guard for the opaque-id corruption class: rather than let a mistyped
    actor/session id sail into a media job and 404 cryptically at ``probe_media_file``, this flags
    it at the source as a clear, correctable signal. Reuses ``extract_source_media`` so only real
    media sources are inspected (never a produced ``.json`` artifact or a config value). Returns
    the offending URIs in first-seen order; empty means every media source path is well-formed (or
    there is no media source). Pure — callers decide whether to warn, reject, or repair."""
    bad: list[str] = []
    for uri in extract_source_media(value):
        seg = _ARTIFACT_SEG_RE.search(uri)
        if seg and seg.group(1).lower() not in _NON_ID_ARTIFACT_SEGMENTS \
                and not _UUID_RE.match(seg.group(1)):
            bad.append(uri)
    return bad
