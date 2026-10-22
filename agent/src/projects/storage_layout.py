"""Where a session's outputs live (asset-lineage Phase 3 (a), design §9.1). Stdlib only.

Two session roots exist:

- ``artifacts/<sid>/``                 — the legacy root. Every output written before Phase 3, every
  session without a trusted project, and ALL session state (``render/*.json``, ``transcript.json``,
  ``input/``) whatever the layout.
- ``projects/<pid>/sessions/<sid>/``   — the project root. Only its ``output/`` sub-tree is written,
  and only when the env opts in (``SESSION_OUTPUT_LAYOUT=project``, set from RuntimeConfigSecret) AND
  the writer has a trusted project id.

WRITERS ask :func:`session_output_prefix`. READERS accept both roots ALWAYS, regardless of the flag
(:func:`parse_session_key`, :func:`session_roots`, :data:`SESSION_ROOT_PATTERN`), so the flag is safe to
flip either way. Existing bytes under ``artifacts/<sid>/`` are never moved: identity is the asset record.

The TypeScript twin is ``ui/src/shared/sessionLayout.ts``; the Lambda bundles (plan-chainer,
polling-worker, result-resolution) carry small copies. ALL of them are pinned by
``agent/tests/fixtures/session_layout_cases.json``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from .ids import is_project_id

__all__ = [
    "LAYOUT_ENV",
    "LEGACY_ROOT",
    "PROJECT_LAYOUT",
    "SESSION_ROOT_PATTERN",
    "SHARED_NAMESPACES",
    "SessionKey",
    "find_session_key",
    "is_project_session_output_key",
    "is_session_id",
    "layout_enabled",
    "parse_session_key",
    "project_session_root",
    "session_id_of",
    "session_output_prefix",
    "session_output_roots",
    "session_roots",
]

LAYOUT_ENV = "SESSION_OUTPUT_LAYOUT"
PROJECT_LAYOUT = "project"
LEGACY_ROOT = "artifacts/"

# Namespaces directly under artifacts/ that are not chat sessions (union of the ingestion and proxy sets).
SHARED_NAMESPACES = frozenset({"external", "durable", "imports", "uploads", "m2c"})

_SID = r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}"
_PID = r"p[0-9a-f]{32}"
_SESSION_ID_RE = re.compile(rf"^{_SID}$")

# Either session root, ending in "/". Unanchored so callers can embed it (``(?:^|/)`` + pattern). The
# session id is in group ``legacy_sid`` or ``project_sid``; use :func:`session_id_of` to read it.
SESSION_ROOT_PATTERN = (
    rf"(?:artifacts/(?P<legacy_sid>{_SID})|projects/(?P<pid>{_PID})/sessions/(?P<project_sid>{_SID}))/"
)


def is_session_id(sid: object) -> bool:
    return isinstance(sid, str) and bool(_SESSION_ID_RE.match(sid)) and sid not in SHARED_NAMESPACES


def session_id_of(match: "re.Match[str]") -> Optional[str]:
    """The session id of a :data:`SESSION_ROOT_PATTERN` match (either root), else ``None``."""
    sid = match.group("legacy_sid") or match.group("project_sid")
    return sid if is_session_id(sid) else None


def layout_enabled() -> bool:
    """True when this env writes new session outputs under the project root. Read lazily, never at import."""
    return (os.environ.get(LAYOUT_ENV) or "").strip().lower() == PROJECT_LAYOUT


def project_session_root(pid: str, sid: str) -> str:
    return f"projects/{pid}/sessions/{sid}/"


def session_output_prefix(sid: Optional[str], pid: Optional[str] = None, *,
                          artifacts_prefix: str = LEGACY_ROOT) -> str:
    """THE writer decision: where a NEW output of session ``sid`` goes.

    ``projects/<pid>/sessions/<sid>/output/`` iff the layout flag is on, ``pid`` is a project id and
    ``sid`` a session id; else ``<artifacts_prefix><sid>/output/``; with no session, ``artifacts_prefix``
    itself (flat, as before Phase 3). ``pid`` must come from a TRUSTED source (the turn's ok-access
    project, the forwarded ``producing_project_id``, a job/plan row) — never from model input.
    """
    if not sid:
        return artifacts_prefix
    if layout_enabled() and is_project_id(pid) and is_session_id(sid):
        return f"{project_session_root(pid, sid)}output/"  # type: ignore[arg-type]
    return f"{artifacts_prefix}{sid}/output/"


def session_roots(sid: str, pid: Optional[str] = None) -> list[str]:
    """Every root a session's objects may live under, legacy first. Flag-independent (readers)."""
    roots = [f"{LEGACY_ROOT}{sid}/"]
    if is_project_id(pid) and is_session_id(sid):
        roots.append(project_session_root(pid, sid))  # type: ignore[arg-type]
    return roots


