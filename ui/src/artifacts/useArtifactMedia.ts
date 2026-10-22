/**
 * React glue for artifact URL resolution (see ./artifactUrls.ts).
 *
 *  - `useArtifactMediaResign(containerRef)`: ONE capture-phase `error` listener per render host.
 *    Media and image `error` events do not bubble, but a capture listener on an ancestor still
 *    receives them, so a single listener covers every <video>/<audio>/<img>/<source> any template
 *    renders. When the failing element's src maps to an artifact key, the cached URL is dropped,
 *    re-resolved once, and assigned imperatively (restoring position and play state). React only
 *    rewrites `src` when the prop value changes, so the imperative assignment persists.
 *  - `useResolvedArtifactUrl(url)`: the presigned URL for one identifier (or the url itself when it
 *    is not an artifact identifier, or when resolution fails).
 *  - `useResolvedArtifactData(value)`: a deep-resolved copy of block/manifest data.
 */

import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import {
  artifactKeyOf,
  hasArtifactRefs,
  invalidateArtifactUrl,
  keyForUrl,
  peekArtifactUrl,
  rememberOriginal,
  resolveArtifactUrls,
  resolveArtifactUrlsDeep,
  resolveArtifactUrlsDeepSync,
  type DeepResolveOptions,
} from './artifactUrls';

/** At most one re-sign per element in this window, so a URL that keeps failing cannot loop. */
export const RESIGN_MIN_INTERVAL_MS = 30_000;

const lastResign = new WeakMap<Element, number>();

/** Waits between further tries when a re-sign finds nothing yet (~20 s in all). */
export const RESIGN_RETRY_DELAYS_MS = [1_500, 4_000, 6_000, 8_000];
let resignSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
/** Test seam: replace the wait between re-sign tries. */
export function __setResignSleepForTests(fn: (ms: number) => Promise<void>): void { resignSleep = fn; }

const MEDIA_TAGS = new Set(['VIDEO', 'AUDIO', 'IMG', 'SOURCE']);

function srcCandidates(el: Element): string[] {
  const out: string[] = [];
  const attr = el.getAttribute('src');
  if (attr) out.push(attr);
  const prop = (el as HTMLImageElement | HTMLMediaElement | HTMLSourceElement).src;
  if (typeof prop === 'string' && prop) out.push(prop);
  const current = (el as HTMLImageElement | HTMLMediaElement).currentSrc;
  if (typeof current === 'string' && current) out.push(current);
  return out;
}

/** Exported for tests: the handler the hook installs. Resolves when the retry (if any) is applied. */
export async function resignOnMediaError(ev: Event): Promise<boolean> {
  const el = ev.target;
  if (!(el instanceof Element) || !MEDIA_TAGS.has(el.tagName)) return false;
  let key: string | null = null;
  for (const c of srcCandidates(el)) {
    key = keyForUrl(c);
    if (key) break;
  }
  if (!key) return false;

  const now = Date.now();
  const last = lastResign.get(el);
  if (last !== undefined && now - last < RESIGN_MIN_INTERVAL_MS) return false;
  lastResign.set(el, now);

  const media: HTMLMediaElement | null =
    el.tagName === 'SOURCE'
      ? (el.parentElement instanceof HTMLMediaElement ? el.parentElement : null)
      : el instanceof HTMLMediaElement ? el : null;
  const resume = media ? { time: media.currentTime || 0, playing: !media.paused && !media.ended } : null;

  invalidateArtifactUrl(key);
  let fresh = (await resolveArtifactUrls([key])).get(key);
  // A result rendered the moment it was written may not be signable yet (its asset row lands a beat later):
  // keep asking for a while rather than leaving the element blank until a reload (staging 2026-10-19: 4 of 5
  // gallery frames stayed blank; all 5 loaded on reload).
  for (let i = 0; !fresh && i < RESIGN_RETRY_DELAYS_MS.length && el.isConnected; i++) {
    await resignSleep(RESIGN_RETRY_DELAYS_MS[i]);
    invalidateArtifactUrl(key);
    fresh = (await resolveArtifactUrls([key])).get(key);
  }
  if (!fresh || !el.isConnected) return false;

  if (media && resume) {
    const restore = () => {
      try { if (resume.time > 0) media.currentTime = resume.time; } catch { /* not seekable yet */ }
      if (resume.playing) void Promise.resolve(media.play()).catch(() => { /* autoplay policy */ });
    };
    media.addEventListener('loadedmetadata', restore, { once: true });
  }
  (el as HTMLImageElement | HTMLMediaElement | HTMLSourceElement).src = fresh.url;
  if (el.tagName === 'SOURCE' && media) {
    try { media.load(); } catch { /* jsdom */ }
  }
  return true;
}

