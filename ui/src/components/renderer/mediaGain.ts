import { corsReadable } from './mediaCors';

// Peak-based playback normalization.
//
// TTS / some stem outputs are synthesized very quiet (a live TTS wav peaked at
// ~0.02 of full scale, ~-33 dBFS), and an <audio> element plays at native volume
// while its `.volume` can only *attenuate* (<=1.0). To make quiet renders audible
// we boost with a Web Audio GainNode, scaled from the manifest peaks so we amplify
// toward a target peak WITHOUT clipping, and only ever boost (never attenuate).

/** Largest absolute sample in a peaks array (peaks are normalized to [-1, 1]). */
export function peaksMax(peaks: number[] | undefined | null): number {
  if (!Array.isArray(peaks) || peaks.length === 0) return 0;
  let m = 0;
  for (const p of peaks) {
    const a = Math.abs(Number(p));
    if (isFinite(a) && a > m) m = a;
  }
  return m;
}

/**
 * Gain to bring a track peaking at `peakMax` up to `target` peak, capped at
 * `maxGain` (so we never over-amplify near-silence into noise) and floored at 1
 * (never attenuate). Returns 1 (no-op) when the track is already loud enough or
 * the peak is unknown/invalid.
 */
export function normalizationGain(
  peakMax: number,
  opts?: { target?: number; maxGain?: number },
): number {
  const target = opts?.target ?? 0.9;
  const maxGain = opts?.maxGain ?? 16;
  if (!peakMax || !isFinite(peakMax) || peakMax <= 0) return 1; // unknown → leave native
  if (peakMax >= target) return 1; // already loud enough
  return Math.min(target / peakMax, maxGain);
}

// One audio graph per media element (createMediaElementSource throws if called
// twice on the same element, e.g. React strict-mode double effects / re-renders).
const _graphs = new WeakMap<HTMLMediaElement, GainNode>();
// Elements we've already attached a lazy 'play' initializer to (so repeated ref
// callbacks / re-renders don't stack duplicate listeners before the first play).
const _wired = new WeakSet<HTMLMediaElement>();

/**
 * Route a media element through a GainNode when its peaks indicate quiet audio.
 * Safe to call from a ref callback: idempotent per element, feature-detects
 * AudioContext, and falls back to native playback on any error. Does nothing when
 * the computed gain is 1.
 *
 * The Web Audio graph is wired LAZILY, inside the element's first `play` event — NOT at mount. Creating an AudioContext outside a user gesture yields a SUSPENDED
 * context, and the moment createMediaElementSource() captures the element its native
 * output is redirected into that (silent) graph → the element plays with no sound.
 * Deferring to the play gesture means native playback is untouched until then and the
 * context is started (resumed) within the gesture, so it is never left suspended.
 */
export function applyMediaGain(el: HTMLMediaElement | null, peaks: number[] | undefined | null): void {
  if (!el) return;
  const existing = _graphs.get(el);
  if (existing) { existing.gain.value = normalizationGain(peaksMax(peaks)); return; }
  if (normalizationGain(peaksMax(peaks)) <= 1) return; // nothing to boost → native playback
  if (_wired.has(el)) return; // initializer already pending for the first play
  _wired.add(el);
  const init = () => {
    if (_graphs.get(el)) return; // already wired on an earlier play
    const gain = normalizationGain(peaksMax(peaks));
    if (gain <= 1) return;
    // A cross-origin source without CORS plays SILENT through Web Audio: keep native playback (mediaCors.ts).
    if (!corsReadable(el)) return;
    try {
      const AC: typeof AudioContext | undefined =
        window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const src = ctx.createMediaElementSource(el);
      const node = ctx.createGain();
      node.gain.value = gain;
      src.connect(node).connect(ctx.destination);
      _graphs.set(el, node);
      void ctx.resume().catch(() => {});
    } catch {
      /* AudioContext unavailable or element already sourced — native playback stands. */
    }
  };
  el.addEventListener('play', init);
}
