/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

const m = vi.hoisted(() => ({ listBookmarks: vi.fn(), removeBookmarks: vi.fn(), resolveCitationSource: vi.fn(), openAssetAt: vi.fn(),
  moveAssets: vi.fn() }));
vi.mock('../../projects/api', async (o) => ({ ...(await o<typeof import('../../projects/api')>()),
  listBookmarks: m.listBookmarks, removeBookmarks: m.removeBookmarks, moveAssets: m.moveAssets }));
vi.mock('../../projects/openCitation', () => ({ resolveCitationSource: m.resolveCitationSource, openAssetAt: m.openAssetAt }));

import { SavedMoments } from './SavedMoments';

const B = { bookmarkId: 'bookmark:1', assetId: 'video:a', startMs: 62000, endMs: 65000, label: 'DJI_0042 · 1:02–1:05',
  folder: 'Boards/Board 07', assetKind: 'video', assetDisplayName: 'DJI_0042.MOV' };
async function flush() { await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); }); }

beforeEach(() => {
  vi.clearAllMocks();
  m.listBookmarks.mockResolvedValue({ ok: true, value: [B] });
  m.removeBookmarks.mockResolvedValue({ ok: true, value: { removed: ['bookmark:1'], missing: [] } });
  m.resolveCitationSource.mockResolvedValue({ ok: true, url: 'https://s3/a.mov?sig' });
  m.openAssetAt.mockResolvedValue({ ok: true });
  m.moveAssets.mockResolvedValue({ ok: true, value: { folder: 'Selects', moved: ['bookmark:1'], failed: [] } });
});
afterEach(cleanup);

describe('SavedMoments', () => {
  it("lists the folder's bookmarks; a click previews at the range; Remove deletes only the bookmark", async () => {
    const onCount = vi.fn();
    render(<SavedMoments projectId="p1" folder="Boards/Board 07" sessionId="s1" refreshKey={0} onCount={onCount} />);
    await flush();
    expect(m.listBookmarks).toHaveBeenCalledWith('p1', 'Boards/Board 07');
    expect(onCount).toHaveBeenLastCalledWith(1);
    const card = screen.getByTestId('saved-moment');
    expect(card).toHaveAttribute('draggable', 'true');
    // The one card: the range and its length on the picture.
    expect(card.querySelector('[data-testid="card-badge-left"]')).toHaveTextContent('1:02–1:05');
    expect(card.querySelector('[data-testid="card-badge"]')).toHaveTextContent('3s');
    fireEvent.click(screen.getByRole('button', { name: 'Preview DJI_0042.MOV at 1:02 – 1:05' }));
    await flush();
    expect(screen.getByTestId('asset-preview-range')).toHaveTextContent('1:02 – 1:05');
    expect(screen.getByTestId('asset-preview-video')).toHaveAttribute('src', 'https://s3/a.mov?sig');
    fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
    fireEvent.click(screen.getByRole('button', { name: 'Open DJI_0042.MOV at 1:02 – 1:05 in the side panel' }));
    await flush();
    expect(m.openAssetAt).toHaveBeenCalledWith(expect.objectContaining({ startMs: 62000, range: { start: 62, end: 65 } }), 's1');
    fireEvent.click(screen.getByRole('button', { name: `More for ${B.label}` }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove' }));
    await flush();
    expect(m.removeBookmarks).toHaveBeenCalledWith('p1', ['bookmark:1']);
    expect(screen.queryByTestId('saved-moments')).toBeNull();
    expect(onCount).toHaveBeenLastCalledWith(0);
  });

  it('Cut sends the one-clip turn with the source for the agent only', async () => {
    const sent: CustomEvent[] = [];
    const on = (e: Event) => sent.push(e as CustomEvent);
    window.addEventListener('chat:send', on);
    render(<SavedMoments projectId="p1" folder="Boards/Board 07" sessionId="s1" refreshKey={0} />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Cut DJI_0042.MOV at 1:02 – 1:05 to a new tab' }));
    await flush();
    window.removeEventListener('chat:send', on);
    expect(sent).toHaveLength(1);
    expect(sent[0].detail.displayText).toContain('Clip "DJI_0042.MOV"');
    expect(sent[0].detail.displayText).not.toContain('https://');
    expect(sent[0].detail.prompt).toContain('[source: https://s3/a.mov?sig]');
    expect(sent[0].detail.sessionId).toBe('s1');
  });

  it('⋯ Move to folder… moves just the bookmark', async () => {
    const onMoved = vi.fn();
    render(<SavedMoments projectId="p1" folder="Boards/Board 07" sessionId="s1" refreshKey={0} folders={['Boards/Board 07', 'Selects']} onMoved={onMoved} />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: `More for ${B.label}` }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Move to folder…' }));
    fireEvent.mouseDown(screen.getByText('Selects'));
    await flush();
    expect(m.moveAssets).toHaveBeenCalledWith('p1', ['bookmark:1'], 'Selects');
    expect(onMoved).toHaveBeenCalled();
    expect(screen.queryByTestId('saved-moments')).toBeNull();
  });

  it('a browse-only grid offers no Move or Remove', async () => {
    render(<SavedMoments projectId="p1" folder="Boards/Board 07" sessionId="s1" refreshKey={0} folders={['Selects']} readOnly />);
    await flush();
    expect(screen.queryByRole('button', { name: `More for ${B.label}` })).toBeNull();
    expect(screen.getByTestId('saved-moment')).toHaveAttribute('draggable', 'false');
  });
});
