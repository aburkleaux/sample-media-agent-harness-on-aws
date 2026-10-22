/**
 * WaveformVisualizer — Renders pre-computed peak data as a Canvas-based waveform.
 *
 * Displays normalized amplitude peaks as a filled waveform shape within an audio
 * track lane. Highlights the played region (0 to currentTime) with a distinct fill
 * color from the unplayed region. Supports click-to-seek and emits `scrub`
 * InteractionEvents.
 *
 * Uses `<canvas>` for rendering (not SVG) to handle thousands of peak data points
 * at 60fps without DOM thrashing.
 *
 * Requirements: 4.1, 4.2, 4.3, 4.5, 4.6, 4.7, 8.6
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { InteractionEvent } from '../renderer/types';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Default samples per second used for expected peak count calculation */
export const DEFAULT_SAMPLES_PER_SECOND = 100;

/** Default waveform color (unplayed region) */
const DEFAULT_COLOR = '#4a90d9';

/** Default played region color */
const DEFAULT_PLAYED_COLOR = '#2ecc71';

/** Placeholder bar color when no peaks are available */
const PLACEHOLDER_COLOR = '#666666';

/** Placeholder tooltip text */
const PLACEHOLDER_TOOLTIP = 'No waveform data available';

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export interface WaveformVisualizerProps {
  peaks: number[];            // Normalized 0.0-1.0 amplitude values
  duration: number;           // Track duration in seconds
  currentTime: number;        // Current playhead position
  zoomLevel: number;          // Pixels per second
  scrollOffset: number;
  color?: string;             // Waveform color (default from theme)
  playedColor?: string;       // Color for played region
  onSeek: (time: number) => void;
  onInteraction?: (event: InteractionEvent) => void;
}

// ---------------------------------------------------------------------------
// Resampling: Linear Interpolation
// ---------------------------------------------------------------------------

/**
 * Resample a peak data array to a target length using linear interpolation.
 *
 * For any input array of length N and target count M (where M ≠ N), produces
 * an output array of exactly length M where all values are bounded within
 * the minimum and maximum of the input array.
 *
 * Property 7: Peak Data Resampling
 */
export function resamplePeaks(peaks: number[], targetLength: number): number[] {
  if (targetLength <= 0) return [];
  if (peaks.length === 0) return new Array(targetLength).fill(0);
  if (peaks.length === targetLength) return [...peaks];
  if (targetLength === 1) {
    // Single sample: average of all peaks
    const sum = peaks.reduce((a, b) => a + b, 0);
    return [sum / peaks.length];
  }

  const result: number[] = new Array(targetLength);
  const ratio = (peaks.length - 1) / (targetLength - 1);

  for (let i = 0; i < targetLength; i++) {
    const srcIndex = i * ratio;
    const lower = Math.floor(srcIndex);
    const upper = Math.min(lower + 1, peaks.length - 1);
    const fraction = srcIndex - lower;
    result[i] = peaks[lower] * (1 - fraction) + peaks[upper] * fraction;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Rendering logic (extracted for testability)
// ---------------------------------------------------------------------------

export interface RenderParams {
  ctx: CanvasRenderingContext2D;
  width: number;
  height: number;
  peaks: number[];
  duration: number;
  currentTime: number;
  color: string;
  playedColor: string;
}

/**
 * Draw the waveform onto a canvas context.
 * Renders the unplayed region first, then the played region on top.
 */
export function renderWaveform(params: RenderParams): void {
  const { ctx, width, height, peaks, duration, currentTime, color, playedColor } = params;

  ctx.clearRect(0, 0, width, height);

  if (peaks.length === 0 || duration <= 0) return;

  const playedFraction = Math.max(0, Math.min(currentTime / duration, 1));
  const playedX = playedFraction * width;

  // Draw unplayed region (full waveform in base color)
  drawWaveformRegion(ctx, peaks, 0, width, height, color);

  // Overdraw played region with played color
  if (playedX > 0) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, playedX, height);
    ctx.clip();
    drawWaveformRegion(ctx, peaks, 0, width, height, playedColor);
    ctx.restore();
  }
}

/**
 * Draw a filled waveform region from startX to endX.
 * Peaks are mapped to track lane height: 0.0 = 0% height, 1.0 = 100% height.
 * The waveform is drawn mirrored around the vertical center.
 */
function drawWaveformRegion(
  ctx: CanvasRenderingContext2D,
  peaks: number[],
  startX: number,
  endX: number,
  height: number,
  fillColor: string,
): void {
  const regionWidth = endX - startX;
  if (regionWidth <= 0 || peaks.length === 0) return;

  const centerY = height / 2;
  const peaksPerPixel = peaks.length / regionWidth;

  ctx.fillStyle = fillColor;
  ctx.beginPath();

  // Draw top half (going right)
  ctx.moveTo(startX, centerY);
  for (let x = 0; x < regionWidth; x++) {
    const peakIdx = Math.min(Math.floor(x * peaksPerPixel), peaks.length - 1);
    const amplitude = Math.max(0, Math.min(1, peaks[peakIdx]));
    const barHeight = amplitude * centerY;
    ctx.lineTo(startX + x, centerY - barHeight);
  }

  // Draw bottom half (going left, mirrored)
  for (let x = regionWidth - 1; x >= 0; x--) {
    const peakIdx = Math.min(Math.floor(x * peaksPerPixel), peaks.length - 1);
    const amplitude = Math.max(0, Math.min(1, peaks[peakIdx]));
    const barHeight = amplitude * centerY;
    ctx.lineTo(startX + x, centerY + barHeight);
  }

  ctx.closePath();
  ctx.fill();
}

