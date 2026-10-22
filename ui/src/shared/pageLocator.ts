/**
 * Document (PDF) page locator — the SPA half of the span encoding (media-harness-project-ingestion
 * Requirement 16, design §21).
 *
 * A document search hit is a `scene` whose `start_ms` encodes the 1-based page number, so the UI can open
 * the PDF at the right page WITHOUT a `document` scope or a `page` column (both are M2C-owned and immutable
 * per collection). The agent writes the span (`ingestion/documents.py`); this decodes it. `PAGE_STRIDE_MS`
 * is cross-pinned with the agent constant by `agent/tests/fixtures/page_locator.json` (see pageLocator.test.ts)
 * so the two never drift.
 */

/** Milliseconds of span reserved per document page (one page occupies `[(n-1)*stride, n*stride)`). */
export const PAGE_STRIDE_MS = 10_000;

/** The 1-based page number encoded in a document `scene` hit's `start_ms` (inverse of the agent encoding). */
export function pageOfSpan(startMs: number): number {
  return Math.floor(startMs / PAGE_STRIDE_MS) + 1;
}

/**
 * A short page locator for a document hit label, e.g. `p.3`. `null` when `startMs` is not a finite,
 * non-negative number (nothing to point at).
 */
export function pageLabel(startMs: number): string | null {
  if (!Number.isFinite(startMs) || startMs < 0) return null;
  return `p.${pageOfSpan(startMs)}`;
}

/** Append `#page=N` to a presigned PDF URL so the browser's viewer jumps to the hit's page. */
export function pdfUrlWithPage(url: string, startMs: number): string {
  const page = pageOfSpan(startMs);
  return `${url.split('#', 1)[0]}#page=${page}`;
}
