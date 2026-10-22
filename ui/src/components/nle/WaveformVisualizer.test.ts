import { describe, it, expect } from 'vitest';
import { resamplePeaks, renderWaveform, renderPlaceholder } from './WaveformVisualizer';

// ---------------------------------------------------------------------------
// resamplePeaks — Unit Tests
// ---------------------------------------------------------------------------

describe('resamplePeaks', () => {
  it('should return empty array for targetLength 0', () => {
    expect(resamplePeaks([0.5, 0.6, 0.7], 0)).toEqual([]);
  });

  it('should return zeros for empty input', () => {
    const result = resamplePeaks([], 5);
    expect(result).toHaveLength(5);
    expect(result.every((v) => v === 0)).toBe(true);
  });

  it('should return a copy when lengths match', () => {
    const input = [0.1, 0.5, 0.9];
    const result = resamplePeaks(input, 3);
    expect(result).toEqual(input);
    // Ensure it's a copy, not the same reference
    expect(result).not.toBe(input);
  });

  it('should downsample correctly with linear interpolation', () => {
    // 4 peaks → 2 peaks
    // Index 0 maps to source 0, index 1 maps to source 3
    const input = [0.0, 0.4, 0.8, 1.0];
    const result = resamplePeaks(input, 2);
    expect(result).toHaveLength(2);
    expect(result[0]).toBeCloseTo(0.0, 5);
    expect(result[1]).toBeCloseTo(1.0, 5);
  });

  it('should upsample correctly with linear interpolation', () => {
    // 2 peaks [0, 1] → 3 peaks should give [0, 0.5, 1]
    const input = [0.0, 1.0];
    const result = resamplePeaks(input, 3);
    expect(result).toHaveLength(3);
    expect(result[0]).toBeCloseTo(0.0, 5);
    expect(result[1]).toBeCloseTo(0.5, 5);
    expect(result[2]).toBeCloseTo(1.0, 5);
  });

  it('should handle single target length', () => {
    const input = [0.2, 0.4, 0.6, 0.8];
    const result = resamplePeaks(input, 1);
    expect(result).toHaveLength(1);
    // Single sample = average of all peaks
    expect(result[0]).toBeCloseTo(0.5, 5);
  });

  it('should handle single input peak to many', () => {
    const result = resamplePeaks([0.7], 5);
    expect(result).toHaveLength(5);
    // All values should be 0.7 since interpolating from same point
    result.forEach((v) => expect(v).toBeCloseTo(0.7, 5));
  });

  it('should produce values bounded by input min/max', () => {
    const input = [0.2, 0.8, 0.3, 0.9, 0.1];
    const result = resamplePeaks(input, 20);
    const inputMin = Math.min(...input);
    const inputMax = Math.max(...input);
    result.forEach((v) => {
      expect(v).toBeGreaterThanOrEqual(inputMin - 1e-10);
      expect(v).toBeLessThanOrEqual(inputMax + 1e-10);
    });
  });

  it('should produce exact target length for various sizes', () => {
    const input = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0];
    for (const target of [1, 3, 7, 10, 15, 50, 100]) {
      const result = resamplePeaks(input, target);
      expect(result).toHaveLength(target);
    }
  });
});

// ---------------------------------------------------------------------------
// renderWaveform — Unit Tests (using mock canvas context)
// ---------------------------------------------------------------------------

function createMockContext(): CanvasRenderingContext2D {
  const calls: Array<{ method: string; args: unknown[] }> = [];
  const handler: ProxyHandler<Record<string, unknown>> = {
    get(_target, prop) {
      if (prop === '_calls') return calls;
      return (...args: unknown[]) => {
        calls.push({ method: prop as string, args });
      };
    },
    set(_target, prop, value) {
      calls.push({ method: `set_${String(prop)}`, args: [value] });
      return true;
    },
  };
  return new Proxy({}, handler) as unknown as CanvasRenderingContext2D;
}