/**
 * Draw a flat placeholder bar for missing/empty peaks.
 */
export function renderPlaceholder(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = PLACEHOLDER_COLOR;

  const barHeight = Math.max(2, height * 0.1);
  const y = (height - barHeight) / 2;
  ctx.fillRect(0, y, width, barHeight);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function WaveformVisualizer(props: WaveformVisualizerProps): React.JSX.Element {
  const {
    peaks,
    duration,
    currentTime,
    zoomLevel,
    scrollOffset,
    color = DEFAULT_COLOR,
    playedColor = DEFAULT_PLAYED_COLOR,
    onSeek,
    onInteraction,
  } = props;

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // #1 (sync): the waveform now FILLS its lane (fit-to-container) so it shares the SAME horizontal
  // time scale as the ruler / markers / speaker lanes (all percent-based), instead of its old
  // duration×zoomLevel pixel width that showed only a sliver and never lined up. We measure the lane
  // width and draw the canvas to match; a shared zoom widens the lane (and thus every lane) together.
  const [measuredWidth, setMeasuredWidth] = useState(0);
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0] && entries[0].contentRect ? entries[0].contentRect.width : 0;
      if (typeof w === 'number' && w > 0) setMeasuredWidth(Math.round(w));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Determine if we have valid peak data
  const hasPeaks = peaks && peaks.length > 0;

  // Canvas width = the measured lane width (fit-to-container, shared scale). Before the first measure,
  // fall back to a capped duration-based width. A browser <canvas> has a hard max dimension
  // (~32767px); the cap keeps long tracks from silently failing to allocate (they'd paint nothing).
  const MAX_CANVAS_WIDTH = 12000;
  const fallbackWidth = Math.max(1, Math.min(Math.ceil(duration * zoomLevel), MAX_CANVAS_WIDTH));
  const canvasWidth = measuredWidth > 0 ? Math.min(measuredWidth, MAX_CANVAS_WIDTH) : fallbackWidth;

  // Resample peaks if length doesn't match expected count
  const expectedPeakCount = Math.ceil(duration * DEFAULT_SAMPLES_PER_SECOND);
  const displayPeaks = hasPeaks
    ? peaks.length !== expectedPeakCount
      ? resamplePeaks(peaks, expectedPeakCount)
      : peaks
    : [];

  // Draw on canvas whenever relevant props change
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Set canvas resolution for crisp rendering. Clamp the effective DPR so the bitmap never exceeds
    // the browser's max canvas dimension (~32767px) even on a 2x/3x display with a wide track.
    const rawDpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
    const displayWidth = canvasWidth;
    const displayHeight = canvas.clientHeight || 60;
    const dpr = Math.max(1, Math.min(rawDpr, 32000 / Math.max(1, displayWidth)));

    canvas.width = Math.floor(displayWidth * dpr);
    canvas.height = Math.floor(displayHeight * dpr);
    ctx.scale(dpr, dpr);

    if (!hasPeaks) {
      renderPlaceholder(ctx, displayWidth, displayHeight);
    } else {
      renderWaveform({
        ctx,
        width: displayWidth,
        height: displayHeight,
        peaks: displayPeaks,
        duration,
        currentTime,
        color,
        playedColor,
      });
    }
  }, [displayPeaks, duration, currentTime, canvasWidth, color, playedColor, hasPeaks]);

  // Click-to-seek handler
  const handleClick = useCallback(
    (event: React.MouseEvent<HTMLCanvasElement>) => {
      if (!hasPeaks || duration <= 0) return;

      const canvas = canvasRef.current;
      if (!canvas) return;

      const rect = canvas.getBoundingClientRect();
      const clickX = event.clientX - rect.left;
      const canvasDisplayWidth = rect.width;

      // Map X position to time: seekTime = (clickX / canvasWidth) * duration
      const seekTime = (clickX / canvasDisplayWidth) * duration;
      const clampedTime = Math.max(0, Math.min(seekTime, duration));

      onSeek(clampedTime);

      // Emit scrub InteractionEvent
      if (onInteraction) {
        const scrubEvent: InteractionEvent = {
          type: 'scrub',
          target: 'waveform',
          data: { time: clampedTime },
          timestamp: Date.now(),
        };
        onInteraction(scrubEvent);
      }
    },
    [hasPeaks, duration, onSeek, onInteraction],
  );

  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative',
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        transform: scrollOffset ? `translateX(-${scrollOffset}px)` : undefined,
      }}
      title={!hasPeaks ? PLACEHOLDER_TOOLTIP : undefined}
    >
      <canvas
        ref={canvasRef}
        onClick={handleClick}
        style={{
          width: '100%',
          height: '100%',
          display: 'block',
          cursor: hasPeaks ? 'pointer' : 'default',
        }}
        aria-label={hasPeaks ? 'Audio waveform visualization' : PLACEHOLDER_TOOLTIP}
        role="img"
      />
    </div>
  );
}

export default WaveformVisualizer;
