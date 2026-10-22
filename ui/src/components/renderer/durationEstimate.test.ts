/**
 * An estimated duration is replaced by what the media element measures.
 *
 * An mp3 cannot be measured agent-side (peak extraction is WAV-only, the container probe reads
 * MP4/MOV), so its manifest carries a fixed 600s hint. A 5.04s track drew a ten-minute timeline.
 * The element knows the real length once loaded.
 */
import { describe, it, expect } from 'vitest';

/** The precedence rule as NleTimeline applies it. */
function effectiveDuration(manifestDuration: number, isEstimate: boolean, measured: number) {
  return isEstimate && measured > 0 ? measured : manifestDuration;
}

describe('duration precedence', () => {
  it('replaces the 600s estimate with the real 5.04s once measured', () => {
    expect(effectiveDuration(600, true, 5.04)).toBe(5.04);
  });

  it('keeps a measured manifest duration even if the element disagrees', () => {
    // shots.json stated 30.03s; the element may report the full proxy length.
    expect(effectiveDuration(30.03, false, 120.02)).toBe(30.03);
  });

  it('holds the estimate until the element reports something usable', () => {
    expect(effectiveDuration(600, true, 0)).toBe(600);
    expect(effectiveDuration(600, true, NaN as unknown as number)).toBe(600);
  });

  it('ignores a non-finite measurement, which is what a streaming source reports', () => {
    const measured = Number.POSITIVE_INFINITY;
    const usable = Number.isFinite(measured) && measured > 0 ? measured : 0;
    expect(effectiveDuration(600, true, usable)).toBe(600);
  });
});
