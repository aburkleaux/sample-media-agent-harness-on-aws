/**
 * Frame-accurate time for the video timeline: the frame rate of the media, the frame under a time, and the
 * editor's timecode `HH:MM:SS:FF` (the last pair is frames; non-drop-frame — 29.97 counts 30 nominal frames a
 * second, as NDF timecode does).
 *
 * The frame rate is MEASURED from the media in the browser (`requestVideoFrameCallback` reports each presented
 * frame's media time), so it needs no manifest field and works for any source. Until it is known — or where the
 * browser cannot measure it — callers fall back to `formatTimecode`'s milliseconds.
 */

/** The rates a measurement snaps to (a measured 23.98 is 24000/1001, not "23.98"). */
export const STANDARD_FPS = [23.976, 24, 25, 29.97, 30, 47.952, 48, 50, 59.94, 60] as const;

/** The standard rate within 3% of a measured one, else the measurement rounded to 0.01 (`null` for nonsense). */
export function snapFps(measured: number): number | null {
  if (!Number.isFinite(measured) || measured < 1 || measured > 240) return null;
  let best: number | null = null;
  let bestErr = 0.03;
  for (const s of STANDARD_FPS) {
    const err = Math.abs(measured - s) / s;
    if (err < bestErr) { bestErr = err; best = s; }
  }
  return best ?? Math.round(measured * 100) / 100;
}

/** The frame rate from a run of presented-frame media times: the median gap, snapped. `null` with too few. */
export function fpsFromFrameTimes(times: readonly number[]): number | null {
  const gaps: number[] = [];
  for (let i = 1; i < times.length; i++) {
    const g = times[i] - times[i - 1];
    if (g > 0.001 && g < 1) gaps.push(g);
  }
  if (gaps.length < 4) return null;
  gaps.sort((a, b) => a - b);
  return snapFps(1 / gaps[Math.floor(gaps.length / 2)]);
}

/** Nominal frames per timecode second: 30 for 29.97, 24 for 23.976 (non-drop-frame counting). */
const nominal = (fps: number) => Math.round(fps);

/** The frame (0-based) shown at `seconds`. A hair of epsilon keeps a frame-start time on its own frame. */
export function frameAt(seconds: number, fps: number): number {
  return Math.max(0, Math.floor(seconds * fps + 1e-4));
}

/** The start time of the frame under `seconds`: marks and seeks land exactly on a frame. */
export function snapToFrame(seconds: number, fps: number | null | undefined): number {
  if (!fps) return seconds;
  return frameAt(seconds, fps) / fps;
}

/** `HH:MM:SS:FF` for the frame at `seconds`. */
export function formatFrameTimecode(seconds: number, fps: number): string {
  const n = nominal(fps);
  const f = frameAt(seconds, fps);
  // Count in nominal frames (NDF): frame f of a 29.97 file is f/30 timecode-seconds.
  const totalSec = Math.floor(f / n);
  const ff = f % n;
  const p2 = (x: number) => String(x).padStart(2, '0');
  return `${p2(Math.floor(totalSec / 3600))}:${p2(Math.floor(totalSec / 60) % 60)}:${p2(totalSec % 60)}:${p2(ff)}`;
}