function onErrorCapture(ev: Event): void {
  void resignOnMediaError(ev);
}

/**
 * Attach the re-sign listener to the render host. Re-attaches if the ref's element changes
 * (e.g. a host that renders a loading state first), and detaches on unmount or when `enabled`
 * turns false.
 */
export function useArtifactMediaResign(containerRef: RefObject<HTMLElement | null>, enabled = true): void {
  const attached = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const el = enabled ? containerRef.current : null;
    if (el === attached.current) return;
    attached.current?.removeEventListener('error', onErrorCapture, true);
    el?.addEventListener('error', onErrorCapture, true);
    attached.current = el;
  });
  useEffect(() => () => {
    attached.current?.removeEventListener('error', onErrorCapture, true);
    attached.current = null;
  }, []);
}

export interface ResolvedUrlState {
  /** The URL to load: presigned, the original (not an artifact / resolution failed), or undefined while resolving. */
  src: string | undefined;
  /** The artifact key, or null when `url` is not an artifact identifier. */
  key: string | null;
  pending: boolean;
}

/** The presigned URL for one artifact identifier; non-artifact URLs pass through unchanged. */
export function useResolvedArtifactUrl(url: string | undefined, artifactsDomain?: string): ResolvedUrlState {
  const key = url ? artifactKeyOf(url, artifactsDomain) : null;
  const [settled, setSettled] = useState<{ key: string; src: string } | null>(() => {
    const hit = key ? peekArtifactUrl(key) : null;
    return key && hit ? { key, src: hit.url } : null;
  });

  useEffect(() => {
    if (!key || !url) return;
    if (settled?.key === key) return;
    const hit = peekArtifactUrl(key);
    if (hit) { rememberOriginal(hit.url, url); setSettled({ key, src: hit.url }); return; }
    let alive = true;
    void resolveArtifactUrls([key]).then((m) => {
      if (!alive) return;
      const r = m.get(key);
      if (r) rememberOriginal(r.url, url);
      setSettled({ key, src: r ? r.url : url });
    });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, url]);

  if (!key) return { src: url, key: null, pending: false };
  if (settled && settled.key === key) return { src: settled.src, key, pending: false };
  const hit = peekArtifactUrl(key);
  if (hit) return { src: hit.url, key, pending: false };
  return { src: undefined, key, pending: true };
}

/**
 * Deep-resolve render data. Returns `value` itself when it holds no artifact identifiers, the
 * resolved copy synchronously when the cache is warm, else null until resolution settles.
 * Keyed on CONTENT, so a poll that re-delivers equal data as a new object neither re-resolves nor
 * hands the template a new object.
 */
export function useResolvedArtifactData<T>(value: T, opts?: DeepResolveOptions): T | null {
  const sig = useMemo(() => {
    try { return JSON.stringify(value) ?? ''; } catch { return String(Math.random()); }
  }, [value]);
  const latest = useRef(value);
  latest.current = value;
  const needs = useMemo(() => hasArtifactRefs(value, opts), [sig]); // eslint-disable-line react-hooks/exhaustive-deps
  // Warm cache: resolve synchronously, so a remount (tab switch, ⟳ refresh) renders at once.
  const syncNow = useMemo(() => (needs ? resolveArtifactUrlsDeepSync(value, opts) : null), [sig]); // eslint-disable-line react-hooks/exhaustive-deps
  const [resolved, setResolved] = useState<{ sig: string; value: T } | null>(null);

  useEffect(() => {
    if (!needs || syncNow) return;
    if (resolved?.sig === sig) return;
    let alive = true;
    void resolveArtifactUrlsDeep(latest.current, opts).then((v) => {
      if (alive) setResolved({ sig, value: v });
    });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, needs]);

  if (!needs) return value;
  if (syncNow) return syncNow;
  if (resolved && resolved.sig === sig) return resolved.value;
  // A content change keeps showing the previous resolution until the new one lands (no flash).
  return resolved ? resolved.value : null;
}
