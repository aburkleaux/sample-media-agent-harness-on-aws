"""PDF text extraction and page-aware chunking for the document ingestion pipeline (Requirement 16).

Pure, no AWS. Pulls a text layer out of a PDF page by page with ``pypdf`` (a pure-Python universal
wheel: the arm64/py3.13 ingestion packager cannot fail to build it), then packs each page's text into
~500-1000-token chunks that each carry the page number and a best-effort heading.

A document that holds no extractable text -- a scan with no OCR, an encrypted file we cannot open, an
empty or corrupt file -- raises :class:`NoText`. The caller turns that into a DEGRADE (``stored`` with
``indexSkippedReason``), never a hard failure, mirroring the audio no-speech path.

Extension point (txt / md / docx, deliberately NOT this round): add a sibling ``extract_pages_*`` that
returns the same ``[Page]`` -- treating a plain-text file as one page, a docx as its paragraphs -- and
feed it to :func:`chunk_pages`, which is format-agnostic. Only PDF is enabled here so the page-locator
semantics stay clean.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import List, Sequence

# A page carries up to ``PAGE_STRIDE_MS`` chunks in the span encoding (ingestion.documents); keep the
# per-page chunk count well under that. With MAX_PAGE_CHARS / target this is in the dozens.
MAX_PDF_PAGES = 5000
MAX_PAGE_CHARS = 200_000          # cap one pathological page before chunking
MAX_TOTAL_CHARS = 20_000_000      # cap the whole document's extracted text (bounds index size)
TARGET_CHUNK_CHARS = 3200         # ~800 tokens at ~4 chars/token
MAX_CHUNK_CHARS = 4000            # ~1000 tokens; < documents.SCENE_TEXT_MAX so a chunk is never truncated
HEADING_MAX = 120


class NoText(Exception):
    """The document holds no extractable text layer (scan, encrypted, empty, or corrupt)."""


@dataclass(frozen=True)
class Page:
    number: int   # 1-based, the PDF's own page number (a page with no text is skipped, so numbers may gap)
    heading: str  # best-effort: the first non-empty line, trimmed (never required)
    text: str


@dataclass(frozen=True)
class Chunk:
    page: int
    chunk_index: int  # 0-based within the page
    heading: str
    text: str


def _normalize(raw: str) -> str:
    return (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _heading(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:HEADING_MAX]
    return ""


def extract_pages(data: bytes, *, max_pages: int = MAX_PDF_PAGES,
                  max_chars: int = MAX_TOTAL_CHARS) -> List[Page]:
    """The pages of ``data`` that have a text layer, in order. Raises :class:`NoText` when none do.

    An encrypted PDF is opened with the empty password (many PDFs are "encrypted" with an empty owner
    password); if that fails it is treated as having no text. A per-page extraction error contributes no
    text rather than failing the whole document.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — any parse failure is "no extractable text", not a crash
        raise NoText(f"unreadable PDF: {type(exc).__name__}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise NoText(f"encrypted PDF: {type(exc).__name__}") from exc

    pages: List[Page] = []
    total = 0
    for i, page in enumerate(reader.pages):
        if i >= max_pages or total >= max_chars:
            break
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — a single bad page yields no text, never a failure
            raw = ""
        text = _normalize(raw)
        if not text:
            continue
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS]
        if total + len(text) > max_chars:
            text = text[: max(0, max_chars - total)].strip()
        if not text:
            continue
        total += len(text)
        pages.append(Page(number=i + 1, heading=_heading(text), text=text))

    if not pages:
        raise NoText("no extractable text layer")
    return pages


def _split_text(text: str, target: int, maximum: int) -> List[str]:
    """Greedy pack of ``text`` into pieces of at most ``maximum`` chars, flushing near ``target``.

    Splits on blank lines first (paragraphs), hard-splits any block longer than ``maximum``, then packs
    blocks together until adding one would pass ``target``. ``target < maximum`` keeps every piece under
    ``maximum`` even after a block that lands between the two.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        blocks = [text.strip()]
    units: List[str] = []
    for b in blocks:
        while len(b) > maximum:
            units.append(b[:maximum])
            b = b[maximum:]
        if b:
            units.append(b)
    chunks: List[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > target:
            chunks.append(cur)
            cur = u
        else:
            cur = f"{cur}\n{u}" if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def chunk_pages(pages: Sequence[Page], *, target_chars: int = TARGET_CHUNK_CHARS,
                max_chars: int = MAX_CHUNK_CHARS) -> List[Chunk]:
    """Each page's text split into ``[Chunk]``, carrying the page number, a 0-based within-page index,
    and the page's heading. Deterministic in its inputs."""
    out: List[Chunk] = []
    for page in pages:
        for ci, piece in enumerate(_split_text(page.text, target_chars, max_chars)):
            piece = piece.strip()
            if piece:
                out.append(Chunk(page=page.number, chunk_index=ci, heading=page.heading, text=piece))
    return out
