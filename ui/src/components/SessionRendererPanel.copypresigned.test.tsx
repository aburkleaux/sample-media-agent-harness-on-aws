// @vitest-environment jsdom
/**
 * "Copy source URL" since the artifacts CDN went private (artifactsCdnPrivate, staging): a tab's
 * source is a stable CloudFront identifier, which now 403s when opened. For one of OUR artifacts the
 * panel pre-resolves a presigned URL when the tab becomes active and the click writes it
 * SYNCHRONOUSLY (a clipboard write after an await can be rejected once the user activation lapses).
 * Unresolved at click time → await, then copy; resolution failure → copy the identifier. Anything
 * that is not our artifact is copied unchanged. Session 36685185 reported the broken copy.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));
vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));
// `test-no-renderer` is a placeholder that renders nothing: since unknown templates now drop their tab
// ("if it can't render, it shouldn't show"), register a no-op renderer for it so the tab — and its copy
// button — stays.
vi.mock('./renderer/RendererRegistry', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./renderer/RendererRegistry')>();
  return { ...actual, lookupRenderer: (t: string) => (t === 'test-no-renderer' ? () => null : actual.lookupRenderer(t)) };
});

import SessionRendererPanel, { absoluteCopyUrl, expiryNote, tabSourceUrl } from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';
import { __resetArtifactUrlsForTests } from '../artifacts/artifactUrls';
import { makeFakePresigner, type FakePresigner } from '../artifacts/testPresigner';

const CDN = 'https://dexamplecdn002.cloudfront.net';
const CLIP = `${CDN}/artifacts/36685185/output/shot_3.mp4`;
const CLIP_KEY = 'artifacts/36685185/output/shot_3.mp4';
const OTHER = `${CDN}/artifacts/36685185/output/shot_4.mp4`;
const OTHER_KEY = 'artifacts/36685185/output/shot_4.mp4';

let presigner: FakePresigner;
const writeText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
  writeText.mockClear();
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
});

/** A rendered tab whose block has no registry renderer, so only the copy path resolves anything. */
function tab(src: string, label = 'Clip'): RenderHistoryEntry {
  return {
    turnIndex: 0, timestamp: 1, label, artifactUrls: [src],
    blocks: [{ template: 'test-no-renderer', title: label, data: {} }],
  } as unknown as RenderHistoryEntry;
}

function panel(history: RenderHistoryEntry[], currentIndex = 0) {
  return render(<SessionRendererPanel history={history} currentIndex={currentIndex} onNavigate={vi.fn()} onClose={vi.fn()} />);
}

const resolvedKeys = () => presigner.calls.flatMap((c) => c.keys);

