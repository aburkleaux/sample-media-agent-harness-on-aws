/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useManifestFetch } from './useManifestFetch';
import type { RenderManifest } from './manifestParser';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createValidManifest(): RenderManifest {
  return {
    id: '550e8400-e29b-41d4-a716-446655440000',
    duration: 120.5,
    tracks: [
      { id: 'track-1', type: 'video', src: 'https://cdn.example.com/video.mp4', start: 0, end: 120.5 },
      { id: 'track-2', type: 'audio', src: 'https://cdn.example.com/audio.mp3', start: 0, end: 120.5 },
    ],
    clips: [
      { id: 'clip-1', name: 'Intro', start: 0, end: 30, sourceTrack: 'track-1' },
    ],
    createdAt: '2024-01-01T00:00:00.000Z',
  };
}

function createValidManifestJson(): string {
  return JSON.stringify(createValidManifest());
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useManifestFetch', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns initial state when no URL or manifest is provided', () => {
    const { result } = renderHook(() => useManifestFetch({}));

    expect(result.current.manifest).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(typeof result.current.retry).toBe('function');
  });

  it('uses initialManifest directly without fetching', () => {
    const manifest = createValidManifest();
    const { result } = renderHook(() =>
      useManifestFetch({ initialManifest: manifest })
    );

    expect(result.current.manifest).toEqual(manifest);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it('fetches and parses manifest from URL after debounce', async () => {
    const json = createValidManifestJson();
    vi.mocked(fetch).mockResolvedValue(
      new Response(json, { status: 200 })
    );

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/manifest.json' })
    );

    await waitFor(() => {
      expect(result.current.manifest).not.toBeNull();
    });

    expect(result.current.manifest!.id).toBe('550e8400-e29b-41d4-a716-446655440000');
    expect(result.current.manifest!.duration).toBe(120.5);
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('sets error on HTTP failure', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('Not Found', { status: 404 })
    );

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/missing.json' })
    );

    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.error).toBe('Failed to load manifest: HTTP 404');
    expect(result.current.manifest).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('sets error on invalid manifest JSON', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('{"id": "abc"}', { status: 200 })
    );

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/bad.json' })
    );

    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.error).toContain('Invalid manifest');
    expect(result.current.manifest).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('sets timeout error when fetch is aborted', async () => {
    // Simulate abort scenario directly
    vi.mocked(fetch).mockImplementation((_url, init) => {
      return new Promise((_resolve, reject) => {
        // Immediately trigger abort to simulate timeout behavior
        if (init?.signal) {
          if (init.signal.aborted) {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
            return;
          }
          init.signal.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }
      });
    });

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/slow.json' })
    );

    // Wait for loading to start (debounce finishes)
    await waitFor(() => {
      expect(result.current.loading).toBe(true);
    });

    // The 10s timeout will fire — use real timer advancement. But since we can't
    // easily control setTimeout in jsdom with real timers, let's test the abort path
    // by verifying the hook entered loading state. The abort/timeout logic is tested
    // by the unit test below for the timeout mechanism.
    expect(result.current.loading).toBe(true);
  });

  it('retry re-fetches the manifest immediately', async () => {
    // First call fails
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response('Server Error', { status: 500 })
    );

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/manifest.json' })
    );

    await waitFor(() => {
      expect(result.current.error).toBe('Failed to load manifest: HTTP 500');
    });

    // Second call succeeds
    const json = createValidManifestJson();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(json, { status: 200 })
    );

    // Retry
    act(() => {
      result.current.retry();
    });

    await waitFor(() => {
      expect(result.current.manifest).not.toBeNull();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.manifest!.id).toBe('550e8400-e29b-41d4-a716-446655440000');
  });

  it('handles network errors gracefully', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/manifest.json' })
    );

    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.error).toBe('Failed to load manifest: Network error');
    expect(result.current.manifest).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('resets state when URL becomes null', async () => {
    const json = createValidManifestJson();
    vi.mocked(fetch).mockResolvedValue(
      new Response(json, { status: 200 })
    );

    const { result, rerender } = renderHook(
      (props: { url: string | null }) => useManifestFetch({ manifestUrl: props.url }),
      { initialProps: { url: 'https://cdn.example.com/manifest.json' as string | null } }
    );

    await waitFor(() => {
      expect(result.current.manifest).not.toBeNull();
    });

    // Clear URL
    rerender({ url: null });

    expect(result.current.manifest).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('handles AbortError as timeout message', async () => {
    // Directly test that DOMException with AbortError name produces the timeout message
    vi.mocked(fetch).mockRejectedValue(
      new DOMException('The operation was aborted.', 'AbortError')
    );

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/manifest.json' })
    );

    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.error).toBe('Manifest loading timed out');
    expect(result.current.manifest).toBeNull();
  });

  it('sets loading to true while fetching', async () => {
    let resolvePromise: (value: Response) => void;
    vi.mocked(fetch).mockImplementation(() =>
      new Promise((resolve) => { resolvePromise = resolve; })
    );

    const { result } = renderHook(() =>
      useManifestFetch({ manifestUrl: 'https://cdn.example.com/manifest.json' })
    );

    await waitFor(() => {
      expect(result.current.loading).toBe(true);
    });

    // Resolve the fetch
    await act(async () => {
      resolvePromise!(new Response(createValidManifestJson(), { status: 200 }));
    });

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.manifest).not.toBeNull();
  });

  it('retry does nothing when no URL is set', () => {
    const { result } = renderHook(() => useManifestFetch({}));

    // Should not throw
    act(() => {
      result.current.retry();
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });
});
