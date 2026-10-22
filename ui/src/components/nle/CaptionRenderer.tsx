/**
 * CaptionRenderer — Overlays speaker-identified captions on the video preview viewport.
 *
 * Responsibilities:
 * - Filter active cues based on current playhead time (start <= t < end)
 * - Limit to max 3 simultaneous cues, most recently started at bottom
 * - Format display as "{SPEAKER}: {text}" with no additional transformation
 * - Assign speaker colors from a 6-color WCAG AA palette (4.5:1 contrast ratio)
 * - Cycle colors in speaker-appearance order
 * - Support caption toggle (hide immediately when visible=false)
 * - Handle text wrapping for long captions (no truncation)
 * - Discard invalid cues (start >= end, empty speaker, empty text) with console warnings
 * - Clamp negative start/end values to zero
 */

import React from 'react';
import type { CaptionCue } from './manifestParser';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * 6-color WCAG AA palette — all meet 4.5:1 contrast ratio against white text.
 * Colors are assigned by cycling through the palette in speaker-appearance order.
 */
export const SPEAKER_COLOR_PALETTE = [
  '#1a5276', // dark blue
  '#7b241c', // dark red
  '#1e8449', // dark green
  '#6c3483', // dark purple
  '#784212', // dark brown
  '#1b4f72', // dark teal
] as const;

/** Maximum number of simultaneous cues displayed at once */
const MAX_SIMULTANEOUS_CUES = 3;

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export interface CaptionRendererProps {
  cues: CaptionCue[];
  currentTime: number;
  visible: boolean;
  speakerColors?: Map<string, string>;
}

// ---------------------------------------------------------------------------
// Helper: assignSpeakerColors
// ---------------------------------------------------------------------------

/**
 * Build a speaker→color map from the cues array in order of appearance.
 * Each unique speaker encountered is assigned the next color from the palette,
 * cycling if there are more than 6 unique speakers.
 */
export function assignSpeakerColors(cues: CaptionCue[]): Map<string, string> {
  const colorMap = new Map<string, string>();
  let colorIndex = 0;

  for (const cue of cues) {
    if (cue.speaker && !colorMap.has(cue.speaker)) {
      colorMap.set(cue.speaker, SPEAKER_COLOR_PALETTE[colorIndex % SPEAKER_COLOR_PALETTE.length]);
      colorIndex++;
    }
  }

  return colorMap;
}

// ---------------------------------------------------------------------------
// Helper: validate and filter cues
// ---------------------------------------------------------------------------

interface ProcessedCue {
  start: number;
  end: number;
  speaker: string;
  text: string;
  originalIndex: number;
}

/**
 * Validates cues at render time:
 * - Clamps negative start/end values to zero (with warning)
 * - Discards cues where start >= end after clamping (with warning)
 * - Discards cues with empty speaker or empty text (with warning)
 */
function processAndValidateCues(cues: CaptionCue[]): ProcessedCue[] {
  const valid: ProcessedCue[] = [];

  for (let i = 0; i < cues.length; i++) {
    const cue = cues[i];
    let start = cue.start;
    let end = cue.end;

    // Clamp negative values to zero
    if (start < 0) {
      console.warn(`CaptionRenderer: cue[${i}] has negative start (${start}), clamping to 0`);
      start = 0;
    }
    if (end < 0) {
      console.warn(`CaptionRenderer: cue[${i}] has negative end (${end}), clamping to 0`);
      end = 0;
    }

    // Discard if start >= end after clamping
    if (start >= end) {
      console.warn(`CaptionRenderer: cue[${i}] has start (${start}) >= end (${end}), discarding`);
      continue;
    }

    // Discard if empty speaker
    if (!cue.speaker || cue.speaker.trim() === '') {
      console.warn(`CaptionRenderer: cue[${i}] has empty speaker, discarding`);
      continue;
    }

    // Discard if empty text
    if (!cue.text || cue.text.trim() === '') {
      console.warn(`CaptionRenderer: cue[${i}] has empty text, discarding`);
      continue;
    }

    valid.push({
      start,
      end,
      speaker: cue.speaker,
      text: cue.text,
      originalIndex: i,
    });
  }

  return valid;
}

/**
 * Filter active cues at the given time: start <= t < end.
 * Returns at most MAX_SIMULTANEOUS_CUES, with most recently started at the end (bottom).
 */
function getActiveCues(processedCues: ProcessedCue[], currentTime: number): ProcessedCue[] {
  const active = processedCues.filter(
    (cue) => cue.start <= currentTime && currentTime < cue.end,
  );

  // Sort by start time ascending — most recent start times at the end (bottom of stack)
  active.sort((a, b) => a.start - b.start);

  // If more than MAX_SIMULTANEOUS_CUES, keep only the most recent ones
  if (active.length > MAX_SIMULTANEOUS_CUES) {
    return active.slice(active.length - MAX_SIMULTANEOUS_CUES);
  }

  return active;
}

// ---------------------------------------------------------------------------
// CaptionRenderer Component
// ---------------------------------------------------------------------------

export const CaptionRenderer: React.FC<CaptionRendererProps> = ({
  cues,
  currentTime,
  visible,
  speakerColors,
}) => {
  // When not visible, render nothing (immediate hide)
  if (!visible) {
    return null;
  }

  // Process and validate cues (at render time per spec)
  const processedCues = processAndValidateCues(cues);

  // Compute speaker colors if not provided
  const colors = speakerColors ?? assignSpeakerColors(cues);

  // Get active cues for current time
  const activeCues = getActiveCues(processedCues, currentTime);

  if (activeCues.length === 0) {
    return null;
  }

  return (
    <div
      data-testid="caption-renderer"
      style={{
        position: 'absolute',
        bottom: '8%',
        left: '50%',
        transform: 'translateX(-50%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '4px',
        maxWidth: '90%',
        pointerEvents: 'none',
      }}
    >
      {activeCues.map((cue) => {
        const bgColor = colors.get(cue.speaker) ?? SPEAKER_COLOR_PALETTE[0];

        return (
          <div
            key={`${cue.originalIndex}-${cue.start}`}
            data-testid="caption-cue"
            style={{
              display: 'inline-flex',
              alignItems: 'baseline',
              gap: '6px',
              padding: '4px 8px',
              borderRadius: '4px',
              backgroundColor: bgColor,
              color: '#ffffff',
              fontSize: '14px',
              lineHeight: '1.4',
              wordWrap: 'break-word',
              overflowWrap: 'break-word',
              whiteSpace: 'pre-wrap',
              maxWidth: '100%',
            }}
          >
            <span
              data-testid="caption-speaker"
              style={{
                fontWeight: 'bold',
                whiteSpace: 'nowrap',
              }}
            >
              {cue.speaker}:
            </span>
            <span data-testid="caption-text">
              {cue.text}
            </span>
          </div>
        );
      })}
    </div>
  );
};

export default CaptionRenderer;
