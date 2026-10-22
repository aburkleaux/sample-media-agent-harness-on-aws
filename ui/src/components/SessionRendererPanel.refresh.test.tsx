/**
 * @vitest-environment jsdom
 *
 * The per-tab ⟳ refresh remounts the rendered block subtree, so a manifest-router
 * tab RE-FETCHES its manifest (via authFetch, which refreshes an expired session
 * token) and the embed reloads. This pins that clicking refresh triggers a second
 * fetch — the recovery path for a stale/expired render.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));
// DocumentViewer (rendered by the document-viewer manifest) resolves a token for its
// iframe; stub it so the test focuses on the remount→re-fetch behavior.
vi.mock('../auth/authManager', () => ({ getAccessToken: vi.fn().mockResolvedValue('tok') }));

import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

const manifest = { id: 'm', createdAt: '2026-10-10T00:00:00Z', template: 'document-viewer', document: { previewUrl: '/api/artifact?key=doc.html', pageCount: null, outline: [] } };

function makeHistory(): RenderHistoryEntry[] {
  return [{
    label: 'Game',
    timestamp: 1,
    blocks: [{ template: 'manifest-router', title: 'Game', data: { manifestUrl: '/api/artifact?key=m.json' } }],
  } as unknown as RenderHistoryEntry];
}

// ManifestRouter's manifest loads (they carry an AbortSignal). The copy-source resolver also reads
// the manifest through authFetch (no signal), so counting every call would double-count.
const routerFetches = () =>
  authFetch.mock.calls.filter(([u, init]) => u === '/api/artifact?key=m.json' && (init as RequestInit | undefined)?.signal).length;

beforeEach(() => {
  authFetch.mockReset();
  authFetch.mockResolvedValue({ ok: true, status: 200, json: async () => manifest, text: async () => JSON.stringify(manifest) });
});

describe('SessionRendererPanel — render refresh', () => {
  it('shows a subtle refresh button and re-fetches the manifest when clicked (remount)', async () => {
    render(
      <SessionRendererPanel
        history={makeHistory()}
        currentIndex={0}
        onNavigate={vi.fn()}
        onClose={vi.fn()}
      />
    );
    // Initial mount fetches the manifest once.
    await waitFor(() => expect(routerFetches()).toBe(1));
    const btn = screen.getByTestId('refresh-render');
    expect(btn).toBeTruthy();
    // Clicking refresh remounts the block → a second manifest fetch.
    fireEvent.click(btn);
    await waitFor(() => expect(routerFetches()).toBeGreaterThanOrEqual(2));
  });
});
