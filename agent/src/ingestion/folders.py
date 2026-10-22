"""Project_Asset folders (media-harness-project-ingestion Req 1, 9, 14; design D20).

An ASSET row may carry an optional string ``folder``: a normalized relative path such as ``Footage/Day 1``.
Absent (or empty) means the project root. A folder is metadata only — the object key stays
``projects/<pid>/<assetId>/<file>`` — and folders exist only because an upload or a generated file names
one (v1 has no create/rename/move).

:func:`normalize_folder` is the single rule, twinned byte for byte by ``ui/server/projects/folders.ts``
``normalizeFolder``; both run the shared fixture ``fixtures/folder_cases.json``. The rule, in order:

1. ``None`` is the root. Anything else that is not a string is ``not_a_string``; a string longer than
   :data:`RAW_MAX` is ``too_long`` before any work is done.
2. NFC-normalize, split on ``/``, trim each segment (the :data:`_TRIM` whitespace set), and drop empty
   segments — so leading, trailing, and doubled slashes normalize away. No segment left → the root.
3. Each remaining segment, in order: ``.`` or ``..`` → ``dot_segment``; a Unicode "Other" character
   (control, format, surrogate, private-use, unassigned) → ``control_character``; one of ``[ ] @ #`` (the
   Display_Name set) → ``forbidden_character``; more than :data:`SEGMENT_MAX` code points →
   ``segment_too_long``.
4. More than :data:`DEPTH_MAX` segments → ``too_deep``; the joined path longer than :data:`FOLDER_MAX`
   code points → ``too_long``.

:func:`session_folder` is the folder the session output registrar files into (asset-lineage R3.3):
``Sessions/<session title> · <YYYY-MM-DD>`` (an M2C job's outputs one level deeper, ``…/<Job label>``),
falling back to ``Sessions/Session <sid[:8]> · <date>`` and, with no session at all, ``Sessions/<date>``.
:func:`generated_folder` is the same rule under ``Generated/`` for the legacy copies (decision 4).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date as _date
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

__all__ = [
    "SEGMENT_MAX",
    "DEPTH_MAX",
    "FOLDER_MAX",
    "RAW_MAX",
    "GENERATED_ROOT",
    "SESSIONS_ROOT",
    "LEGACY_SESSION_ROOTS",
    "FOLDER_METADATA_KEY",
    "InvalidFolder",
    "normalize_folder",
    "folder_or_error",
    "folder_path_text",
    "sanitize_segment",
    "generated_folder",
    "session_folder",
    "date_from",
]

SEGMENT_MAX = 64
DEPTH_MAX = 8
FOLDER_MAX = 255
# Refuse absurd input before normalizing it (a valid folder is at most 255 code points).
RAW_MAX = 4096
GENERATED_ROOT = "Generated"
# Where chat (session) outputs, and the M2C outputs a chat's jobs report, are filed (asset-lineage R3.3): one
# `Chats/<chat title> · <date>` folder per chat, so the agent's files stay out of the user's own folders.
SESSIONS_ROOT = "Chats"
# Earlier roots for the same files: `Sessions/` (until 2026-10) and the legacy copies' `Generated/`. Readers treat
# them as chat folders; `scripts/migrate_chat_folders.py` moves them under `Chats/`.
LEGACY_SESSION_ROOTS = ("Sessions", "Generated")
# The S3 user-metadata key an upload carries its (percent-encoded) folder in; twin of `FOLDER_METADATA_KEY`
# in `ui/server/projects/folders.ts`. boto3 reports it lower-cased without the `x-amz-meta-` prefix.
FOLDER_METADATA_KEY = "harness-folder"
# The placeholder title a session row carries before its first message renames it (``ui/server/app.ts``).
_PLACEHOLDER_TITLES = frozenset({"new session"})
_SEP = " · "

_FORBIDDEN = frozenset("[]@#")
# Unicode White_Space, spelled out so the TS twin trims exactly the same characters (Python's
# ``str.strip`` and JS ``trim`` disagree on a few, e.g. U+FEFF and U+001C-U+001F).
_TRIM = (
    "\t\n\u000b\u000c\r \u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_WS_RUN = re.compile("[" + re.escape(_TRIM) + "]+")
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class InvalidFolder(ValueError):
    """A folder that cannot be normalized; ``reason`` is the shared code (``dot_segment``, ...)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_other(ch: str) -> bool:
    return unicodedata.category(ch).startswith("C")


