"""Single source of truth for what the harness can RENDER and how it is typed.

Historically three modules each hard-coded their own extension allowlist —
``hooks/provider.py`` (the render-hook gate), ``render_agent.py`` (the template
classifier), and the polling-worker's ``artifact_capability.py`` (the async-job
gate). They drifted (that is how ``.html`` rendered in one layer but not another),
and every new renderable type meant editing 3+ lists.

This module is the ONE place a renderable type is declared. Add a line to
``MIME_BY_EXT`` (and, for a brand-new category, teach ``render_category``) and the
renderable set, the per-category extension sets, and the mime lookups all update
together. Detection is MIME-FIRST: a producer-declared mime (M2C ``outputs[].mime``)
or the object's stored ``Content-Type`` wins; the extension map is only the
fallback when no mime is present.

Adding 3D, for example, is one line here — ``".glb": "model/gltf-binary"`` — plus
a ``model/`` category and a viewer component; no per-layer patches.
"""
from __future__ import annotations

# --- THE registry: extension (with dot) -> canonical mime. Add a type HERE. ---
MIME_BY_EXT: dict[str, str] = {
    # video
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    # audio
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    # image
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    # document (browser renders natively / in a sandboxed frame)
    ".pdf": "application/pdf", ".html": "text/html",
    # data + captions
    ".json": "application/json", ".csv": "text/csv", ".vtt": "text/vtt", ".srt": "text/plain",
}

# Mimes renderable beyond the category prefixes (documents/data have no single prefix).
RENDERABLE_MIMES: frozenset[str] = frozenset({
    "application/pdf", "application/json", "text/html", "text/csv", "text/vtt", "text/plain",
})
# Whole categories that are renderable by prefix. NB: NOT "text/" (too broad — would
# pull in arbitrary text/*; the specific text mimes we render are listed above).
RENDERABLE_MIME_PREFIXES: tuple[str, ...] = ("audio/", "video/", "image/")


def _mime_is_renderable(mime: str | None) -> bool:
    return bool(mime) and (mime in RENDERABLE_MIMES or mime.startswith(RENDERABLE_MIME_PREFIXES))


def render_category(mime: str | None) -> str:
    """Coarse render bucket for a mime — the axis the template selector keys on.

    A new category (e.g. ``model`` for 3D) is added here once.
    """
    if not mime:
        return "other"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if mime in ("application/pdf", "text/html"):
        return "document"
    if mime in ("application/json", "text/csv", "text/vtt", "text/plain"):
        return "data"
    return "other"


# --- Derived views (do not hand-edit; they follow MIME_BY_EXT) ---
def _exts_for_category(cat: str, *, with_dot: bool) -> frozenset[str]:
    return frozenset(
        (e if with_dot else e.lstrip("."))
        for e, m in MIME_BY_EXT.items()
        if render_category(m) == cat
    )


# extension sets WITHOUT leading dot (render_agent classifies on `_ext_from_url` output)
VIDEO_EXTS: frozenset[str] = _exts_for_category("video", with_dot=False)
AUDIO_EXTS: frozenset[str] = _exts_for_category("audio", with_dot=False)
IMAGE_EXTS: frozenset[str] = _exts_for_category("image", with_dot=False)
DOC_EXTS: frozenset[str] = _exts_for_category("document", with_dot=False)

# renderable extensions WITH leading dot (provider matches with `endswith`)
RENDERABLE_EXTS: tuple[str, ...] = tuple(
    e for e, m in MIME_BY_EXT.items() if _mime_is_renderable(m)
)

# no-dot ext -> mime, for callers that carry a bare extension (render_agent)
EXT_MIME_NODOT: dict[str, str] = {e.lstrip("."): m for e, m in MIME_BY_EXT.items()}

# Generic/placeholder content types that carry no real signal — an upload without an
# explicit ContentType lands as octet-stream, so these must never beat the extension.
_GENERIC_MIMES: set[str] = {"", "application/octet-stream", "binary/octet-stream"}


def mime_for(name: str | None, declared: str | None = None) -> str | None:
    """MIME for a url/filename. A SPECIFIC declared mime (producer / Content-Type)
    wins; a GENERIC octet-stream declared type is ignored in favour of the extension.

    A boto3 upload with no ContentType stores ``application/octet-stream``; treating
    that as authoritative made a ``.pdf`` classify as non-renderable and the pipeline
    fell back to a stale video. Ignoring the generic type here mirrors the
    artifact-serving proxy, which already prefers an extension guess over octet-stream.
    """
    if declared and declared.split(";", 1)[0].strip().lower() not in _GENERIC_MIMES:
        return declared
    base = (name or "").split("?", 1)[0].lower()
    for ext, mime in MIME_BY_EXT.items():
        if base.endswith(ext):
            return mime
    return declared or None


def is_renderable(name: str | None, declared: str | None = None) -> bool:
    """Whether a url/filename is renderable — mime-first, extension fallback."""
    return _mime_is_renderable(mime_for(name, declared))
