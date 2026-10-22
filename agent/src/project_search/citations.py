"""Citation_Formatter and Citation_Parser (Component 3, media-harness-search-qa-rendering).

Pure module, no I/O. Shares one grammar and one test fixture
(``infra/lib/projects/fixtures/citation-cases.json``) with the TypeScript twin
``ui/src/components/chat/citations.ts`` so the two implementations cannot drift.

Consumers (agent side): ``turn_end.finish_turn`` (Component 4),
``moments_render`` (Component 6).

Implements Requirements 2.1-2.4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from projects.document_locator import PAGE_STRIDE_MS

# The citation grammar, byte-identical to the ``pattern`` field of
# ``infra/lib/projects/fixtures/citation-cases.json`` (and to the TypeScript
# ``CITATION_PATTERN``). The fixture test asserts this equality. Kept as a raw
# string so the literal ``\u2013`` escape (en-dash) survives unchanged; Python's
# ``re`` engine interprets it when compiling, yielding a character class of the
# en-dash (U+2013) and the hyphen-minus (U+002D).
CITATION_PATTERN = (
    r"\[([^\[\]\r\n]+?) *@ *((?:\d{1,2}:[0-5]\d|[0-5]?\d):[0-5]\d) *[\u2013-] *"
    r"((?:\d{1,2}:[0-5]\d|[0-5]?\d):[0-5]\d) *\](?!\()"
)

# The PAGE citation grammar, for a document (PDF) hit: ``[report.pdf p.3]``, ``[report.pdf @ p.3]``, or a
# RANGE ``[report.pdf p.1–2]`` / ``[report.pdf pp. 1-2]`` (session 993b85b5: the model cited ranges, which the
# single-page grammar left as plain text -- no link, no Moments card).
# Byte-identical to the fixture's ``pagePattern`` (and the TypeScript ``PAGE_CITATION_PATTERN``). The
# name must be followed by whitespace or ``@`` before ``p.``, so ``[app.3]`` is not "ap" page 3. A document
# hit's span encodes its page (``ingestion/documents.py``), so a timecode would be meaningless to a reader.
PAGE_CITATION_PATTERN = (
    r"\[([^\[\]\r\n]+?)(?: *@ *| +)pp?\. ?([1-9]\d{0,3})(?: *[\u2013-] *([1-9]\d{0,3}))? *\](?!\()"
)


# Maximum Display_Name length, in characters, after trimming (Req 2.3).
NAME_MAX = 255

# The explicit trim set shared with the TypeScript side. Python ``str.strip()``
# (no args) and JS ``String.prototype.trim()`` disagree on some characters, so
# neither default is used: both languages trim exactly this set.
# U+0020, U+0009, U+00A0, U+1680, U+2000-U+200A, U+202F, U+205F, U+3000, U+FEFF.
TRIM_CHARS = (
    "\u0020\u0009\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u202f\u205f\u3000\ufeff"
)

# The separator the formatter writes between timecodes: en-dash (U+2013).
_FORMAT_DASH = "\u2013"

_PATTERN = re.compile(CITATION_PATTERN)
_PAGE_PATTERN = re.compile(PAGE_CITATION_PATTERN)
_TRIM_SET = frozenset(TRIM_CHARS)


def _trim(name: str) -> str:
    """Strip the explicit ``TRIM_CHARS`` set from both ends (not ``str.strip()``)."""
    start = 0
    end = len(name)
    while start < end and name[start] in _TRIM_SET:
        start += 1
    while end > start and name[end - 1] in _TRIM_SET:
        end -= 1
    return name[start:end]


def _timecode_to_ms(timecode: str) -> int:
    """``mm:ss`` or ``h:mm:ss`` -> whole-second milliseconds."""
    parts = timecode.split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), int(parts[1])
    else:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
    return (hours * 3600 + minutes * 60 + seconds) * 1000


def format_timecode(total_seconds: int) -> str:
    """``'mm:ss'`` when ``total_seconds < 3600``, else ``'h:mm:ss'`` with ``h``
    unpadded. ValueError when negative."""
    if total_seconds < 0:
        raise ValueError("total_seconds must not be negative")
    if total_seconds < 3600:
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def format_citation(display_name: str, start_ms: int, end_ms: int) -> str:
    """``'[<display_name> @ <floor start>-<ceil end>]'`` using integer floor on
    the start and integer ceiling on the end. ValueError when ``start_ms < 0`` or
    ``end_ms < start_ms``."""
    if start_ms < 0 or end_ms < start_ms:
        raise ValueError("require 0 <= start_ms <= end_ms")
    start_seconds = start_ms // 1000
    end_seconds = -(-end_ms // 1000)  # integer ceiling
    return (
        f"[{display_name} @ {format_timecode(start_seconds)}"
        f"{_FORMAT_DASH}{format_timecode(end_seconds)}]"
    )


def page_start_ms(page: int) -> int:
    """The span point a page citation stands for: the start of page ``page``'s stride. Every hit on that
    page has a whole-second range containing it, so verification needs no page-specific rule."""
    return (page - 1) * PAGE_STRIDE_MS


def format_page_citation(display_name: str, page: int, page_end: Optional[int] = None) -> str:
    """``'[<display_name> p.<page>]'``, or ``'[<display_name> p.<page>–<page_end>]'`` for a range.
    ValueError when ``page < 1`` or ``page_end < page``."""
    if page < 1 or (page_end is not None and page_end < page):
        raise ValueError("require 1 <= page <= page_end")
    if page_end is None or page_end == page:
        return f"[{display_name} p.{page}]"
    return f"[{display_name} p.{page}{_FORMAT_DASH}{page_end}]"


def normalize_name(name: str) -> str:
    """``name`` with ``TRIM_CHARS`` stripped from both ends, then ``str.lower()``.
    The case-insensitive comparison key of Req 2.5."""
    return _trim(name).lower()


def citation_key(display_name: str, start_ms: int, end_ms: int) -> str:
    """``f"{normalize_name(display_name)}|{start_ms}|{end_ms}"``. Identical in TS."""
    return f"{normalize_name(display_name)}|{start_ms}|{end_ms}"


def page_citation_key(display_name: str, page: int, page_end: Optional[int] = None) -> str:
    """``f"{normalize_name(display_name)}|p{page}"`` (``…|p{page}-{page_end}`` for a range) -- never equal to a
    timecode key. Identical in TS."""
    if page_end is None or page_end == page:
        return f"{normalize_name(display_name)}|p{page}"
    return f"{normalize_name(display_name)}|p{page}-{page_end}"


@dataclass(frozen=True)
class Citation:
    display_name: str          # trimmed, original case
    start_ms: int              # whole seconds x 1000
    end_ms: int                # whole seconds x 1000, >= start_ms
    span: tuple[int, int]      # [start, end) code-point offsets of the bracketed text
    text: str                  # the bracketed text exactly as written
    page: Optional[int] = None  # a PAGE citation (a document): its first page; start_ms == page_start_ms(page)
    page_end: Optional[int] = None  # its last page (== page for one page); end_ms == page_start_ms(page_end)

    @property
    def key(self) -> str:
        if self.page is not None:
            return page_citation_key(self.display_name, self.page, self.page_end)
        return citation_key(self.display_name, self.start_ms, self.end_ms)


def parse_citations(text: str) -> list[Citation]:
    """Every Citation in ``text`` -- timecode AND page form -- in order of appearance (Req 2.2, 2.3).

    A match is rejected (and its bracketed text left plain) when the name is
    empty after trimming, longer than ``NAME_MAX`` after trimming, or its start
    is later than its end.
    """
    citations: list[Citation] = []
    for match in _PAGE_PATTERN.finditer(text):
        display_name = _trim(match.group(1))
        if not display_name or len(display_name) > NAME_MAX:
            continue
        page = int(match.group(2))
        page_end = int(match.group(3)) if match.group(3) else page
        if page_end < page:
            continue
        citations.append(Citation(display_name=display_name, start_ms=page_start_ms(page),
                                  end_ms=page_start_ms(page_end), span=(match.start(), match.end()),
                                  text=match.group(0), page=page, page_end=page_end))
    for match in _PATTERN.finditer(text):
        display_name = _trim(match.group(1))
        if not display_name or len(display_name) > NAME_MAX:
            continue
        start_ms = _timecode_to_ms(match.group(2))
        end_ms = _timecode_to_ms(match.group(3))
        if start_ms > end_ms:
            continue
        citations.append(
            Citation(
                display_name=display_name,
                start_ms=start_ms,
                end_ms=end_ms,
                span=(match.start(), match.end()),
                text=match.group(0),
            )
        )
    citations.sort(key=lambda c: c.span[0])
    return citations