describe('SessionRendererPanel — copy source URL with a private artifacts CDN', () => {
  it('pre-resolves the active tab and copies the presigned URL synchronously in the click', async () => {
    panel([tab(CLIP)]);
    await waitFor(() => expect(resolvedKeys()).toContain(CLIP_KEY));
    // Let the resolution settle into the cache, then click: no await between click and write.
    await Promise.resolve();
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(presigner.urlFor(CLIP_KEY));
    expect(await screen.findByTestId('copy-source-expiry')).toHaveTextContent('link expires in 1 hour');
    expect(screen.getByTestId('copy-source-url')).toHaveTextContent('Copied source URL');
  });

  it('keeps the identifier in the tooltip (the stable reference), not the expiring URL', async () => {
    panel([tab(CLIP)]);
    await waitFor(() => expect(resolvedKeys()).toContain(CLIP_KEY));
    expect(screen.getByTestId('copy-source-url').getAttribute('title')).toContain(CLIP);
  });

  it('awaits the resolution when the click comes first, then copies the presigned URL', async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    authFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      await gate;
      return presigner.handle(input, init);
    });
    panel([tab(CLIP)]);
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).not.toHaveBeenCalled();
    release();
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(presigner.urlFor(CLIP_KEY)));
    expect(writeText).toHaveBeenCalledTimes(1);
    // The pre-resolve and the click share ONE in-flight request.
    expect(presigner.calls).toHaveLength(1);
    expect(await screen.findByTestId('copy-source-expiry')).toHaveTextContent('1 hour');
  });

  it('falls back to the identifier when resolution fails, without an expiry note', async () => {
    presigner.failStatus = 500;
    panel([tab(CLIP)]);
    fireEvent.click(screen.getByTestId('copy-source-url'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(CLIP));
    await waitFor(() => expect(screen.getByTestId('copy-source-url')).toHaveTextContent('Copied source URL'));
    expect(screen.queryByTestId('copy-source-expiry')).toBeNull();
  });

  it('copies a non-artifact URL unchanged and synchronously, with no presign request', () => {
    const fal = 'https://v3.fal.media/files/abc/out.mp4';
    panel([tab(fal)]);
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledWith(fal);
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('re-resolves when the active tab changes and copies that tab\'s URL', async () => {
    const history = [tab(CLIP, 'Shot 3'), tab(OTHER, 'Shot 4')];
    const { rerender } = panel(history, 0);
    await waitFor(() => expect(resolvedKeys()).toContain(CLIP_KEY));
    rerender(<SessionRendererPanel history={history} currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(resolvedKeys()).toContain(OTHER_KEY));
    await Promise.resolve();
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledWith(presigner.urlFor(OTHER_KEY));
    expect(writeText).not.toHaveBeenCalledWith(presigner.urlFor(CLIP_KEY));
  });

  it('replaces a cached URL that is nearly spent instead of copying it', async () => {
    presigner.ttlSeconds = 5 * 60; // the cache would keep this URL, but a pasted link would die in 5 min
    panel([tab(CLIP)]);
    await waitFor(() => expect(resolvedKeys()).toContain(CLIP_KEY));
    const before = presigner.calls.length;
    presigner.generation = 2;
    presigner.ttlSeconds = 3600;
    fireEvent.click(screen.getByTestId('copy-source-url'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(presigner.urlFor(CLIP_KEY, false, 2)));
    expect(presigner.calls.length).toBeGreaterThan(before);
    expect(writeText).not.toHaveBeenCalledWith(presigner.urlFor(CLIP_KEY, false, 1));
  });

  it('tabSourceUrl still reports the identifier (persisted tabs never hold an expiring URL)', () => {
    expect(tabSourceUrl(tab(CLIP))).toBe(CLIP);
  });
});

// Staging 2026-10-10 (session 2dba61af): a render-video tab's source is a RAW PRESIGNED own-bucket
// project-asset URL (the bake). Copy must still re-sign it — through the asset-id-gated
// /projects/:pid/assets/urls route — not write the expiring/expired token verbatim.
describe('SessionRendererPanel — copy source URL for a baked presigned project-asset source', () => {
  const PID = 'p' + 'a'.repeat(32);
  const VID = 'video:6865ef88-8411-4629-898f-618ee72d080f';
  const PRESIGNED = `https://mediaworks-harness-skills-store-100000000002-us-west-2.s3.us-west-2.amazonaws.com/projects/${PID}/video%3A6865ef88-8411-4629-898f-618ee72d080f/meridian.mp4?X-Amz-Signature=stale`;
  const FRESH = 'https://fresh.s3.us-west-2.amazonaws.com/meridian?X-Amz-Signature=new';

  it('re-signs the project-asset through the Asset_URL_Route and copies the fresh URL', async () => {
    authFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input);
      if (url === `/projects/${PID}/assets/urls`) {
        const { asset_ids } = JSON.parse(String(init?.body));
        expect(asset_ids).toEqual([VID]);
        return new Response(JSON.stringify({
          urls: { [VID]: { source: { url: FRESH, expiresAt: Math.floor(Date.now() / 1000) + 3600 } } },
          errors: {},
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return presigner.handle(input, init);
    });
    panel([tab(PRESIGNED, 'Video')]);
    await waitFor(() => expect(writeText.mock.calls.length + 0).toBeGreaterThanOrEqual(0));
    // Pre-resolve settles into the cache; then the click writes synchronously.
    await waitFor(() => expect(authFetch).toHaveBeenCalledWith(`/projects/${PID}/assets/urls`, expect.anything()));
    await Promise.resolve();
    fireEvent.click(screen.getByTestId('copy-source-url'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(FRESH));
    expect(writeText).not.toHaveBeenCalledWith(PRESIGNED);
  });
});

describe('expiryNote', () => {
  const now = 1_800_000_000_000;
  it('says "1 hour" for a freshly minted URL and minutes otherwise', () => {
    expect(expiryNote(now / 1000 + 3600, now)).toBe('1 hour');
    expect(expiryNote(now / 1000 + 56 * 60, now)).toBe('1 hour');
    expect(expiryNote(now / 1000 + 20 * 60 + 30, now)).toBe('20 min');
    expect(expiryNote(now / 1000 + 10, now)).toBe('1 min');
  });
});

// Prod session d52d544e: an HTML page resolves to a same-origin VIEW-TICKET URL (the proxy re-signs the links
// inside it). It is relative, and "Copy source URL" put that relative path on the clipboard -- not a link.
describe('SessionRendererPanel — copy source URL of an HTML page', () => {
  const PAGE = `${CDN}/artifacts/36685185/output/storyboard.html`;
  const PAGE_KEY = 'artifacts/36685185/output/storyboard.html';
  const VIEW = `/api/artifact?key=${encodeURIComponent(PAGE_KEY)}&view=tkt`;

  it('copies the view-ticket URL as an ABSOLUTE link', async () => {
    authFetch.mockImplementation(async () => new Response(JSON.stringify({
      urls: { [PAGE_KEY]: { url: VIEW, expiresAt: Math.floor(Date.now() / 1000) + 3600 } }, errors: {},
    }), { status: 200, headers: { 'content-type': 'application/json' } }));
    panel([tab(PAGE, 'Storyboard')]);
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    await Promise.resolve();
    fireEvent.click(screen.getByTestId('copy-source-url'));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}${VIEW}`);
  });
});

describe('absoluteCopyUrl', () => {
  it('makes a same-origin path absolute and leaves everything else alone', () => {
    expect(absoluteCopyUrl('/api/artifact?key=a&view=t', 'https://app.example')).toBe('https://app.example/api/artifact?key=a&view=t');
    expect(absoluteCopyUrl('https://s3.example/x?sig=1', 'https://app.example')).toBe('https://s3.example/x?sig=1');
    expect(absoluteCopyUrl('//evil.example/x', 'https://app.example')).toBe('//evil.example/x');
  });
});
