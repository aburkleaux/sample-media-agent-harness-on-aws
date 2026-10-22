// @vitest-environment jsdom
// The folder store every "where is this file" view reads: fed by URL answers, updated by moves at once, re-asked
// after the chat agent files things.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import {
  _resetAssetFolders, invalidateAssetFolders, noteAssetFolders, noteFolderRenamed, noteMoved, peekAssetFolder,
  setAssetFolderFetcher, useAssetFolder,
} from './assetFolders';

beforeEach(() => { _resetAssetFolders(); setAssetFolderFetcher(null); });

describe('assetFolders', () => {
  it('records folders, a move, and a folder rename (sub-folders included)', () => {
    noteAssetFolders('p', { a: 'Footage/Trailers', b: 'Footage/Trailers/Tidal', c: 'Other', d: '' });
    noteMoved('p', ['c'], 'Selects');
    expect(peekAssetFolder('p', 'c')).toBe('Selects');
    noteFolderRenamed('p', 'Footage/Trailers', 'Trailers');
    expect(peekAssetFolder('p', 'a')).toBe('Trailers');
    expect(peekAssetFolder('p', 'b')).toBe('Trailers/Tidal');
    expect(peekAssetFolder('p', 'd')).toBe('');
    noteFolderRenamed('p', 'Trailers', '');
    expect(peekAssetFolder('p', 'b')).toBe('Tidal');
    expect(peekAssetFolder('other', 'a')).toBeUndefined();
  });

  it('is undefined while unknown, asks the fetcher once, and updates when the answer lands', async () => {
    const fetcher = vi.fn(async (pid: string, ids: string[]) => { noteAssetFolders(pid, { [ids[0]]: 'Boards' }); });
    setAssetFolderFetcher(fetcher);
    const { result, rerender } = renderHook(() => useAssetFolder('p', 'x'));
    await waitFor(() => expect(result.current).toBe('Boards'));
    rerender();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('a view showing a file re-asks after the chat agent moved files', async () => {
    let answer = 'Before';
    const fetcher = vi.fn(async (pid: string, ids: string[]) => { noteAssetFolders(pid, { [ids[0]]: answer }); });
    setAssetFolderFetcher(fetcher);
    const { result } = renderHook(() => useAssetFolder('p', 'x'));
    await waitFor(() => expect(result.current).toBe('Before'));
    answer = 'After';
    act(() => invalidateAssetFolders());
    await waitFor(() => expect(result.current).toBe('After'));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('does not re-ask an id the route could not resolve on every change', async () => {
    const fetcher = vi.fn(async () => undefined);
    setAssetFolderFetcher(fetcher);
    renderHook(() => useAssetFolder('p', 'gone'));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    act(() => noteAssetFolders('p', { other: 'X' }));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
