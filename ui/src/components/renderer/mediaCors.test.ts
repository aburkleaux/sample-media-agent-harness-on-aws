/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { __resetMediaCorsForTests, corsReadable, gainCrossOrigin, watchCorsRefusal } from './mediaCors';
import { applyMediaGain } from './mediaGain';

const M2C = 'https://m2c-proxy.s3.us-west-2.amazonaws.com/micro-workflow/tts-on-aws/voices/x/preview.wav?X-Amz-Signature=a';

beforeEach(() => __resetMediaCorsForTests());

describe('media CORS (staging 20fd99a7: every audition preview refused to load)', () => {
  it('asks for CORS only on a cross-origin source, and stops once that origin refused', () => {
    expect(gainCrossOrigin('/api/artifact?key=x')).toBeUndefined();
    expect(gainCrossOrigin(M2C)).toBe('anonymous');
    const el = document.createElement('audio');
    el.crossOrigin = 'anonymous';
    el.src = M2C;
    el.load = vi.fn();
    watchCorsRefusal(el);
    el.dispatchEvent(new Event('error'));                 // a CORS-blocked load: an error before any data
    expect(el.hasAttribute('crossorigin')).toBe(false);   // retried as plain playback
    expect(el.load).toHaveBeenCalledTimes(1);
    expect(corsReadable(el)).toBe(false);
    expect(gainCrossOrigin(M2C)).toBeUndefined();         // the next element from that host asks for none
  });

  it('a same-origin or CORS-granted element is readable', () => {
    const a = document.createElement('audio');
    a.src = '/api/artifact?key=x';
    expect(corsReadable(a)).toBe(true);
    const b = document.createElement('audio');
    b.crossOrigin = 'anonymous';
    b.src = 'https://cdn.example/x.wav';
    expect(corsReadable(b)).toBe(true);
    const c = document.createElement('audio');
    c.src = 'https://cdn.example/x.wav';                  // cross-origin, no CORS asked
    expect(corsReadable(c)).toBe(false);
  });

  it('the gain boost never routes a non-CORS element through Web Audio (it would play silent)', () => {
    const created = vi.fn();
    (window as unknown as { AudioContext: unknown }).AudioContext = class { createMediaElementSource() { created(); return { connect: () => ({ connect: () => {} }) }; } createGain() { return { gain: { value: 1 } }; } resume() { return Promise.resolve(); } destination = {}; };
    const el = document.createElement('audio');
    el.src = M2C;                                         // no crossOrigin
    applyMediaGain(el, [0.02, -0.02]);                    // quiet: would normally boost
    el.dispatchEvent(new Event('play'));
    expect(created).not.toHaveBeenCalled();
  });
});
