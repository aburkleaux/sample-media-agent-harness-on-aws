// CORS for media elements: playback never needs it; only reading samples through Web Audio does.
//
// A `crossOrigin="anonymous"` element whose host sends no `Access-Control-Allow-Origin` refuses to LOAD
// at all ("source not supported") -- the M2C proxy bucket sends none, and every audition preview in
// staging 2be13758 / 20fd99a7 was silent because the gallery's <audio> asked for CORS. So:
//   * an element that only plays sets no crossOrigin;
//   * the one path that reads samples (mediaGain's GainNode) asks via `gainCrossOrigin`, and an origin
//     that refuses is remembered and the element reloaded WITHOUT CORS (it plays, just un-boosted);
//   * mediaGain wires a graph only on a CORS-readable element: a non-CORS cross-origin source routed
//     through Web Audio plays SILENT.

const refused = new Set<string>();
const nonCors = new WeakSet<HTMLMediaElement>();

function originOf(src: string | null | undefined): string | null {
  if (!src) return null;
  try {
    return new URL(src, window.location.href).origin;
  } catch {
    return null;
  }
}

function crossOriginSrc(src: string | null | undefined): boolean {
  const o = originOf(src);
  return !!o && o !== window.location.origin;
}

/** `crossOrigin` for an element whose samples Web Audio may read: same-origin needs none; a cross-origin
 *  host asks for CORS unless it already refused it in this tab. */
export function gainCrossOrigin(src: string | null | undefined): 'anonymous' | undefined {
  if (!crossOriginSrc(src)) return undefined;
  const o = originOf(src);
  return o && refused.has(o) ? undefined : 'anonymous';
}

/** On a CORS-blocked load (an error before any data), remember the origin and reload without CORS. Idempotent. */
export function watchCorsRefusal(el: HTMLMediaElement | null): void {
  if (!el || (el as unknown as { __corsWatched?: boolean }).__corsWatched) return;
  (el as unknown as { __corsWatched?: boolean }).__corsWatched = true;
  el.addEventListener('error', () => {
    if (!el.crossOrigin || el.readyState > 0) return;
    const src = el.currentSrc || el.src;
    if (!crossOriginSrc(src)) return;
    const o = originOf(src);
    if (o) refused.add(o);
    nonCors.add(el);
    el.removeAttribute('crossorigin');
    el.load();
  });
}

/** Whether Web Audio can read this element's samples (same-origin, or CORS was asked for and not refused). */
export function corsReadable(el: HTMLMediaElement): boolean {
  if (nonCors.has(el)) return false;
  const src = el.currentSrc || el.src;
  if (!crossOriginSrc(src)) return true;
  const o = originOf(src);
  return !!el.crossOrigin && !(o && refused.has(o));
}

/** Test seam. */
export function __resetMediaCorsForTests(): void {
  refused.clear();
}
