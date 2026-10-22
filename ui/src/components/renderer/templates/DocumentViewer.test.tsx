// @vitest-environment jsdom
/**
 * DocumentViewer frames a preview only when `toProxySrc` vouches for it. Session 865950fd rendered a
 * `document-viewer` manifest whose previewUrl held two links joined by a literal `\n`; the viewer
 * framed `/api/artifact?key=…` for it and the tab showed the proxy's raw 400 body
 * `{"error":"artifact key is not readable by this caller"}`. A refused preview now renders a themed
 * "Preview unavailable" state and no iframe.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('../../../auth/authFetch', () => ({ authFetch: vi.fn() }));
// The viewer must never ask for the access token: a frame/link authenticates with a view ticket.
vi.mock('../../../auth/authManager', () => ({ getAccessToken: vi.fn(() => { throw new Error('DocumentViewer must not read the access token'); }) }));
const resolveArtifactUrls = vi.hoisted(() => vi.fn());
vi.mock('../../../artifacts/artifactUrls', () => ({ resolveArtifactUrls }));

import DocumentViewer from './DocumentViewer';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';

const CF = 'https://dexamplecdn002.cloudfront.net/artifacts/865950fd/output';

function manifest(previewUrl: string | undefined): RenderManifest {
  return {
    id: 'm-1', createdAt: '2026-09-20T00:00:00Z', template: 'document-viewer',
    document: previewUrl === undefined ? {} : { previewUrl },
    tracks: [],
  } as unknown as RenderManifest;
}

function viewer(previewUrl: string | undefined) {
  return render(<DocumentViewer data={{ manifest: manifest(previewUrl) }} title="Doc" theme={theme} />);
}

describe('DocumentViewer — preview src', () => {
  it('shows "Preview unavailable" and no iframe for two links joined by a literal \\n (session 865950fd)', () => {
    const bad = `${CF}/compliance_report.json\\nhttps://dexamplecdn002.cloudfront.net/artifacts/865950fd/output/compliance_report.pdf`;
    const { container } = viewer(bad);
    expect(screen.getByTestId('document-preview-unavailable')).toHaveTextContent('Preview unavailable');
    expect(container.querySelector('iframe')).toBeNull();
    expect(screen.queryByText('Open document ↗')).toBeNull();
    expect(container.textContent).not.toContain('not readable by this caller');
  });

  it('shows "Preview unavailable" for a real newline, a backslash, and a non-artifact relative path', () => {
    for (const bad of [`${CF}/a.json\n${CF}/a.pdf`, 'artifacts/865950fd\\a.pdf', 'report.pdf']) {
      const { container, unmount } = viewer(bad);
      expect(screen.getByTestId('document-preview-unavailable')).toBeInTheDocument();
      expect(container.querySelector('iframe')).toBeNull();
      unmount();
    }
  });

  it('(b42731df) frames an HTML page with a view ticket — never the user\'s access token', async () => {
    // An agent-written page can read its own location.href; a token there is the user's identity.
    const key = 'artifacts/865950fd/output/storyboard.html';
    const ticketUrl = `/api/artifact?key=${encodeURIComponent(key)}&view=v1.1.YQ.sig`;
    resolveArtifactUrls.mockResolvedValue(new Map([[key, { url: ticketUrl, expiresAt: 1 }]]));
    const { container } = viewer(`${CF}/storyboard.html`);
    await waitFor(() => expect(container.querySelector('iframe')).toHaveAttribute('src', ticketUrl));
    expect(container.querySelector('iframe')!.getAttribute('src')).not.toContain('access_token');
    expect(resolveArtifactUrls).toHaveBeenCalledWith([key], { frame: true });
  });

  it('frames a PDF with a view ticket too — every type, and NO access token anywhere in the DOM', async () => {
    const key = 'artifacts/865950fd/output/compliance_report.pdf';
    const ticketUrl = `/api/artifact?key=${encodeURIComponent(key)}&view=v1.2.YQ.sig`;
    resolveArtifactUrls.mockReset();
    resolveArtifactUrls.mockResolvedValue(new Map([[key, { url: ticketUrl, expiresAt: 2 }]]));
    const { container } = viewer(`${CF}/compliance_report.pdf`);
    await waitFor(() => expect(container.querySelector('iframe')).toHaveAttribute('src', ticketUrl));
    expect(resolveArtifactUrls).toHaveBeenCalledWith([key], { frame: true });
    expect(screen.getByTestId('document-open-link')).toHaveAttribute('href', ticketUrl);
    expect(container.innerHTML).not.toContain('access_token');
    expect(container.innerHTML).not.toContain('tok');
    expect(screen.queryByTestId('document-preview-unavailable')).toBeNull();
  });

  it('falls back to the BARE proxy path (no credential) when the ticket cannot be minted', async () => {
    resolveArtifactUrls.mockReset();
    resolveArtifactUrls.mockResolvedValue(new Map());
    const { container } = viewer(`${CF}/compliance_report.pdf`);
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull());
    expect(container.querySelector('iframe')).toHaveAttribute(
      'src', `/api/artifact?key=${encodeURIComponent('artifacts/865950fd/output/compliance_report.pdf')}`);
    expect(container.innerHTML).not.toContain('access_token');
  });

  it('drops any query an agent put on a proxy previewUrl (a stale access_token never reaches the frame)', async () => {
    resolveArtifactUrls.mockReset();
    resolveArtifactUrls.mockRejectedValue(new Error('offline'));
    const key = 'artifacts/865950fd/output/a.pdf';
    const { container } = viewer(`/api/artifact?key=${encodeURIComponent(key)}&access_token=leaked`);
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull());
    expect(container.querySelector('iframe')).toHaveAttribute('src', `/api/artifact?key=${encodeURIComponent(key)}`);
    expect(container.innerHTML).not.toContain('leaked');
  });

  it('sandboxes every non-PDF document frame (no allow-same-origin); a PDF frame stays unsandboxed', async () => {
    resolveArtifactUrls.mockReset();
    resolveArtifactUrls.mockImplementation(async (keys: string[]) =>
      new Map(keys.map((k) => [k, { url: `/api/artifact?key=${encodeURIComponent(k)}&view=t`, expiresAt: 9 }])));
    for (const [name, sandboxed] of [['page.html', true], ['notes.xml', true], ['pic.svg', true], ['notes.txt', true], ['report.pdf', false]] as const) {
      const { container, unmount } = viewer(`${CF}/${name}`);
      await waitFor(() => expect(container.querySelector('iframe')?.getAttribute('src')).toContain('view=t'));
      const frame = container.querySelector('iframe')!;
      if (sandboxed) {
        expect(frame.getAttribute('sandbox'), name).toContain('allow-scripts');
        expect(frame.getAttribute('sandbox'), name).not.toMatch(/allow-same-origin/);
      } else {
        expect(frame.hasAttribute('sandbox'), name).toBe(false);
      }
      unmount();
    }
  });

  it('keeps the plain empty state when no preview was offered', () => {
    const { container } = viewer(undefined);
    expect(screen.getByText('No document preview available')).toBeInTheDocument();
    expect(screen.queryByTestId('document-preview-unavailable')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
  });
});

describe('DocumentViewer — several documents (session d6a560a8: three compliance PDFs, one shown)', () => {
  const SESS = 'https://dexamplecdn002.cloudfront.net/projects/p2522f8b5a58b4efb94313b54649cf03f/sessions/d6a560a8/output';
  const urls = [`${SESS}/compliance_report.pdf`, `${SESS}/nba_compliance_report.pdf`, `${SESS}/tidal_line_compliance_report.pdf`];
  const multi = {
    id: 'm-2', createdAt: '2026-10-09T13:27:00Z', template: 'document-viewer',
    document: { previewUrl: urls[0] },
    documents: urls.map((u) => ({ previewUrl: u, title: u.split('/').pop() })),
    tracks: [],
  } as unknown as RenderManifest;

  it('offers one tab per document and frames the one selected', async () => {
    const { container } = render(<DocumentViewer data={{ manifest: multi }} title="Reports" theme={theme} />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((b) => b.textContent)).toEqual(
      ['compliance_report.pdf', 'nba_compliance_report.pdf', 'tidal_line_compliance_report.pdf']);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    tabs.forEach((b) => expect(b.querySelector('svg')).not.toBeNull());
    await waitFor(() => expect(container.querySelector('iframe')?.getAttribute('src')).toContain(encodeURIComponent('compliance_report.pdf')));
    fireEvent.click(tabs[2]);
    await waitFor(() => expect(container.querySelector('iframe')?.getAttribute('src'))
      .toContain(encodeURIComponent('tidal_line_compliance_report.pdf')));
    expect(screen.getAllByRole('tab')[2]).toHaveAttribute('aria-selected', 'true');
  });

  it('shows no tabs for a single document', () => {
    viewer(`${CF}/compliance_report.pdf`);
    expect(screen.queryByTestId('document-tabs')).toBeNull();
  });
});
