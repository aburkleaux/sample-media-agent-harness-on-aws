"""``ingestion.pdf`` — PDF text extraction and page-aware chunking (Requirement 16).

Builds tiny real PDFs in-process (no committed binary fixtures, no new runtime dep beyond pypdf, which the
deploy bundles) so pypdf actually parses a text layer: a text PDF extracts per page with the right page
numbers; a graphics-only / empty / corrupt PDF raises ``NoText`` so the pipeline can DEGRADE rather than fail.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import documents as D  # noqa: E402
from ingestion import pdf  # noqa: E402


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A minimal, valid multi-page PDF: ``pages`` is a list of pages, each a list of text lines. A page with
    an empty line list has a content stream with no text operators (a graphics-only / scanned-style page)."""
    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    n = 3
    content_nums, page_nums = [], []
    for _ in pages:
        content_nums.append(n); n += 1
        page_nums.append(n); n += 1
    font_num = n
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    pages_obj = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objs: dict[int, bytes] = {1: catalog, 2: pages_obj}
    for lines, cn, pn in zip(pages, content_nums, page_nums):
        if lines:
            parts = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
            for ln in lines:
                parts += [f"({_esc(ln)}) Tj", "T*"]
            parts.append("ET")
        else:
            parts = ["0 0 100 100 re", "S"]  # a stroked rectangle: no text layer
        stream = "\n".join(parts).encode()
        objs[cn] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        objs[pn] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cn} 0 R "
                    f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>").encode()
    objs[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in range(1, font_num + 1):
        offsets[num] = buf.tell()
        buf.write(f"{num} 0 obj\n".encode()); buf.write(objs[num]); buf.write(b"\nendobj\n")
    xref_pos = buf.tell()
    buf.write(b"xref\n"); buf.write(f"0 {font_num + 1}\n".encode()); buf.write(b"0000000000 65535 f \n")
    for num in range(1, font_num + 1):
        buf.write(f"{offsets[num]:010d} 00000 n \n".encode())
    buf.write(b"trailer\n"); buf.write(f"<< /Size {font_num + 1} /Root 1 0 R >>\n".encode())
    buf.write(b"startxref\n"); buf.write(f"{xref_pos}\n".encode()); buf.write(b"%%EOF")
    return buf.getvalue()


# ---------------------------------------------------------------------------------------- extract
def test_extract_pages_reads_text_per_page_with_real_page_numbers():
    data = make_pdf([["Quarterly Report", "Revenue grew 12 percent."], ["Appendix", "Table of data."]])
    pages = pdf.extract_pages(data)
    assert [p.number for p in pages] == [1, 2]
    assert pages[0].heading == "Quarterly Report"
    assert "Revenue grew 12 percent." in pages[0].text
    assert pages[1].heading == "Appendix"


def test_extract_pages_skips_a_blank_page_and_keeps_the_real_page_number():
    # page 1 has no text layer (graphics only), page 2 does: the kept page keeps number 2, so the locator
    # opens the correct page even when earlier pages are scans.
    data = make_pdf([[], ["Chapter Two", "Body text here."]])
    pages = pdf.extract_pages(data)
    assert [p.number for p in pages] == [2]
    assert pages[0].heading == "Chapter Two"


@pytest.mark.parametrize("data", [
    make_pdf([[]]),            # a single graphics-only page: no text layer (a scan)
    make_pdf([[], []]),        # every page graphics-only
    b"",                       # empty file
    b"%PDF-1.4\nnot really a pdf",  # corrupt
    b"Just some bytes, not a PDF at all.",
])
def test_no_text_pdf_raises_notext(data):
    with pytest.raises(pdf.NoText):
        pdf.extract_pages(data)


def test_heading_is_trimmed():
    long = "H" * 500
    pages = pdf.extract_pages(make_pdf([[long, "body"]]))
    assert len(pages[0].heading) == pdf.HEADING_MAX


def test_total_char_cap_truncates_without_failing():
    big = ["word " * 200] * 10  # one page, lots of text
    pages = pdf.extract_pages(make_pdf([big]), max_chars=100)
    assert sum(len(p.text) for p in pages) <= 100


# ---------------------------------------------------------------------------------------- chunk
def test_chunk_pages_respects_max_and_indexes_within_a_page():
    # one long page → several chunks, each under MAX_CHUNK_CHARS, indexed 0,1,2,… within the page
    page = pdf.Page(number=1, heading="Big", text="\n\n".join(f"para {i} " + "x" * 1000 for i in range(20)))
    chunks = pdf.chunk_pages([page])
    assert len(chunks) > 1
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.page == 1 and c.heading == "Big" for c in chunks)
    assert all(len(c.text) <= pdf.MAX_CHUNK_CHARS for c in chunks)


def test_chunk_pages_keeps_each_page_separate():
    pages = [pdf.Page(1, "A", "alpha text"), pdf.Page(3, "C", "charlie text")]
    chunks = pdf.chunk_pages(pages)
    assert [(c.page, c.chunk_index) for c in chunks] == [(1, 0), (3, 0)]


def test_chunk_pages_hard_splits_a_single_huge_line():
    page = pdf.Page(1, "H", "z" * (pdf.MAX_CHUNK_CHARS * 3 + 100))
    chunks = pdf.chunk_pages([page])
    assert len(chunks) >= 3
    assert all(len(c.text) <= pdf.MAX_CHUNK_CHARS for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


# ------------------------------------------------------------------------------- end-to-end + cross-pin
def test_extract_then_chunk_then_build_document_passes_the_contract():
    from ingestion import contract

    data = make_pdf([["Intro", "First page body."], ["Methods", "Second page body."]])
    chunks = [vars(c) for c in pdf.chunk_pages(pdf.extract_pages(data))]
    res = D.build_document("document:abc123", chunks)
    assert res.documents and all(d["doc_type"] == "scene" for d in res.documents)
    dur = max(int(c["page"]) for c in chunks) * D.PAGE_STRIDE_MS
    contract.check(res.documents, "document:abc123", dur)
    # the span of each document decodes back to its page number
    for ch, doc in zip(chunks, res.documents):
        assert D.page_of_span(doc["start_ms"]) == ch["page"]


def test_page_stride_cross_pinned_with_the_spa():
    fixture = json.loads((pathlib.Path(__file__).parent / "fixtures" / "page_locator.json").read_text())
    assert D.PAGE_STRIDE_MS == fixture["pageStrideMs"]
