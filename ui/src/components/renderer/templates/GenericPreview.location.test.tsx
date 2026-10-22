/**
 * @vitest-environment jsdom
 *
 * An image gallery says where its images are filed: once above the grid when they share a folder, on each tile
 * when they do not, and always in the lightbox caption.
 */
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({ resolveLocations: vi.fn(), getAssetUrls: vi.fn() }));
vi.mock('../../../projects/api', async (orig) => ({ ...(await orig<typeof import('../../../projects/api')>()),
  resolveLocations: h.resolveLocations, getAssetUrls: h.getAssetUrls }));
vi.mock('../../../auth/authFetch', () => ({ authFetch: vi.fn() }));

import GenericPreview from './GenericPreview';
import { ChatAssetContext } from '../../chat/InlineAssetImage';
import { _resetAssetFolders } from '../../../projects/assetFolders';
import { _resetLocationAssets } from '../../../projects/useLocationAssets';
import { theme } from '../../../styles/tokens';

const A = 'https://cf/projects/p1/image:a/frame_01.png';
const B = 'https://cf/projects/p1/image:b/frame_02.png';
const show = (urls: string[]) => render(
  <ChatAssetContext.Provider value={{ sessionId: 's', projectId: 'p1' }}>
    <GenericPreview data={{ manifest: { template: 'generic-preview', artifacts: urls.map((url) => ({ url, label: url.split('/').pop() })) } } as never}
      title="Boards" theme={theme as never} />
  </ChatAssetContext.Provider>);

function folders(map: Record<string, string>) {
  h.getAssetUrls.mockImplementation(async (_p: string, ids: string[]) => ({ ok: true, value: {
    urls: Object.fromEntries(ids.map((id) => [id, { source: { url: 'u', expiresAt: 0 }, folder: map[id] }])), errors: {} } }));
}

beforeEach(() => {
  vi.clearAllMocks(); _resetAssetFolders(); _resetLocationAssets();
  h.resolveLocations.mockResolvedValue({ ok: true, value: { [A]: 'image:a', [B]: 'image:b' } });
});

describe('gallery location', () => {
  it('one line above the grid when every image shares a folder', async () => {
    folders({ 'image:a': 'Boards/Board 07', 'image:b': 'Boards/Board 07' });
    show([A, B]);
    const shared = await screen.findByTestId('gallery-location');
    expect(shared).toHaveTextContent('All inBoards/Board 07');
    expect(within(screen.getByTestId('image-gallery')).queryByTestId('file-location')).toBeNull();
  });

  it('each tile names its own folder when they differ; the lightbox says too', async () => {
    folders({ 'image:a': 'Boards/Board 07', 'image:b': 'References' });
    show([A, B]);
    const gallery = screen.getByTestId('image-gallery');
    expect(await within(gallery).findAllByTestId('file-location')).toHaveLength(2);
    expect(screen.queryByTestId('gallery-location')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Enlarge frame_02/ }));
    expect(within(screen.getByTestId('lightbox-caption')).getByTestId('file-location')).toHaveAttribute('data-folder', 'References');
  });

  it('shows nothing for images that are not project files', async () => {
    h.resolveLocations.mockResolvedValue({ ok: true, value: { [A]: null, [B]: null } });
    show([A, B]);
    await vi.waitFor(() => expect(h.resolveLocations).toHaveBeenCalled());
    expect(screen.queryByTestId('file-location')).toBeNull();
  });
});
