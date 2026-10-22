/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, render, screen, cleanup } from '@testing-library/react';

const h = vi.hoisted(() => ({ doc: null as string | null }));
vi.mock('../../projects/htmlEmbeds', () => ({ useRefreshedHtml: () => h.doc }));

import { AssetPreviewModal } from './AssetPreviewModal';

afterEach(() => { cleanup(); h.doc = null; });

describe('AssetPreviewModal — an HTML file', () => {
  it('shows the re-linked page in a NEW, sandboxed frame — never the plain frame with sandbox added later', () => {
    const { rerender } = render(<AssetPreviewModal kind="document" src="https://s3/x.html" name="board.html" projectId="p1" onClose={() => {}} />);
    const plain = screen.getByTestId('asset-preview-document');
    // Even before the re-linked copy arrives, a non-PDF document is framed sandboxed, never same-origin.
    expect(plain.getAttribute('sandbox')).toContain('allow-scripts');
    expect(plain.getAttribute('sandbox')).not.toMatch(/allow-same-origin/);
    h.doc = '<p>page</p>';
    act(() => { rerender(<AssetPreviewModal kind="document" src="https://s3/x.html" name="board.html" projectId="p1" onClose={() => {}} />); });
    const boxed = screen.getByTestId('asset-preview-document');
    // A re-used element would be navigated by srcdoc BEFORE the sandbox flags apply (same origin as the app).
    expect(boxed).not.toBe(plain);
    expect(boxed).toHaveAttribute('sandbox', 'allow-scripts allow-popups');
    expect(boxed.getAttribute('sandbox')).not.toMatch(/allow-same-origin/);
    expect(boxed).toHaveAttribute('srcdoc', '<p>page</p>');
  });

  it('frames a PDF unsandboxed (Chromium blocks PDFs in sandboxed frames), every other document sandboxed', () => {
    render(<AssetPreviewModal kind="document" src="https://s3/r.pdf?X-Amz-Signature=s" name="r.pdf" onClose={() => {}} />);
    expect(screen.getByTestId('asset-preview-document')).not.toHaveAttribute('sandbox');
    cleanup();
    render(<AssetPreviewModal kind="document" src="https://s3/notes.xml" name="notes.xml" onClose={() => {}} />);
    expect(screen.getByTestId('asset-preview-document').getAttribute('sandbox')).not.toMatch(/allow-same-origin/);
    expect(screen.getByTestId('asset-preview-document')).toHaveAttribute('sandbox');
  });
});