def session_output_roots(sid: str, pid: Optional[str] = None) -> list[str]:
    """Every ``output/`` prefix a session's outputs may live under, legacy first. Flag-independent."""
    return [f"{r}output/" for r in session_roots(sid, pid)]


@dataclass(frozen=True)
class SessionKey:
    """A key under a session root: ``layout`` is ``"artifacts"`` or ``"project"``; ``rel`` is the path
    after ``<root>``; ``pid`` is set only for the project layout."""

    layout: str
    sid: str
    pid: Optional[str]
    rel: str


def _safe(key: object) -> bool:
    return isinstance(key, str) and bool(key) and ".." not in key and "\\" not in key and not key.startswith("/")


def parse_session_key(key: object) -> Optional[SessionKey]:
    """The session a key belongs to, under either root, else ``None``.

    ``None`` for: shared namespaces (``artifacts/external/…``), flat keys (``artifacts/<file>``), project
    ASSET keys (``projects/<pid>/<assetId>/…``), a malformed pid/sid, ``..`` or a backslash, and a bare
    root with nothing after it.
    """
    if not _safe(key):
        return None
    parts = key.split("/")  # type: ignore[union-attr]
    if parts[0] == "artifacts" and len(parts) >= 3:
        sid, rel = parts[1], "/".join(parts[2:])
        if is_session_id(sid) and rel:
            return SessionKey(layout="artifacts", sid=sid, pid=None, rel=rel)
        return None
    if parts[0] == "projects" and len(parts) >= 5 and parts[2] == "sessions":
        pid, sid, rel = parts[1], parts[3], "/".join(parts[4:])
        if is_project_id(pid) and is_session_id(sid) and rel:
            return SessionKey(layout=PROJECT_LAYOUT, sid=sid, pid=pid, rel=rel)
    return None


def is_project_session_output_key(key: object) -> bool:
    """True for a FILE under ``projects/<pid>/sessions/<sid>/output/`` — the only project-root sub-tree
    artifact routes admit (project ASSET bytes stay asset-id-gated)."""
    sk = parse_session_key(key)
    return (
        sk is not None
        and sk.layout == PROJECT_LAYOUT
        and sk.rel.startswith("output/")
        and len(sk.rel) > len("output/")
        and not sk.rel.endswith("/")
    )


_FIND_RE = re.compile(r"(?:^|/)(" + SESSION_ROOT_PATTERN + r"[^?#\s\"'<>]*)")


def find_session_key(ref: object) -> Optional[SessionKey]:
    """The FIRST session key embedded in a URL, ``s3://`` URI or bare key (either root), else ``None``.

    ``https://<cdn>/projects/<pid>/sessions/<sid>/output/a.mp4?X-Amz…`` → that key's :class:`SessionKey`.
    Percent-encoding is not decoded; the root segments never need it.
    """
    if not isinstance(ref, str) or not ref:
        return None
    for m in _FIND_RE.finditer(ref):
        sk = parse_session_key(m.group(1))
        if sk is not None:
            return sk
    return None
