// @vitest-environment jsdom
// Save to folder on a Renderer tab that shows ONE project file: the tab's source URL resolves to its asset, and the
// button files it with the shared folder picker. A tab whose URL names no project asset shows no button.
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const h = vi.hoisted(() => ({ getAssetUrls: vi.fn(), resolveLocations: vi.fn(), getAsset: vi.fn() }));
vi.mock('../projects/api', async (orig) => ({ ...(await orig<typeof import('../projects/api')>()),
  getAssetUrls: h.getAssetUrls, resolveLocations: h.resolveLocations, getAsset: h.getAsset }));
vi.mock('../auth/authFetch', () => ({ authFetch: vi.fn() }));
vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));
vi.mock('../projects/sessionProjectStore', () => ({ useSessionProject: () => ({ activeProjectId: 'p1' }) }));
vi.mock('./renderer/RendererRegistry', async (orig) => ({ ...(await orig<typeof import('./renderer/RendererRegistry')>()),
  lookupRenderer: () => () => null }));

import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

const SIGNED = 'https://ingest.s3.us-west-2.amazonaws.com/projects/p1/image:abc/sam.png?X-Amz-Signature=1';
const manifest = { id: 'v', template: 'asset-player', ref: { project_id: 'p1', asset_id: 'image:abc', frame_s3_key: null },
  kind: 'image', displayName: 'sam.png', startMs: 0 };
const entry = { turnIndex: 0, timestamp: 1, label: 'sam.png',
  blocks: [{ template: 'manifest-router', title: 'sam.png', data: { manifest } }] } as unknown as RenderHistoryEntry;

beforeEach(() => {
  vi.clearAllMocks();
  h.getAssetUrls.mockResolvedValue({ ok: true, value: { urls: { 'image:abc': { source: { url: SIGNED, expiresAt: 0 } } }, errors: {} } });
  h.getAsset.mockResolvedValue({ ok: true, value: { assetId: 'image:abc', displayName: 'sam.png', kind: 'image', status: 'indexed', folder: 'Boards/Board 07' } });
});

describe('Save to folder on a single-file Renderer tab', () => {
  it("offers it once the tab's source resolves to a project asset", async () => {
    h.resolveLocations.mockResolvedValue({ ok: true, value: { [SIGNED]: 'image:abc' } });
    render(<SessionRendererPanel history={[entry]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} activeSessionId="s1" />);
    expect(await screen.findByTestId('move-to-folder')).toHaveTextContent('Move');
    expect(h.resolveLocations).toHaveBeenCalledWith('p1', [SIGNED]);
    // Where it lives, beside the Move: you see the folder before you move the file out of it.
    const loc = await screen.findByTestId('file-location');
    expect(loc).toHaveAttribute('data-folder', 'Boards/Board 07');
    expect(loc).toHaveTextContent('Board 07');
  });

  it('shows none when the source is not a project asset', async () => {
    h.resolveLocations.mockResolvedValue({ ok: true, value: { [SIGNED]: null } });
    render(<SessionRendererPanel history={[entry]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} activeSessionId="s1" />);
    await waitFor(() => expect(h.resolveLocations).toHaveBeenCalled());
    expect(screen.queryByTestId('move-to-folder')).toBeNull();
    expect(screen.queryByTestId('file-location')).toBeNull();
  });
});