def normalize_folder(raw: object) -> Optional[str]:
    """The normalized folder for ``raw``, ``None`` for the root; raises :class:`InvalidFolder`."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidFolder("not_a_string")
    if len(raw) > RAW_MAX:
        raise InvalidFolder("too_long")
    segments: list[str] = []
    for part in unicodedata.normalize("NFC", raw).split("/"):
        seg = part.strip(_TRIM)
        if not seg:
            continue
        if seg in (".", ".."):
            raise InvalidFolder("dot_segment")
        if any(_is_other(ch) for ch in seg):
            raise InvalidFolder("control_character")
        if any(ch in _FORBIDDEN for ch in seg):
            raise InvalidFolder("forbidden_character")
        if len(seg) > SEGMENT_MAX:
            raise InvalidFolder("segment_too_long")
        segments.append(seg)
    if not segments:
        return None
    if len(segments) > DEPTH_MAX:
        raise InvalidFolder("too_deep")
    folder = "/".join(segments)
    if len(folder) > FOLDER_MAX:
        raise InvalidFolder("too_long")
    return folder


def folder_or_error(raw: object) -> tuple[Optional[str], Optional[str]]:
    """``(folder, None)`` or ``(None, reason)`` — :func:`normalize_folder` without the exception."""
    try:
        return normalize_folder(raw), None
    except InvalidFolder as exc:
        return None, exc.reason


def folder_path_text(row: Any) -> str:
    """``folder + "/" + displayName`` (just the name at the root): what a name lookup matches against."""
    if not isinstance(row, dict):
        return ""
    name = row.get("displayName") if isinstance(row.get("displayName"), str) else ""
    folder = row.get("folder")
    return f"{folder}/{name}" if isinstance(folder, str) and folder else name


def sanitize_segment(text: object, max_len: int = SEGMENT_MAX) -> str:
    """``text`` made into a valid folder segment, or ``""`` when nothing usable is left.

    ``/`` becomes ``-`` (it would start a new segment); whitespace runs (incl. tabs and newlines) become one
    space; forbidden ``[ ] @ #`` and every other Unicode "Other" character are dropped; the ends are trimmed
    and the result is cut to ``max_len`` code points. A bare ``.``/``..`` is ``""``.
    """
    s = unicodedata.normalize("NFC", text) if isinstance(text, str) else ""
    s = s.replace("/", "-")
    s = _WS_RUN.sub(" ", s)
    s = "".join(ch for ch in s if ch not in _FORBIDDEN and not _is_other(ch))
    s = _WS_RUN.sub(" ", s).strip(_TRIM)
    s = s[:max(0, max_len)].rstrip(_TRIM)
    return "" if s in (".", "..") else s


def date_from(value: object) -> Optional[str]:
    """``YYYY-MM-DD`` (UTC) from an ISO-8601 string, an epoch-ms number, or a ``datetime``; else ``None``."""
    try:
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).date().isoformat()
        if isinstance(value, _date):
            return value.isoformat()
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date().isoformat()
        if isinstance(value, str) and value.strip():
            s = value.strip()
            if _DATE_RE.match(s):
                return s
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return date_from(dt)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def generated_folder(*, title: object, session_id: Optional[str], day: str) -> str:
    """The folder a (legacy) generated file joins (decision 4); always a valid :func:`normalize_folder` result.

    ``Generated/<title> · <day>`` with the title sanitized and shortened so the segment fits 64 code points
    (the date is kept whole); a session with no usable title (absent, empty after sanitizing, or the
    ``New Session`` placeholder) is ``Generated/Session <first 8 of sid> · <day>``; no session at all is
    ``Generated/<day>``.
    """
    return _titled_folder(GENERATED_ROOT, title=title, session_id=session_id, day=day)


def session_folder(*, title: object, session_id: Optional[str], day: str, label: object = None) -> str:
    """The folder a session's outputs join (asset-lineage R3.3): ``Sessions/<title> · <day>``, with the same
    fallbacks as :func:`generated_folder`, and an M2C job's outputs in the subfolder ``…/<label>`` (an
    unusable label is dropped). Always a valid :func:`normalize_folder` result."""
    folder = _titled_folder(SESSIONS_ROOT, title=title, session_id=session_id, day=day)
    seg = sanitize_segment(label) if label is not None else ""
    if seg:
        nested, err = folder_or_error(f"{folder}/{seg}")
        if nested and not err:
            return nested
    return folder


def _titled_folder(root: str, *, title: object, session_id: Optional[str], day: str) -> str:
    day = day if isinstance(day, str) and _DATE_RE.match(day) else datetime.now(timezone.utc).date().isoformat()
    if not session_id:
        return f"{root}/{day}"
    suffix = f"{_SEP}{day}"
    room = SEGMENT_MAX - len(suffix)
    name = ""
    if isinstance(title, str) and title.strip(_TRIM).casefold() not in _PLACEHOLDER_TITLES:
        name = sanitize_segment(title, room)
    if not name:
        name = sanitize_segment(f"Session {session_id[:8]}", room) or "Session"
    folder, err = folder_or_error(f"{root}/{name}{suffix}")
    return folder if folder and not err else f"{root}/{day}"
