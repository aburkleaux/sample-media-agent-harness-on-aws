import { describe, it, expect } from 'vitest';
import { formatTimecode, parseTimecode } from './formatTimecode';

// ---------------------------------------------------------------------------
// formatTimecode
// ---------------------------------------------------------------------------

describe('formatTimecode', () => {
  it('formats 0 as "00:00.000"', () => {
    expect(formatTimecode(0)).toBe('00:00.000');
  });

  it('formats 63.833 as "01:03.833"', () => {
    expect(formatTimecode(63.833)).toBe('01:03.833');
  });

  it('formats 120.5 as "02:00.500"', () => {
    expect(formatTimecode(120.5)).toBe('02:00.500');
  });

  it('formats 3661.123 as "61:01.123" (minutes can exceed 59)', () => {
    expect(formatTimecode(3661.123)).toBe('61:01.123');
  });

  it('formats sub-second values correctly', () => {
    expect(formatTimecode(0.5)).toBe('00:00.500');
    expect(formatTimecode(0.001)).toBe('00:00.001');
    expect(formatTimecode(0.999)).toBe('00:00.999');
  });

  it('formats exact minute boundaries', () => {
    expect(formatTimecode(60)).toBe('01:00.000');
    expect(formatTimecode(120)).toBe('02:00.000');
  });

  it('pads minutes to at least 2 digits', () => {
    expect(formatTimecode(5)).toBe('00:05.000');
    expect(formatTimecode(65)).toBe('01:05.000');
  });

  it('handles large minute values (> 99)', () => {
    // 100 minutes = 6000 seconds
    expect(formatTimecode(6000)).toBe('100:00.000');
  });

  it('clamps negative values to zero', () => {
    expect(formatTimecode(-1)).toBe('00:00.000');
    expect(formatTimecode(-100.5)).toBe('00:00.000');
  });

  it('handles very small positive values', () => {
    expect(formatTimecode(0.0005)).toBe('00:00.001'); // rounds up
    expect(formatTimecode(0.0004)).toBe('00:00.000'); // rounds down
  });
});

// ---------------------------------------------------------------------------
// parseTimecode
// ---------------------------------------------------------------------------

describe('parseTimecode', () => {
  it('parses "00:00.000" to 0', () => {
    expect(parseTimecode('00:00.000')).toBe(0);
  });

  it('parses "01:03.833" to 63.833', () => {
    expect(parseTimecode('01:03.833')).toBe(63.833);
  });

  it('parses "02:00.500" to 120.5', () => {
    expect(parseTimecode('02:00.500')).toBe(120.5);
  });

  it('parses "61:01.123" to 3661.123', () => {
    expect(parseTimecode('61:01.123')).toBe(3661.123);
  });

  it('parses large minute values', () => {
    expect(parseTimecode('100:00.000')).toBe(6000);
  });

  it('throws on invalid format — missing milliseconds', () => {
    expect(() => parseTimecode('01:03')).toThrow('Invalid timecode format');
  });

  it('throws on invalid format — wrong separator', () => {
    expect(() => parseTimecode('01:03:833')).toThrow('Invalid timecode format');
  });

  it('throws on invalid format — single-digit seconds', () => {
    expect(() => parseTimecode('01:3.833')).toThrow('Invalid timecode format');
  });

  it('throws on invalid format — two-digit milliseconds', () => {
    expect(() => parseTimecode('01:03.83')).toThrow('Invalid timecode format');
  });

  it('throws on invalid format — empty string', () => {
    expect(() => parseTimecode('')).toThrow('Invalid timecode format');
  });

  it('throws on seconds exceeding 59', () => {
    expect(() => parseTimecode('01:60.000')).toThrow('Invalid seconds value');
  });
});

// ---------------------------------------------------------------------------
// Round-trip property
// ---------------------------------------------------------------------------

describe('round-trip', () => {
  it('parseTimecode(formatTimecode(t)) ≈ t within ±1ms for integer seconds', () => {
    for (const t of [0, 1, 30, 59, 60, 120, 3600, 7200]) {
      const roundTripped = parseTimecode(formatTimecode(t));
      expect(Math.abs(roundTripped - t)).toBeLessThanOrEqual(0.001);
    }
  });

  it('parseTimecode(formatTimecode(t)) ≈ t within ±1ms for fractional seconds', () => {
    for (const t of [0.001, 0.5, 1.234, 63.833, 120.5, 3661.123]) {
      const roundTripped = parseTimecode(formatTimecode(t));
      expect(Math.abs(roundTripped - t)).toBeLessThanOrEqual(0.001);
    }
  });

  it('round-trips boundary values correctly', () => {
    const values = [0, 0.001, 59.999, 60, 60.001, 3599.999, 3600];
    for (const t of values) {
      const roundTripped = parseTimecode(formatTimecode(t));
      expect(Math.abs(roundTripped - t)).toBeLessThanOrEqual(0.001);
    }
  });
});
