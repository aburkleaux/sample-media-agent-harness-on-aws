"""Object-key sanitization and Display_Name derivation (design Properties 4 and 5; Req 1.8-1.10, 3.4-3.6).

Both functions are pure, have no I/O, and must agree with their TypeScript twins in
``ui/server/projects`` / ``ui/src/components/projects`` (shared through the JSON fixtures the design
names). They are the only place a user-supplied file name turns into (a) the last segment of an S3
object key and (b) a human Display_Name, so every rule the design pins lives here.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = [
    "SANITIZE_MAX",
    "DISPLAY_NAME_MAX",
    "sanitize",
    "derive_display_name",
    "split_ext",
    "with_suffix",
]

# A sanitized object-key segment is at most 200 characters (design Property 4); a Display_Name is at
# most 255 code points (Req 3.4, DynamoDB-safe once folded and hashed into the DNAME guard).
SANITIZE_MAX = 200
DISPLAY_NAME_MAX = 255

# Everything outside this class becomes "_" in an object key: the key never carries a character that
# would need percent-encoding or could confuse an S3 prefix match.
_DISALLOWED_KEY_CHAR = re.compile(r"[^A-Za-z0-9._-]")

# Req 3.4: a Display_Name never contains these punctuation marks or any Unicode control/format char.
_FORBIDDEN_NAME_CHARS = frozenset("[]@#")

# A plausible file extension for extension-preserving truncation: a dot, then 1-10 of [A-Za-z0-9].
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,10}$")

_UNTITLED = {
    "video": "Untitled video",
    "image": "Untitled image",
    "audio": "Untitled audio",
    "document": "Untitled document",
}


def split_ext(name: str) -> tuple[str, str]:
    """``(stem, ext)`` where ``ext`` includes the leading dot, or ``(name, "")`` with no ext."""
    m = _EXT_RE.search(name)
    if not m:
        return name, ""
    return name[: m.start()], name[m.start() :]


def sanitize(name: object) -> str:
    """The last segment of a Project object key for ``name`` (design Property 4).

    Contains only ``[A-Za-z0-9._-]``, is at most :data:`SANITIZE_MAX` characters, keeps the original
    extension when one is present, is idempotent, and is never empty.
    """
    s = name if isinstance(name, str) else ""
    cleaned = _DISALLOWED_KEY_CHAR.sub("_", s)
    if not cleaned:
        cleaned = "_"
    if len(cleaned) <= SANITIZE_MAX:
        return cleaned
    stem, ext = split_ext(cleaned)
    if ext and len(ext) < SANITIZE_MAX:
        return (stem[: SANITIZE_MAX - len(ext)] or "_") + ext
    return cleaned[:SANITIZE_MAX]


def _strip_display(s: str) -> str:
    """Remove forbidden punctuation + control chars, collapse whitespace runs, trim the ends."""
    kept: list[str] = []
    for ch in s:
        if ch in _FORBIDDEN_NAME_CHARS:
            continue
        # Unicode "Other" categories (Cc control, Cf format, Cs surrogate, Co private, Cn unassigned).
        if unicodedata.category(ch).startswith("C"):
            continue
        kept.append(ch)
    collapsed = re.sub(r"\s+", " ", "".join(kept))
    return collapsed.strip()


def _truncate_display(s: str) -> str:
    """At most :data:`DISPLAY_NAME_MAX` code points, keeping any extension and no trailing space."""
    if len(s) <= DISPLAY_NAME_MAX:
        return s
    stem, ext = split_ext(s)
    if ext and len(ext) < DISPLAY_NAME_MAX:
        return (stem[: DISPLAY_NAME_MAX - len(ext)]).rstrip() + ext
    return s[:DISPLAY_NAME_MAX].rstrip()


def derive_display_name(name: object, kind: str) -> str:
    """The base Display_Name for a file name or key segment and ``kind`` (Req 3.4).

    Keeps the extension; contains no ``[``, ``]``, ``@``, ``#``, or control character; has no leading,
    trailing, or doubled whitespace; is at most :data:`DISPLAY_NAME_MAX` code points; and is
    ``Untitled <kind>`` when nothing else remains. The ``(n)`` allocation lives in
    :func:`with_suffix` and the registry (design §2).
    """
    s = name if isinstance(name, str) else ""
    stripped = _strip_display(s)
    truncated = _truncate_display(stripped)
    if not truncated:
        return _UNTITLED.get(kind, "Untitled")
    return truncated


def with_suffix(base: str, n: int) -> str:
    """``base`` for ``n <= 1``, else ``base (n)`` with ``base`` shortened so the whole stays <= 255.

    The suffix is appended before any extension is NOT preserved here on purpose: Req 3.5's "``name
    (2)``" form appends after the whole derived name, matching the TS twin and the design's examples
    (``clip.mp4 (2)``).
    """
    if n <= 1:
        return base
    suffix = f" ({n})"
    room = DISPLAY_NAME_MAX - len(suffix)
    if room <= 0:
        return suffix.strip()
    return base[:room].rstrip() + suffix
