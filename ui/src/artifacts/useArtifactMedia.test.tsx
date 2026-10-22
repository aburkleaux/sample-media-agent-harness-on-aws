// @vitest-environment jsdom
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useRef } from 'react';
import { render, waitFor, act } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));
vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

import { __resetArtifactUrlsForTests, resolveArtifactUrls } from './artifactUrls';
import { RESIGN_MIN_INTERVAL_MS, RESIGN_RETRY_DELAYS_MS, __setResignSleepForTests, useArtifactMediaResign, useResolvedArtifactUrl } from './useArtifactMedia';
import { makeFakePresigner, type FakePresigner } from './testPresigner';

const CDN = 'https://d1abcd.cloudfront.net';
const KEY = 'artifacts/sess-1/clip.mp4';
let presigner: FakePresigner;

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
  __setResignSleepForTests(async () => {});
});
afterEach(() => {
  vi.useRealTimers();
});

function Host({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useArtifactMediaResign(ref);
  return <div ref={ref} data-testid="host">{children}</div>;
}

/** Media/img `error` does not bubble; the hook must catch it in the capture phase. */
function fireMediaError(el: Element) {
  el.dispatchEvent(new Event('error', { bubbles: false }));
}

describe('useArtifactMediaResign', () => {
  it('keeps trying when the result is not signable yet, then loads it (a just-written gallery frame)', async () => {
    const IMG = 'artifacts/sess-1/frame_0021.jpg';
    presigner.notFound.add(IMG);
    const { container } = render(<Host><img src={`${CDN}/${IMG}`} alt="" /></Host>);
    const img = container.querySelector('img')!;
    let tries = 0;
    __setResignSleepForTests(async () => { tries += 1; if (tries === 2) presigner.notFound.delete(IMG); });
    fireMediaError(img);
    await waitFor(() => expect(img.src).toBe(presigner.urlFor(IMG)));
    expect(tries).toBe(2);
    expect(tries).toBeLessThan(RESIGN_RETRY_DELAYS_MS.length + 1);
  });

  it('re-signs a <video> whose presigned src failed, restoring position and playback', async () => {
    const first = (await resolveArtifactUrls([KEY])).get(KEY)!.url;
    presigner.generation = 2;
    const { container } = render(<Host><video src={first} /></Host>);
    const video = container.querySelector('video')!;
    const play = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(video, 'paused', { configurable: true, get: () => false });
    Object.defineProperty(video, 'play', { configurable: true, value: play });
    let t = 12.5;
    Object.defineProperty(video, 'currentTime', { configurable: true, get: () => t, set: (v: number) => { t = v; } });

    fireMediaError(video);
    const fresh = presigner.urlFor(KEY, false, 2);
    await waitFor(() => expect(video.src).toBe(fresh));
    expect(presigner.calls).toHaveLength(2);
    expect(presigner.calls[1].keys).toEqual([KEY]);

    t = 0; // the new src reloads from the start…
    video.dispatchEvent(new Event('loadedmetadata'));
    expect(t).toBe(12.5); // …and the hook seeks back
    expect(play).toHaveBeenCalledTimes(1);
  });

  it('re-signs a <video> whose baked presigned project-asset src expired (render-video regression)', async () => {
    const PID = 'p' + 'a'.repeat(32);
    const VID = 'video:6865ef88-8411-4629-898f-618ee72d080f';
    const STALE = `https://mediaworks-harness-skills-store-100000000002-us-west-2.s3.us-west-2.amazonaws.com/projects/${PID}/video%3A6865ef88-8411-4629-898f-618ee72d080f/meridian.mp4?X-Amz-Signature=stale`;
    const FRESH = 'https://fresh.s3.us-west-2.amazonaws.com/meridian?X-Amz-Signature=new';
    authFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input);
      if (url === `/projects/${PID}/assets/urls`) {
        expect(JSON.parse(String(init?.body)).asset_ids).toEqual([VID]);
        return new Response(JSON.stringify({
          urls: { [VID]: { source: { url: FRESH, expiresAt: Math.floor(Date.now() / 1000) + 3600 } } },
          errors: {},
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return presigner.handle(input, init);
    });
    const { container } = render(<Host><video src={STALE} /></Host>);
    const video = container.querySelector('video')!;
    fireMediaError(video);
    await waitFor(() => expect(video.src).toBe(FRESH));
  });

  it('rate-limits to one re-sign per element per 30 s', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-20T00:00:00Z'));
    const { container } = render(<Host><img src={`${CDN}/${KEY.replace('.mp4', '.png')}`} alt="" /></Host>);
    const img = container.querySelector('img')!;
    const key = KEY.replace('.mp4', '.png');

    fireMediaError(img);
    await waitFor(() => expect(img.src).toBe(presigner.urlFor(key)));
    expect(presigner.calls).toHaveLength(1);

    fireMediaError(img); // within 30 s: ignored
    await act(async () => { await Promise.resolve(); });
    expect(presigner.calls).toHaveLength(1);

    vi.setSystemTime(Date.now() + RESIGN_MIN_INTERVAL_MS + 1);
    presigner.generation = 3;
    fireMediaError(img);
    await waitFor(() => expect(img.src).toBe(presigner.urlFor(key, false, 3)));
    expect(presigner.calls).toHaveLength(2);
  });

  it('re-signs a <source> child and reloads its media element', async () => {
    const { container } = render(<Host><video><source src={`${CDN}/${KEY}`} type="video/mp4" /></video></Host>);
    const video = container.querySelector('video')!;
    const load = vi.fn();
    Object.defineProperty(video, 'load', { configurable: true, value: load });
    const source = container.querySelector('source')!;
    fireMediaError(source);
    await waitFor(() => expect(source.src).toBe(presigner.urlFor(KEY)));
    expect(load).toHaveBeenCalled();
  });

  it('ignores errors from non-artifact media and when resolution fails', async () => {
    const { container } = render(
      <Host>
        <img data-testid="fal" src="https://v3.fal.media/files/x.png" alt="" />
        <img data-testid="cdn" src={`${CDN}/artifacts/s/y.png`} alt="" />
      </Host>,
    );
    fireMediaError(container.querySelector('[data-testid="fal"]')!);
    await act(async () => { await Promise.resolve(); });
    expect(presigner.calls).toHaveLength(0);

    presigner.failStatus = 500;
    const cdn = container.querySelector('[data-testid="cdn"]') as HTMLImageElement;
    fireMediaError(cdn);
    // A bounded number of tries (the first + one per retry delay), then it stops and leaves the src alone.
    await waitFor(() => expect(presigner.calls).toHaveLength(RESIGN_RETRY_DELAYS_MS.length + 1));
    await act(async () => { await Promise.resolve(); });
    expect(presigner.calls).toHaveLength(RESIGN_RETRY_DELAYS_MS.length + 1);
    expect(cdn.getAttribute('src')).toBe(`${CDN}/artifacts/s/y.png`);
  });

  it('detaches on unmount', async () => {
    const { container, unmount } = render(<Host><img src={`${CDN}/artifacts/s/z.png`} alt="" /></Host>);
    const img = container.querySelector('img')!;
    const host = container.querySelector('[data-testid="host"]')!;
    unmount();
    host.appendChild(img);
    fireMediaError(img);
    await act(async () => { await Promise.resolve(); });
    expect(presigner.calls).toHaveLength(0);
  });
});

describe('useResolvedArtifactUrl', () => {
  function Probe({ url }: { url: string }) {
    const r = useResolvedArtifactUrl(url);
    return <span data-testid="probe" data-src={r.src ?? ''} data-pending={String(r.pending)} />;
  }

  it('is pending, then the presigned URL; non-artifact URLs pass through synchronously', async () => {
    const { getByTestId, rerender } = render(<Probe url={`${CDN}/${KEY}`} />);
    expect(getByTestId('probe')).toHaveAttribute('data-pending', 'true');
    await waitFor(() => expect(getByTestId('probe')).toHaveAttribute('data-src', presigner.urlFor(KEY)));

    rerender(<Probe url="https://v3.fal.media/x.mp4" />);
    expect(getByTestId('probe')).toHaveAttribute('data-src', 'https://v3.fal.media/x.mp4');
  });

  it('falls back to the original URL when resolution fails', async () => {
    presigner.failStatus = 500;
    const { getByTestId } = render(<Probe url={`${CDN}/${KEY}`} />);
    await waitFor(() => expect(getByTestId('probe')).toHaveAttribute('data-src', `${CDN}/${KEY}`));
  });
});
