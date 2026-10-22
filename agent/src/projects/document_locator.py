"""Where a document (PDF) page lives in the search index -- shared by the ingestion writer and the search reader.

A document page is indexed as a :data:`DOCUMENT_SCOPE` Index_Document whose span ENCODES its page
(``ingestion/documents.py`` writes it; ``project_search`` decodes it, and the SPA's ``shared/pageLocator.ts``
mirrors it, cross-pinned by ``agent/tests/fixtures/page_locator.json``):

    start_ms = (page - 1) * PAGE_STRIDE_MS + chunk_index      # page 1-based, chunk 0-based
    page     = start_ms // PAGE_STRIDE_MS + 1

It lives in ``projects/`` -- not ``ingestion/`` -- because ``projects/`` ships in BOTH the agent runtime zip and
the ingestion Lambda zip, while ``ingestion/`` is Lambda-only (``lambdaOnlyPackages`` in
``infra/lib/agent-infra-stack.ts``). Stdlib only.
"""
from __future__ import annotations

# Milliseconds of span reserved per document page.
PAGE_STRIDE_MS = 10_000

# The ONE scope a document's pages are indexed under. Search reads it too: a plain query ranks every video
# shot above a PDF page, so `project_search.document_recall` re-asks this scope for documents.
DOCUMENT_SCOPE = "scene"


def page_of_span(start_ms: int) -> int:
    """The 1-based page number encoded in a document hit's ``start_ms``."""
    return int(start_ms) // PAGE_STRIDE_MS + 1
