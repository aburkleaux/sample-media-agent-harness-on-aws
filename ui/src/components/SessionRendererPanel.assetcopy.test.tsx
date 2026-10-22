// @vitest-environment jsdom
// Session 34ca292e: a file opened from the project browser (an asset-player tab) had no "Copy source URL" -- the
// tab stores the asset REFERENCE, never a URL, so the button found nothing. Its source is minted from the ref.
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

const h = vi.hoisted(() => ({ getAssetUrls: vi.fn(), authFetch: vi.fn() }));
vi.mock('../projects/api', async (orig) => ({ ...(await orig<typeof import('../projects/api')>()), getAssetUrls: h.getAssetUrls }));
vi.mock('../auth/authFetch', () => ({ authFetch: h.authFetch }));
vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));
vi.mock('./renderer/RendererRegistry', async (orig) => {
  const actual = await orig<typeof import('./renderer/RendererRegistry')>();
  return { ...actual, lookupRenderer: () => () => null }; // the player itself is not under test
});

import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

const SIGNED = 'https://s3.example/projects/p1/image:abc/sam.png?sig=1';
const writeText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  h.getAssetUrls.mockReset().mockResolvedValue({ ok: true, value: { urls: { 'image:abc': { source: { url: SIGNED, expiresAt: 0 } } }, errors: {} } });
  h.authFetch.mockReset();
  writeText.mockClear();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
});

const assetManifest = { id: 'view-1', template: 'asset-player', ref: { project_id: 'p1', asset_id: 'image:abc', frame_s3_key: null },
  kind: 'image', displayName: 'sam.png', startMs: 0 };

function panel(entry: RenderHistoryEntry) {
  return render(<SessionRendererPanel history={[entry]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} />);
}

describe('Copy source URL on an asset-player tab', () => {
  it('a tab opened in the browser (inline manifest) offers Copy, minted from its asset reference', async () => {
    panel({ turnIndex: 0, timestamp: 1, label: 'sam.png',
      blocks: [{ template: 'manifest-router', title: 'sam.png', data: { manifest: assetManifest } }] } as unknown as RenderHistoryEntry);
    const btn = await screen.findByTestId('copy-source-url');
    expect(h.getAssetUrls).toHaveBeenCalledWith('p1', ['image:abc']);
    fireEvent.click(btn);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SIGNED));
  });

  it("a tab the agent opened (show_asset: a manifest pointer) offers it too", async () => {
    h.authFetch.mockResolvedValue(new Response(JSON.stringify(assetManifest), { status: 200 }));
    panel({ turnIndex: 0, timestamp: 1, label: 'sam.png',
      blocks: [{ template: 'manifest-router', title: 'sam.png', data: { manifestUrl: '/api/artifact?key=artifacts%2Fs%2Frender%2Fasset-1.json' } }] } as unknown as RenderHistoryEntry);
    expect(await screen.findByTestId('copy-source-url')).toBeInTheDocument();
    expect(h.getAssetUrls).toHaveBeenCalledWith('p1', ['image:abc']);
  });

  it('shows no button when the asset cannot be read', async () => {
    h.getAssetUrls.mockResolvedValue({ ok: false, kind: 'not_found' });
    panel({ turnIndex: 0, timestamp: 1, label: 'sam.png',
      blocks: [{ template: 'manifest-router', title: 'sam.png', data: { manifest: assetManifest } }] } as unknown as RenderHistoryEntry);
    await waitFor(() => expect(h.getAssetUrls).toHaveBeenCalled());
    expect(screen.queryByTestId('copy-source-url')).toBeNull();
  });
});
