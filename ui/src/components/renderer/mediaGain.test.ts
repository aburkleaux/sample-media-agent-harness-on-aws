// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { peaksMax, normalizationGain, applyMediaGain } from './mediaGain';

describe('peaksMax', () => {
  it('returns the largest absolute sample', () => {
    expect(peaksMax([0.001, -0.02, 0.015, -0.008])).toBeCloseTo(0.02);
  });
  it('returns 0 for empty/invalid input', () => {
    expect(peaksMax([])).toBe(0);
    expect(peaksMax(undefined)).toBe(0);
    expect(peaksMax(null)).toBe(0);
  });
});

describe('normalizationGain', () => {
  it('boosts a very quiet track toward the target peak', () => {
    // the live TTS wav peaked at ~0.0216 -> should get a big (but capped) boost
    expect(normalizationGain(0.0216)).toBeCloseTo(16); // 0.9/0.0216 = 41.6, capped at 16
  });
  it('boosts a moderately quiet track exactly to target without clipping', () => {
    expect(normalizationGain(0.3)).toBeCloseTo(3); // 0.9/0.3 = 3, under the cap
  });
  it('never attenuates a track that is already loud enough', () => {
    expect(normalizationGain(0.95)).toBe(1);
    expect(normalizationGain(1.0)).toBe(1);
  });
  it('is a no-op for unknown/invalid peaks (native playback)', () => {
    expect(normalizationGain(0)).toBe(1);
    expect(normalizationGain(NaN)).toBe(1);
    expect(normalizationGain(-0.5)).toBe(1);
  });
  it('respects a custom target and cap', () => {
    expect(normalizationGain(0.1, { target: 0.5, maxGain: 4 })).toBeCloseTo(4); // 0.5/0.1=5, capped 4
    expect(normalizationGain(0.25, { target: 0.5, maxGain: 4 })).toBeCloseTo(2);
  });
});

describe('applyMediaGain lazy wiring', () => {
  function fakeEl() {
    const listeners: Record<string, Array<() => void>> = {};
    const el = {
      addEventListener: (ev: string, cb: () => void) => { (listeners[ev] ||= []).push(cb); },
    } as unknown as HTMLMediaElement;
    return { el, fire: (ev: string) => (listeners[ev] || []).forEach((c) => c()), count: (ev: string) => (listeners[ev] || []).length };
  }

  it('does NOT create the Web Audio graph at mount; wires it on the first play', () => {
    let created = 0;
    class FakeCtx {
      destination = {};
      createMediaElementSource() { created += 1; return { connect: () => ({ connect: () => undefined }) }; }
      createGain() { return { gain: { value: 1 }, connect: () => ({ connect: () => undefined }) }; }
      resume() { return Promise.resolve(); }
    }
    (window as unknown as { AudioContext: unknown }).AudioContext = FakeCtx;
    const { el, fire, count } = fakeEl();
    // quiet peaks → gain > 1 → should wire, but only on the play gesture
    applyMediaGain(el, [0.02, -0.02]);
    expect(created).toBe(0);          // native output is untouched at mount (no suspended-context capture)
    expect(count('play')).toBe(1);    // a single play initializer is attached
    applyMediaGain(el, [0.02, -0.02]); // a re-render must not stack another listener
    expect(count('play')).toBe(1);
    fire('play');                     // user presses play → wire inside the gesture
    expect(created).toBe(1);
  });

  it('leaves loud audio on native playback (no Web Audio, no listener)', () => {
    let created = 0;
    class FakeCtx { destination = {}; createMediaElementSource() { created += 1; return { connect: () => ({ connect: () => undefined }) }; } createGain() { return { gain: { value: 1 }, connect: () => ({ connect: () => undefined }) }; } resume() { return Promise.resolve(); } }
    (window as unknown as { AudioContext: unknown }).AudioContext = FakeCtx;
    const { el, count } = fakeEl();
    applyMediaGain(el, [0.95]); // peak >= target → gain 1 → nothing to do
    expect(count('play')).toBe(0);
    expect(created).toBe(0);
  });
});
