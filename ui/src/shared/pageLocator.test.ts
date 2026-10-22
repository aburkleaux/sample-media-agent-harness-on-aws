import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { PAGE_STRIDE_MS, pageOfSpan, pageLabel, pdfUrlWithPage } from './pageLocator';

// The SAME fixture the agent reads (agent/tests/test_ingestion_pdf.py) so the span stride never drifts.
const FIXTURE = JSON.parse(readFileSync(
  resolve(__dirname, '../../../agent/tests/fixtures/page_locator.json'), 'utf8'));

describe('pageLocator (cross-pinned with the agent)', () => {
  it('PAGE_STRIDE_MS matches the agent constant', () => {
    expect(PAGE_STRIDE_MS).toBe(FIXTURE.pageStrideMs);
  });

  it('decodes a page number from a span start', () => {
    expect(pageOfSpan(0)).toBe(1);
    expect(pageOfSpan(PAGE_STRIDE_MS - 1)).toBe(1); // last chunk of page 1
    expect(pageOfSpan(PAGE_STRIDE_MS)).toBe(2);
    expect(pageOfSpan(6 * PAGE_STRIDE_MS + 42)).toBe(7);
  });

  it('round-trips the agent encoding', () => {
    for (const page of [1, 2, 7, 100]) {
      for (const chunk of [0, 1, 9999]) {
        expect(pageOfSpan((page - 1) * PAGE_STRIDE_MS + chunk)).toBe(page);
      }
    }
  });

  it('labels a hit and opens the PDF at the page', () => {
    expect(pageLabel(2 * PAGE_STRIDE_MS)).toBe('p.3');
    expect(pageLabel(-1)).toBeNull();
    expect(pdfUrlWithPage('https://x/doc.pdf?sig=abc', 2 * PAGE_STRIDE_MS)).toBe('https://x/doc.pdf?sig=abc#page=3');
    expect(pdfUrlWithPage('https://x/doc.pdf#page=9', 0)).toBe('https://x/doc.pdf#page=1');
  });
});
