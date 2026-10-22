// @vitest-environment jsdom
//
// Unit tests for useMomentUrls (Component 7, "URLs at view time"; Req 3.26, 3.27, 3.28, 3.15).
//
// The hook mints view-time presigned URLs for a MomentsBlock through the (mocked) asset-URL batcher: one
// batch on mount for the distinct asset ids, a per-card refresh machine, the no_access / load_failed phases,
// and `freshUrlFor` for bulk actions. The batcher is mocked so no network / lab-api is touched; each test
// uses a distinct project id so the hook's module-level URL cache cannot leak between tests.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

vi.mock('./assetUrlBatcher', () => ({ request: vi.fn(), MAX_IDS_PER_CALL: 100 }));

import { request } from './assetUrlBatcher';
import type { AssetUrlOutcome } from './assetUrlBatcher';
import { useMomentUrls } from './useMomentUrls';
import type { MomentItem, MomentsBlock } from '../components/renderer/types';

const mockRequest = request as unknown as ReturnType<typeof vi.fn>;

const ok = (url: string, rendition?: string): AssetUrlOutcome => ({
  ok: true,
  entry: { source: { url, expiresAt: 0 }, ...(rendition ? { rendition: { url: rendition, expiresAt: 0 } } : {}) },
});

function item(id: string, projectId: string, assetId: string, kind: MomentItem['kind']): MomentItem {
  return {
    id,
    ref: { project_id: projectId, asset_id: assetId, frame_s3_key: null },
    kind,
    display_name: id,
    start_ms: 0,
    end_ms: 1000,
    source_index: null,
    final_score: 0.5,
    text: '',
    text_truncated: false,
    highlight_terms: [],
    citation: null,
  };
}

function block(projectId: string, items: MomentItem[]): MomentsBlock {
  return { v: 1, project_id: projectId, turn_id: 't1', basis: 'cited', truncated: false, items };
}

/** Resolve the request mock to a fixed per-id outcome map. */
function resolveWith(map: Record<string, AssetUrlOutcome>) {
  mockRequest.mockImplementation(async (_pid: string, ids: string[]) => {
    const out = new Map<string, AssetUrlOutcome>();
    for (const id of ids) if (map[id]) out.set(id, map[id]);
    return out;
  });
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  mockRequest.mockReset();
});

describe('useMomentUrls', () => {
  it('a grown block (Load more) seeds and mints only the new cards, keeping the shown ones', async () => {
    resolveWith({ g1: ok('https://u/g1'), g2: { ok: false, kind: 'not_found' } as AssetUrlOutcome, g3: ok('https://u/g3') });
    const first = block('pGrow', [item('m1', 'pGrow', 'g1', 'video'), item('m2', 'pGrow', 'g2', 'video')]);
    const { result, rerender } = renderHook(({ b }) => useMomentUrls(b), { initialProps: { b: first } });
    await flush();
    await waitFor(() => expect(result.current.state('m1').phase).toBe('ready'));
    expect(result.current.state('m2').phase).toBe('no_access');
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenLastCalledWith('pGrow', ['g1', 'g2']);

    rerender({ b: block('pGrow', [...first.items, item('m3', 'pGrow', 'g3', 'video')]) });
    await flush();
    await waitFor(() => expect(result.current.state('m3').phase).toBe('ready'));
    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(mockRequest).toHaveBeenLastCalledWith('pGrow', ['g3']);
    // The no_access card was kept, not re-seeded and re-minted.
    expect(result.current.state('m2').phase).toBe('no_access');
  });

  it('mints the distinct asset ids in one batch on mount, then marks cards ready', async () => {
    resolveWith({ a1: ok('https://u/a1') });
    // Two cards of the SAME asset → one id in the batch (Req 3.26).
    const b = block('pMount', [item('m1', 'pMount', 'a1', 'video'), item('m2', 'pMount', 'a1', 'video')]);
    const { result } = renderHook(() => useMomentUrls(b));

    expect(result.current.state('m1').phase).toBe('minting');
    await flush();

    await waitFor(() => expect(result.current.state('m1').phase).toBe('ready'));
    expect(result.current.state('m2').phase).toBe('ready');
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenCalledWith('pMount', ['a1']);
    const s = result.current.state('m1');
    expect(s.phase === 'ready' && s.url).toBe('https://u/a1');
  });

  it('prefers an image rendition over the source bytes', async () => {
    resolveWith({ a1: ok('https://u/source', 'https://u/rendition') });
    const b = block('pImg', [item('m1', 'pImg', 'a1', 'image')]);
    const { result } = renderHook(() => useMomentUrls(b));
    await flush();
    await waitFor(() => expect(result.current.state('m1').phase).toBe('ready'));
    const s = result.current.state('m1');
    expect(s.phase === 'ready' && s.url).toBe('https://u/rendition');
  });

  it('maps a not_found outcome to the no_access phase (Req 3.28)', async () => {
    resolveWith({ a1: { ok: false, kind: 'not_found' } });
    const b = block('pNA', [item('m1', 'pNA', 'a1', 'video')]);
    const { result } = renderHook(() => useMomentUrls(b));
    await flush();
    await waitFor(() => expect(result.current.state('m1').phase).toBe('no_access'));
  });

  it('maps any other mint error to the load_failed phase (Req 3.15)', async () => {
    resolveWith({ a1: { ok: false, kind: 'error', code: 'x', message: 'y' } });
    const b = block('pLF', [item('m1', 'pLF', 'a1', 'video')]);
    const { result } = renderHook(() => useMomentUrls(b));
    await flush();
    await waitFor(() => expect(result.current.state('m1').phase).toBe('load_failed'));
  });

  it('freshUrlFor returns the in-hand URL for a ready card without a new request', async () => {
    resolveWith({ a1: ok('https://u/a1') });
    const b = block('pFresh', [item('m1', 'pFresh', 'a1', 'video')]);
    const { result } = renderHook(() => useMomentUrls(b));
    await flush();
    await waitFor(() => expect(result.current.state('m1').phase).toBe('ready'));

    mockRequest.mockClear();
    let url: string | null = null;
    await act(async () => {
      url = await result.current.freshUrlFor('m1');
    });
    expect(url).toBe('https://u/a1');
    expect(mockRequest).not.toHaveBeenCalled(); // the fresh URL is reused, not re-minted
  });

  it('freshUrlFor returns null for a card the Actor cannot access', async () => {
    resolveWith({ a1: { ok: false, kind: 'not_found' } });
    const b = block('pDeny', [item('m1', 'pDeny', 'a1', 'video')]);
    const { result } = renderHook(() => useMomentUrls(b));
    await flush();
    await waitFor(() => expect(result.current.state('m1').phase).toBe('no_access'));

    let url: string | null = 'x';
    await act(async () => {
      url = await result.current.freshUrlFor('m1');
    });
    expect(url).toBeNull();
  });

  it('reports minting for an id that is not part of the block', async () => {
    resolveWith({ a1: ok('https://u/a1') });
    const b = block('pUnknown', [item('m1', 'pUnknown', 'a1', 'video')]);
    const { result } = renderHook(() => useMomentUrls(b));
    await flush();
    expect(result.current.state('nope').phase).toBe('minting');
  });
});
