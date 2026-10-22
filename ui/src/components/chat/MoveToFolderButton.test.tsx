/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  getAsset: vi.fn(),
  listFolders: vi.fn(),
  moveAssets: vi.fn(),
  resolveLocations: vi.fn(),
}));
vi.mock('../../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../projects/api')>()),
  getAsset: mocks.getAsset,
  listFolders: mocks.listFolders,
  moveAssets: mocks.moveAssets,
  resolveLocations: mocks.resolveLocations,
}));

import { MoveToFolderButton } from './MoveToFolderButton';

const PID = 'p1';
const AID = 'video:00000000-0000-4000-8000-000000000001';
const URL_ = 'https://cdn.example/projects/p1/sessions/s1/output/cut.mp4';

async function flush() {
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getAsset.mockResolvedValue({ ok: true, value: { assetId: AID, displayName: 'cut.mp4', kind: 'video', status: 'indexed',
    folder: 'Chats/Grip review · 2026-10-16' } });
  mocks.listFolders.mockResolvedValue({ ok: true, value: { truncated: false, folders: [
    { folder: 'Boards/Board 07', count: 1 }, { folder: 'Chats/Grip review · 2026-10-16', count: 3 }] } });
  mocks.moveAssets.mockImplementation(async (_p: string, ids: string[], folder: string) => ({ ok: true, value: { folder, moved: ids, failed: [] } }));
  mocks.resolveLocations.mockResolvedValue({ ok: true, value: { [URL_]: AID } });
});
afterEach(cleanup);

describe('MoveToFolderButton', () => {
  it('resolves a chat URL to its asset, files it in the picked folder, and Undo puts it back', async () => {
    render(<MoveToFolderButton target={{ projectId: PID, location: URL_ }} name="cut.mp4" />);
    await flush();
    expect(mocks.resolveLocations).toHaveBeenCalledWith(PID, [URL_]);
    fireEvent.click(screen.getByTestId('move-to-folder'));
    await flush();
    const picker = screen.getByTestId('folder-picker');
    // Its current folder is not offered as a destination.
    expect(within(picker).getAllByTestId('folder-picker-option').map((o) => o.textContent))
      .toEqual(['Top level', 'Boards', 'Boards / Board 07']); // chat folders wait until you type
    fireEvent.mouseDown(within(picker).getByText('Boards / Board 07'));
    await flush();
    expect(mocks.moveAssets).toHaveBeenCalledWith(PID, [AID], 'Boards/Board 07');
    const status = screen.getByTestId('move-to-folder-status');
    expect(status).toHaveTextContent('Moved to Boards/Board 07');
    fireEvent.click(within(status).getByRole('button', { name: 'Undo' }));
    await flush();
    expect(mocks.moveAssets).toHaveBeenLastCalledWith(PID, [AID], 'Chats/Grip review · 2026-10-16');
  });

  it('is absent for a URL that names no asset of this project, and for a hidden "this chat only" upload', async () => {
    mocks.resolveLocations.mockResolvedValue({ ok: true, value: { [URL_]: null } });
    const { unmount } = render(<MoveToFolderButton target={{ projectId: PID, location: URL_ }} name="x" />);
    await flush();
    expect(screen.queryByTestId('move-to-folder')).toBeNull();
    unmount();
    mocks.getAsset.mockResolvedValue({ ok: true, value: { assetId: AID, displayName: 'x', kind: 'video', status: 'stored',
      visibility: 'session' } });
    render(<MoveToFolderButton target={{ projectId: PID, assetId: AID }} name="x" />);
    await flush();
    expect(screen.queryByTestId('move-to-folder')).toBeNull();
    expect(mocks.resolveLocations).toHaveBeenCalledTimes(1); // an id needs no lookup
  });
});
