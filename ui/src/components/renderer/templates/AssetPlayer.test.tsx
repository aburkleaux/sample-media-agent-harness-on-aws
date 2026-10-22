/**
 * @vitest-environment jsdom
 *
 * AssetPlayer unit tests (Component 5, media-harness-search-qa-rendering; Req 2.8, 2.11, 2.12).
 *
 * AssetPlayer mints a presigned URL for an Asset_Reference on mount, opens a video/audio asset PAUSED
 * and seeked to the Citation's start via NleTimeline's `initialTime`, and drives the shared URL-refresh
 * machine: a stale URL re-mints on play, a load error re-mints once and then fails, and after a refresh
 * the position and play state are restored. The API, clock and cache are injected through `data.deps`;
 * NleTimeline is mocked to a stub that reports the media element so element events can be driven.
 *
 * Covers Requirements 2.8, 2.11, 2.12.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import AssetPlayer from './AssetPlayer';
import { theme } from '../../../styles/tokens';
import { URL_MAX_AGE_MS } from '../../../projects/assetUrl';
import type { ProjectsResult, AssetDetail, AssetUrlsResponse } from '../../../projects/api';
import type { OpenCitationDeps, AssetKind } from '../../../projects/openCitation';

// NleTimeline is replaced with a stub that captures its props and reports a real <video> element up
// through onMediaElement, so the tests can dispatch play/seeking/error/loadedmetadata on it.
let nleProps: { data: { initialTime?: number; onMediaElement?: (el: HTMLMediaElement | null) => void } } | null;
let mediaEl: HTMLVideoElement | null;
vi.mock('./NleTimeline', () => ({
  default: (props: { data: { onMediaElement?: (el: HTMLMediaElement | null) => void } }) => {
    nleProps = props as typeof nleProps;
    React.useEffect(() => {
      const el = document.createElement('video');
      mediaEl = el;
      props.data.onMediaElement?.(el);
      return () => props.data.onMediaElement?.(null);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return <div data-testid="nle-timeline-mock" />;
  },
}));

// Avoid pulling the real /projects client (authFetch + lab-api) — every call is injected via deps.
vi.mock('../../../projects/api', () => ({ getAsset: vi.fn(), getAssetUrls: vi.fn() }));

beforeAll(() => {
  // jsdom has no media clock; make currentTime settable and stub play().
  Object.defineProperty(window.HTMLMediaElement.prototype, 'currentTime', {
    configurable: true,
    writable: true,
    value: 0,
  });
  window.HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve());
});

beforeEach(() => {
  nleProps = null;
  mediaEl = null;
  (window.HTMLMediaElement.prototype.play as ReturnType<typeof vi.fn>).mockClear();
});

function assetOk(status: AssetDetail['status'] = 'indexed'): ProjectsResult<AssetDetail> {
  return { ok: true, value: { assetId: 'a1', displayName: 'nba.mp4', kind: 'video', status } };
}
function urlsOk(url: string): ProjectsResult<AssetUrlsResponse> {
  return { ok: true, value: { urls: { a1: { source: { url, expiresAt: 0 } } }, errors: {} } };
}

function renderPlayer(kind: AssetKind, deps: OpenCitationDeps, over: Record<string, unknown> = {}) {
  const data = {
    ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null },
    kind,
    displayName: 'nba.mp4',
    startMs: 5000,
    deps,
    ...over,
  };
  return render(<AssetPlayer data={data as never} title="nba.mp4" theme={theme} />);
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

describe('AssetPlayer', () => {
  it('hands a persisted view its viewState and change reporter (lineage spec R8.2)', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/u1')),
      now: () => 1000,
      cache: new Map(),
    };
    const onViewStateChange = vi.fn();
    const viewState = { range: { start: 2, end: 6 }, frameMs: 3000 };
    renderPlayer('video', deps, { viewState, onViewStateChange });
    await flush();
    const data = (nleProps as unknown as { data: { viewState?: unknown; onViewStateChange?: unknown } }).data;
    expect(data.viewState).toEqual(viewState);
    expect(data.onViewStateChange).toBe(onViewStateChange);
  });

  it('an Expand range is a SELECTION on the whole asset, never a trim (Moments Expand, staging 2026-10-09)', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/u1')),
      now: () => 1000,
      cache: new Map(),
    };
    // What WorkspaceView hands a tab opened from a Moment card: its In/Out as viewState.range.
    renderPlayer('video', deps, { startMs: 294700, viewState: { range: { start: 294.7, end: 312.1 } } });
    await flush();
    const data = (nleProps as unknown as {
      data: { viewState?: { range?: unknown }; initialTime?: number;
        manifest?: { tracks: { start: number; end: number; src: string }[] } };
    }).data;
    expect(data.viewState?.range).toEqual({ start: 294.7, end: 312.1 });
    expect(data.initialTime).toBe(294.7);
    // The track spans the whole asset (0/0 = measured length), so the full video loads.
    expect(data.manifest?.tracks).toHaveLength(1);
    expect(data.manifest?.tracks[0]).toMatchObject({ start: 0, end: 0, src: 'https://cdn.example/u1' });
  });

  it('tells NleTimeline not to set crossOrigin — a presigned m2c-bucket URL has no CORS (staging 2026-10-09)', async () => {
    // The per-speaker diarization .wav is m2c-source; its presigned GET from the no-CORS m2c proxy
    // bucket is refused by a crossOrigin="anonymous" <audio> (0:00/0:00 or "could not be loaded").
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/u1')),
      now: () => 1000,
      cache: new Map(),
    };
    renderPlayer('audio', deps);
    await flush();
    const data = (nleProps as unknown as { data: { mediaCrossOrigin?: boolean } }).data;
    expect(data.mediaCrossOrigin).toBe(false);
  });

  it('opens a video paused and seeks to the Citation start via NleTimeline initialTime', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/u1')),
      now: () => 1000,
      cache: new Map(),
    };
    renderPlayer('video', deps);
    await flush();

    expect(screen.getByTestId('nle-timeline-mock')).toBeInTheDocument();
    // startMs 5000 → 5 seconds, within 0.5 s of the citation start.
    expect(nleProps?.data.initialTime).toBe(5);
    // The player does not auto-play on mount.
    expect(window.HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it('forwards onInteraction so Clip / Frame / Analyze / Crop reach the agent (staging 2026-10-08)', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/u1')),
      now: () => 1000,
      cache: new Map(),
    };
    const onInteraction = vi.fn();
    const data = { ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null }, kind: 'video',
      displayName: 'nba.mp4', startMs: 0, deps };
    render(<AssetPlayer data={data as never} title="nba.mp4" theme={theme} onInteraction={onInteraction} />);
    await flush();

    const forwarded = (nleProps as unknown as { onInteraction?: (e: unknown) => void }).onInteraction;
    expect(forwarded).toBe(onInteraction);
    const ev = { type: 'action', data: { prompt: 'Clip 0:00–0:10', src: 'https://cdn.example/u1' } };
    forwarded?.(ev);
    expect(onInteraction).toHaveBeenCalledWith(ev);
  });

  it('re-mints the URL when a play starts a load with a URL older than 50 minutes', async () => {
    let nowVal = 1000;
    const getAssetUrls = vi
      .fn<() => Promise<ProjectsResult<AssetUrlsResponse>>>()
      .mockResolvedValueOnce(urlsOk('https://cdn.example/u1'))
      .mockResolvedValueOnce(urlsOk('https://cdn.example/u2'));
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls,
      now: () => nowVal,
      cache: new Map(),
    };
    renderPlayer('video', deps);
    await flush();
    expect(getAssetUrls).toHaveBeenCalledTimes(1);

    nowVal = 1000 + URL_MAX_AGE_MS + 1; // the minted URL is now stale
    await act(async () => {
      mediaEl!.dispatchEvent(new Event('play'));
    });
    await flush();

    expect(getAssetUrls).toHaveBeenCalledTimes(2); // this load's one refresh
    expect(deps.getAsset).toHaveBeenCalledTimes(1); // status is read only on the initial mint
  });

  it('refreshes once on a load error and then shows the error state on a second error', async () => {
    const getAssetUrls = vi
      .fn<() => Promise<ProjectsResult<AssetUrlsResponse>>>()
      .mockResolvedValueOnce(urlsOk('https://cdn.example/u1'))
      .mockResolvedValueOnce(urlsOk('https://cdn.example/u2'));
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls,
      now: () => 1000,
      cache: new Map(),
    };
    renderPlayer('video', deps);
    await flush();

    await act(async () => {
      mediaEl!.dispatchEvent(new Event('error'));
    });
    await flush();
    expect(getAssetUrls).toHaveBeenCalledTimes(2); // one refresh spent

    await act(async () => {
      mediaEl!.dispatchEvent(new Event('error'));
    });
    await flush();

    expect(screen.getByTestId('asset-player-error')).toBeInTheDocument();
    expect(getAssetUrls).toHaveBeenCalledTimes(2); // no second refresh — the budget was spent
  });

  it('restores the position and play state after a refresh', async () => {
    let nowVal = 1000;
    const getAssetUrls = vi
      .fn<() => Promise<ProjectsResult<AssetUrlsResponse>>>()
      .mockResolvedValueOnce(urlsOk('https://cdn.example/u1'))
      .mockResolvedValueOnce(urlsOk('https://cdn.example/u2'));
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls,
      now: () => nowVal,
      cache: new Map(),
    };
    renderPlayer('video', deps);
    await flush();

    (mediaEl as unknown as { currentTime: number }).currentTime = 12;
    nowVal = 1000 + URL_MAX_AGE_MS + 1; // stale → play triggers a refresh
    await act(async () => {
      mediaEl!.dispatchEvent(new Event('play'));
    });
    await flush();

    await act(async () => {
      mediaEl!.dispatchEvent(new Event('loadedmetadata'));
    });
    await flush();

    expect((mediaEl as unknown as { currentTime: number }).currentTime).toBe(12);
    expect(window.HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it('renders the image branch as an <img> with the minted source', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => ({ ok: true as const, value: { assetId: 'a1', displayName: 'poster.jpg', kind: 'image' as const, status: 'indexed' as const } })),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/poster.jpg')),
      now: () => 1000,
      cache: new Map(),
    };
    renderPlayer('image', deps, { displayName: 'poster.jpg' });
    await flush();

    const img = screen.getByTestId('asset-player-image') as HTMLImageElement;
    expect(img).toBeInTheDocument();
    expect(img.getAttribute('src')).toBe('https://cdn.example/poster.jpg');
    expect(img).toHaveAttribute('alt', 'poster.jpg');
    // 4b: a single image tab is capped at 62vh like the other previews, not full-height
    // (session aa9e30ba, template asset-player/kind image, a tall generated PNG filled the tab).
    expect(img.style.maxHeight).toBe('62vh');
    expect(img.style.objectFit).toBe('contain');
  });

  it('shows the no-access message when the asset status read is not_found', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => ({ ok: false as const, kind: 'not_found' as const })),
      getAssetUrls: vi.fn(async () => urlsOk('https://cdn.example/u1')),
      now: () => 1000,
      cache: new Map(),
    };
    renderPlayer('video', deps);
    await flush();
    expect(screen.getByTestId('asset-player-error')).toHaveTextContent('You no longer have access to this project.');
  });
});

// A document (PDF page) Expand / page citation opens HERE, inside the harness, at the hit's page.
describe('AssetPlayer — documents', () => {
  it('embeds the PDF at the page its span encodes, with an Open-in-new-tab link', async () => {
    const deps: OpenCitationDeps = {
      getAsset: vi.fn(async () => assetOk()),
      getAssetUrls: vi.fn(async () => urlsOk('https://s3.example/report.pdf?X-Amz-Signature=s')),
      now: () => 1000,
      cache: new Map(),
    };
    renderPlayer('document', deps, { displayName: 'report.pdf', startMs: 2 * 10_000 + 3 });
    await flush();
    const frame = (await screen.findByTestId('asset-player-document')).querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('https://s3.example/report.pdf?X-Amz-Signature=s#page=3');
    expect(frame.getAttribute('title')).toBe('report.pdf, page 3');
    expect(screen.getByTestId('asset-player-document-page')).toHaveTextContent('Page 3');
    const link = screen.getByTestId('asset-player-document-newtab');
    expect(link).toHaveAttribute('href', 'https://s3.example/report.pdf?X-Amz-Signature=s#page=3');
    expect(link).toHaveAttribute('target', '_blank');
    expect(screen.queryByTestId('asset-player-timeline')).toBeNull();
  });
});
