/**
 * Timecode formatting utility for the Clip Manager.
 *
 * Provides:
 * - `formatTimecode(seconds)` — converts a time in seconds to `MM:SS.mmm` format
 * - `parseTimecode(timecode)` — parses `MM:SS.mmm` back to seconds
 *
 * The format is always `MM:SS.mmm` where:
 * - MM = total minutes (zero-padded to at least 2 digits, can exceed 59 for long media)
 * - SS = seconds within minute (00-59, zero-padded)
 * - mmm = milliseconds (000-999, zero-padded)
 *
 * Round-trip property: `parseTimecode(formatTimecode(t))` returns a value within ±0.001 of t.
 */

/**
 * Format a time value in seconds to `MM:SS.mmm` timecode string.
 *
 * @param seconds - Non-negative time value in seconds
 * @returns Formatted timecode string (e.g., "01:03.833")
 */
export function formatTimecode(seconds: number): string {
  // Clamp negative values to zero
  const clamped = Math.max(0, seconds);

  // Round to millisecond precision to avoid floating-point drift
  const totalMs = Math.round(clamped * 1000);

  const totalSeconds = Math.floor(totalMs / 1000);
  const ms = totalMs % 1000;

  const minutes = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;

  const mm = String(minutes).padStart(2, '0');
  const ss = String(secs).padStart(2, '0');
  const mmm = String(ms).padStart(3, '0');

  return `${mm}:${ss}.${mmm}`;
}

/**
 * Parse a `MM:SS.mmm` timecode string back to seconds.
 *
 * @param timecode - Timecode string in `MM:SS.mmm` format
 * @returns Time value in seconds
 * @throws Error if timecode format is invalid
 */
export function parseTimecode(timecode: string): number {
  const match = timecode.match(/^(\d{2,}):(\d{2})\.(\d{3})$/);
  if (!match) {
    throw new Error(
      `Invalid timecode format: "${timecode}". Expected MM:SS.mmm (e.g., "01:03.833")`
    );
  }

  const minutes = parseInt(match[1], 10);
  const seconds = parseInt(match[2], 10);
  const milliseconds = parseInt(match[3], 10);

  if (seconds > 59) {
    throw new Error(
      `Invalid seconds value ${seconds} in timecode "${timecode}". Seconds must be 00-59.`
    );
  }

  if (milliseconds > 999) {
    throw new Error(
      `Invalid milliseconds value ${milliseconds} in timecode "${timecode}". Milliseconds must be 000-999.`
    );
  }

  return minutes * 60 + seconds + milliseconds / 1000;
}