describe('renderWaveform', () => {
  it('should clear the canvas before rendering', () => {
    const ctx = createMockContext();
    renderWaveform({
      ctx,
      width: 100,
      height: 60,
      peaks: [0.5, 0.6, 0.7],
      duration: 3,
      currentTime: 1,
      color: '#4a90d9',
      playedColor: '#2ecc71',
    });
    const calls = (ctx as unknown as { _calls: Array<{ method: string }> })._calls;
    expect(calls[0].method).toBe('clearRect');
  });

  it('should not draw anything for empty peaks', () => {
    const ctx = createMockContext();
    renderWaveform({
      ctx,
      width: 100,
      height: 60,
      peaks: [],
      duration: 10,
      currentTime: 5,
      color: '#4a90d9',
      playedColor: '#2ecc71',
    });
    const calls = (ctx as unknown as { _calls: Array<{ method: string }> })._calls;
    // Only clearRect should be called
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('clearRect');
  });

  it('should not draw anything for zero duration', () => {
    const ctx = createMockContext();
    renderWaveform({
      ctx,
      width: 100,
      height: 60,
      peaks: [0.5],
      duration: 0,
      currentTime: 0,
      color: '#4a90d9',
      playedColor: '#2ecc71',
    });
    const calls = (ctx as unknown as { _calls: Array<{ method: string }> })._calls;
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('clearRect');
  });

  it('should use clipping for played region', () => {
    const ctx = createMockContext();
    renderWaveform({
      ctx,
      width: 200,
      height: 60,
      peaks: [0.5, 0.8, 0.3],
      duration: 10,
      currentTime: 5,
      color: '#4a90d9',
      playedColor: '#2ecc71',
    });
    const calls = (ctx as unknown as { _calls: Array<{ method: string }> })._calls;
    const methodNames = calls.map((c) => c.method);
    // Should use save/clip/restore for played region
    expect(methodNames).toContain('save');
    expect(methodNames).toContain('clip');
    expect(methodNames).toContain('restore');
  });

  it('should not clip when currentTime is 0', () => {
    const ctx = createMockContext();
    renderWaveform({
      ctx,
      width: 200,
      height: 60,
      peaks: [0.5, 0.8, 0.3],
      duration: 10,
      currentTime: 0,
      color: '#4a90d9',
      playedColor: '#2ecc71',
    });
    const calls = (ctx as unknown as { _calls: Array<{ method: string }> })._calls;
    const methodNames = calls.map((c) => c.method);
    expect(methodNames).not.toContain('save');
    expect(methodNames).not.toContain('clip');
  });
});

// ---------------------------------------------------------------------------
// renderPlaceholder — Unit Tests
// ---------------------------------------------------------------------------

describe('renderPlaceholder', () => {
  it('should clear and draw a centered bar', () => {
    const ctx = createMockContext();
    renderPlaceholder(ctx, 200, 60);
    const calls = (ctx as unknown as { _calls: Array<{ method: string; args: unknown[] }> })._calls;
    const methodNames = calls.map((c) => c.method);
    expect(methodNames[0]).toBe('clearRect');
    expect(methodNames).toContain('set_fillStyle');
    expect(methodNames).toContain('fillRect');
  });
});

// ---------------------------------------------------------------------------
// Click-to-seek mapping — Unit Tests
// ---------------------------------------------------------------------------

describe('click-to-seek mapping', () => {
  it('should compute seek time as (X / W) * D', () => {
    // Simulate the formula used in the component
    const canvasWidth = 500;
    const duration = 120;

    const testCases = [
      { clickX: 0, expected: 0 },
      { clickX: 250, expected: 60 },
      { clickX: 500, expected: 120 },
      { clickX: 125, expected: 30 },
    ];

    for (const { clickX, expected } of testCases) {
      const seekTime = (clickX / canvasWidth) * duration;
      expect(seekTime).toBeCloseTo(expected, 5);
    }
  });

  it('should clamp seek time to [0, duration]', () => {
    const duration = 60;
    const canvasWidth = 300;

    // Negative X (shouldn't happen but guard)
    const neg = Math.max(0, Math.min((-10 / canvasWidth) * duration, duration));
    expect(neg).toBe(0);

    // Beyond width
    const beyond = Math.max(0, Math.min((350 / canvasWidth) * duration, duration));
    expect(beyond).toBe(duration);
  });
});
