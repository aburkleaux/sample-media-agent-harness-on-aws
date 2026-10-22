/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { AssetPreviewModal } from './AssetPreviewModal';

afterEach(cleanup);

/** jsdom media elements do not play: give the element a settable clock and a recorded pause. */
function fakeClock(el: HTMLMediaElement) {
  let t = 0;
  const log = { seeks: [] as number[], pauses: 0 };
  Object.defineProperty(el, 'readyState', { configurable: true, get: () => 1 });
  Object.defineProperty(el, 'currentTime', { configurable: true, get: () => t, set: (v: number) => { t = v; log.seeks.push(v); } });
  el.pause = () => { log.pauses += 1; };
  return { log, playTo: (v: number) => { t = v; el.dispatchEvent(new Event('timeupdate')); } };
}

describe('AssetPreviewModal: a cited range plays once', () => {
  it('seeks to the start once and pauses at the end; a re-render with a NEW range object does not seek back', () => {
    const props = { kind: 'video' as const, src: 'https://s3/clip.mp4?sig', name: 'clip.mp4', onClose: () => {} };
    // Mount once without a range so the clock is installed before the range effect runs.
    const { rerender } = render(<AssetPreviewModal {...props} />);
    const clock = fakeClock(screen.getByTestId('asset-preview-video') as HTMLMediaElement);
    rerender(<AssetPreviewModal {...props} range={{ startMs: 9000, endMs: 14000 }} />);
    expect(clock.log.seeks).toEqual([9]);
    clock.playTo(11);
    // The chat re-renders mid-play (streaming, polling): the caller builds `range` inline, a new object each time.
    rerender(<AssetPreviewModal {...props} range={{ startMs: 9000, endMs: 14000 }} />);
    rerender(<AssetPreviewModal {...props} range={{ startMs: 9000, endMs: 14000 }} />);
    expect(clock.log.seeks).toEqual([9]);          // never back to the start: that was the "loop"
    clock.playTo(14.1);
    expect(clock.log.pauses).toBe(1);
    clock.playTo(15);                              // the user pressed play again: carry on, no second pause
    expect(clock.log.pauses).toBe(1);
  });
});
