import { describe, expect, it } from 'vitest';
import { formatFrameTimecode, fpsFromFrameTimes, frameAt, snapFps, snapToFrame } from './frameTime';

describe('frameTime', () => {
  it('snaps a measured rate to the standard one', () => {
    expect(snapFps(23.98)).toBe(23.976);
    expect(snapFps(24.3)).toBe(24);
    expect(snapFps(29.95)).toBe(29.97);
    expect(snapFps(59.9)).toBe(59.94);
    expect(snapFps(12.5)).toBe(12.5);
    expect(snapFps(0)).toBeNull();
    expect(snapFps(Number.NaN)).toBeNull();
  });

  it('measures the rate from presented-frame times (median gap, robust to a dropped frame)', () => {
    const t = [0, 1, 2, 4, 5, 6, 7, 8].map((i) => i / 25);   // frame 3 was dropped
    expect(fpsFromFrameTimes(t)).toBe(25);
    expect(fpsFromFrameTimes([0, 0.04])).toBeNull();          // too few to trust
  });

  it('names the frame under a time and lands marks on frame starts', () => {
    expect(frameAt(1, 24)).toBe(24);                           // a frame-start time stays on its frame
    expect(frameAt(1.0415, 24)).toBe(24);
    expect(frameAt(1.042, 24)).toBe(25);
    expect(snapToFrame(1.03, 24)).toBeCloseTo(1, 9);
    expect(snapToFrame(1.03, null)).toBe(1.03);                // no rate: untouched
  });

  it('formats HH:MM:SS:FF, counting 29.97 in 30 nominal frames (NDF)', () => {
    expect(formatFrameTimecode(0, 24)).toBe('00:00:00:00');
    expect(formatFrameTimecode(194 / 24, 24)).toBe('00:00:08:02');
    expect(formatFrameTimecode(3725.5, 25)).toBe('01:02:05:12');
    expect(formatFrameTimecode(30 / 29.97, 29.97)).toBe('00:00:01:00');
  });
});
