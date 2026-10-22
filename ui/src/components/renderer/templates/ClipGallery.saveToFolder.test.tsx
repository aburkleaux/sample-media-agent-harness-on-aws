// @vitest-environment jsdom
//
// Moments → folder: pick moments in the gallery (or one card's ⋯ Save to folder…), and each becomes a BOOKMARK of its
// asset over the moment's own span. Nothing is cut or moved.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import { act, render, screen, fireEvent, within } from '@testing-library/react';
import React from 'react';
import ClipGallery from './ClipGallery';
import { theme } from '../../../styles/tokens';
import type { MomentItem, ClipGalleryData } from '../types';

const h = vi.hoisted(() => ({ createBookmarks: vi.fn(), listFolders: vi.fn() }));
vi.mock('../../../projects/api', async (orig) => ({
  ...(await orig<typeof import('../../../projects/api')>()),
  createBookmarks: h.createBookmarks,
  listFolders: h.listFolders,
}));
vi.mock('../../../projects/useMomentUrls', () => ({
  useMomentUrls: () => ({ state: () => ({ phase: 'ready', url: 'https://x', obtainedAt: 0 }), onLoadStart: vi.fn(),
    onLoadOk: vi.fn(), onLoadError: vi.fn(), freshUrlFor: vi.fn() }),
  URL_MAX_AGE_MS: 3_000_000,
}));
vi.mock('../../../projects/turnState', () => ({ useSessionTurnInProgress: () => false }));
vi.mock('../../../services/activeSession', () => ({ getActiveSessionId: () => 'sess-1' }));
vi.mock('../../../projects/sessionProjectStore', () => ({ getSessionProject: () => ({ activeProjectId: 'p1' }) }));
vi.mock('./MomentCard', () => ({
  default: (props: { moment: MomentItem; selected?: boolean; onSelectedChange?: (v: boolean) => void;
    onSaveToFolder?: (m: MomentItem) => void }) => (
    <li data-testid={`mc-${props.moment.id}`}>
      {props.onSelectedChange && <input type="checkbox" data-testid="moment-pick" checked={!!props.selected}
        onChange={(e) => props.onSelectedChange!(e.target.checked)} />}
      <button data-testid={`save-${props.moment.id}`} onClick={() => props.onSaveToFolder?.(props.moment)}>save</button>
    </li>
  ),
}));

const item = (id: string, over: Partial<MomentItem> = {}): MomentItem => ({
  id, ref: { project_id: 'p1', asset_id: `video:${id}`, frame_s3_key: null }, kind: 'video', display_name: `${id}.mp4`,
  start_ms: 12000, end_ms: 16000, source_index: null, final_score: 0.5, text: '', text_truncated: false,
  highlight_terms: [], citation: null, ...over,
});
const data = (items: MomentItem[]): ClipGalleryData =>
  ({ galleryMoments: { v: 1, project_id: 'p1', turn_id: 't1', basis: 'cited', truncated: false, items } });

async function flush() { await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); }); }

beforeEach(() => {
  vi.clearAllMocks();
  h.listFolders.mockResolvedValue({ ok: true, value: { truncated: false, folders: [{ folder: 'Boards/Board 07', count: 1 }] } });
  h.createBookmarks.mockImplementation(async (_p: string, items: unknown[], folder: string) =>
    ({ ok: true, value: { folder, created: items.map((_, i) => ({ bookmarkId: `bookmark:${i}` })), failed: [] } }));
});

describe('Moments → Save to folder', () => {
  it('saves the picked moments as bookmarks over each moment\'s span', async () => {
    render(<ClipGallery data={data([item('a'), item('b', { start_ms: 62000, end_ms: 65500 }), item('c')])} title="Moments" theme={theme} />);
    const picks = screen.getAllByTestId('moment-pick');
    fireEvent.click(picks[0]);
    fireEvent.click(picks[1]);
    const bar = screen.getByTestId('moments-selection-bar');
    expect(bar).toHaveTextContent('2 selected');
    fireEvent.click(within(bar).getByTestId('moments-save-to-folder'));
    await flush();
    const picker = screen.getByTestId('folder-picker');
    expect(screen.getByTestId('folder-picker-note')).toHaveTextContent('Saved as bookmarks: the files stay where they are.');
    fireEvent.mouseDown(within(picker).getByText('Boards / Board 07'));
    await flush();
    expect(h.createBookmarks).toHaveBeenCalledWith('p1', [
      expect.objectContaining({ assetId: 'video:a', startMs: 12000, endMs: 16000 }),
      expect.objectContaining({ assetId: 'video:b', startMs: 62000, endMs: 65500 }),
    ], 'Boards/Board 07', 'sess-1');
    expect(screen.getByTestId('moments-save-status')).toHaveTextContent('Saved 2 moments to Boards/Board 07');
    expect(screen.getAllByTestId('moment-pick').every((c) => !(c as HTMLInputElement).checked)).toBe(true);
  });

  it("one card's ⋯ Save to folder… files just that moment", async () => {
    render(<ClipGallery data={data([item('a'), item('b')])} title="Moments" theme={theme} />);
    fireEvent.click(screen.getByTestId('save-b'));
    await flush();
    expect(screen.getByTestId('moments-selection-bar')).toHaveTextContent('1 selected');
    fireEvent.mouseDown(within(screen.getByTestId('folder-picker')).getByText('Boards / Board 07'));
    await flush();
    expect(h.createBookmarks).toHaveBeenCalledWith('p1', [expect.objectContaining({ assetId: 'video:b', startMs: 12000, endMs: 16000 })],
      'Boards/Board 07', 'sess-1');
  });
});
