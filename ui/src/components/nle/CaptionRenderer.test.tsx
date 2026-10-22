/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';
import {
  CaptionRenderer,
  assignSpeakerColors,
  SPEAKER_COLOR_PALETTE,
} from './CaptionRenderer';
import type { CaptionCue } from './manifestParser';

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

function makeCue(overrides: Partial<CaptionCue> = {}): CaptionCue {
  return {
    start: 0,
    end: 5,
    speaker: 'Alice',
    text: 'Hello world',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// assignSpeakerColors
// ---------------------------------------------------------------------------

describe('assignSpeakerColors', () => {
  it('assigns colors in appearance order', () => {
    const cues: CaptionCue[] = [
      makeCue({ speaker: 'Alice' }),
      makeCue({ speaker: 'Bob' }),
      makeCue({ speaker: 'Alice' }),
      makeCue({ speaker: 'Carol' }),
    ];

    const colors = assignSpeakerColors(cues);

    expect(colors.get('Alice')).toBe(SPEAKER_COLOR_PALETTE[0]);
    expect(colors.get('Bob')).toBe(SPEAKER_COLOR_PALETTE[1]);
    expect(colors.get('Carol')).toBe(SPEAKER_COLOR_PALETTE[2]);
  });

  it('cycles colors when more than 6 speakers', () => {
    const speakers = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
    const cues = speakers.map((s) => makeCue({ speaker: s }));
    const colors = assignSpeakerColors(cues);

    expect(colors.get('G')).toBe(SPEAKER_COLOR_PALETTE[0]); // cycles back
    expect(colors.get('H')).toBe(SPEAKER_COLOR_PALETTE[1]);
  });

  it('returns empty map for empty cues array', () => {
    const colors = assignSpeakerColors([]);
    expect(colors.size).toBe(0);
  });

  it('skips cues with empty speaker for color assignment', () => {
    const cues: CaptionCue[] = [
      makeCue({ speaker: '' }),
      makeCue({ speaker: 'Bob' }),
    ];
    const colors = assignSpeakerColors(cues);
    expect(colors.has('')).toBe(false);
    expect(colors.get('Bob')).toBe(SPEAKER_COLOR_PALETTE[0]);
  });
});

// ---------------------------------------------------------------------------
// CaptionRenderer component
// ---------------------------------------------------------------------------

describe('CaptionRenderer', () => {
  let warnSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    warnSpy.mockRestore();
  });

  describe('visibility toggle', () => {
    it('renders nothing when visible is false', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ start: 0, end: 10 })]}
          currentTime={5}
          visible={false}
        />,
      );
      expect(container.innerHTML).toBe('');
    });

    it('renders captions when visible is true and cues are active', () => {
      render(
        <CaptionRenderer
          cues={[makeCue({ start: 0, end: 10 })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(screen.getByTestId('caption-renderer')).toBeDefined();
    });
  });

  describe('active cue filtering', () => {
    it('displays cue when start <= t < end', () => {
      render(
        <CaptionRenderer
          cues={[makeCue({ start: 2, end: 8, text: 'Active cue' })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(screen.getByText('Active cue')).toBeDefined();
    });

    it('does not display cue when t < start', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ start: 5, end: 10, text: 'Future cue' })]}
          currentTime={3}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
    });

    it('does not display cue when t >= end', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ start: 0, end: 5, text: 'Past cue' })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
    });

    it('displays cue at exact start time (inclusive)', () => {
      render(
        <CaptionRenderer
          cues={[makeCue({ start: 3, end: 7, text: 'At start' })]}
          currentTime={3}
          visible={true}
        />,
      );
      expect(screen.getByText('At start')).toBeDefined();
    });
  });

  describe('max 3 simultaneous cues', () => {
    it('limits display to 3 cues when more overlap', () => {
      const cues = [
        makeCue({ start: 0, end: 10, speaker: 'A', text: 'First' }),
        makeCue({ start: 1, end: 10, speaker: 'B', text: 'Second' }),
        makeCue({ start: 2, end: 10, speaker: 'C', text: 'Third' }),
        makeCue({ start: 3, end: 10, speaker: 'D', text: 'Fourth' }),
      ];

      render(
        <CaptionRenderer cues={cues} currentTime={5} visible={true} />,
      );

      const cueElements = screen.getAllByTestId('caption-cue');
      expect(cueElements).toHaveLength(3);
    });

    it('keeps most recently started cues (bottom of stack)', () => {
      const cues = [
        makeCue({ start: 0, end: 10, speaker: 'A', text: 'Oldest' }),
        makeCue({ start: 1, end: 10, speaker: 'B', text: 'Middle' }),
        makeCue({ start: 2, end: 10, speaker: 'C', text: 'Newer' }),
        makeCue({ start: 3, end: 10, speaker: 'D', text: 'Newest' }),
      ];

      render(
        <CaptionRenderer cues={cues} currentTime={5} visible={true} />,
      );

      // The oldest cue should be hidden
      expect(screen.queryByText('Oldest')).toBeNull();
      // The 3 most recent should be visible
      expect(screen.getByText('Middle')).toBeDefined();
      expect(screen.getByText('Newer')).toBeDefined();
      expect(screen.getByText('Newest')).toBeDefined();
    });
  });

  describe('speaker prefix formatting', () => {
    it('displays "{SPEAKER}: {text}" format', () => {
      render(
        <CaptionRenderer
          cues={[makeCue({ speaker: 'Alice', text: 'Hello world' })]}
          currentTime={2}
          visible={true}
        />,
      );
      expect(screen.getByText('Alice:')).toBeDefined();
      expect(screen.getByText('Hello world')).toBeDefined();
    });
  });

  describe('invalid cue handling', () => {
    it('discards cues with start >= end', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ start: 5, end: 5, text: 'Invalid' })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringContaining('start (5) >= end (5)'),
      );
    });

    it('discards cues with empty speaker', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ speaker: '', start: 0, end: 10 })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringContaining('empty speaker'),
      );
    });

    it('discards cues with empty text', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ text: '', start: 0, end: 10 })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringContaining('empty text'),
      );
    });

    it('clamps negative start to zero', () => {
      render(
        <CaptionRenderer
          cues={[makeCue({ start: -5, end: 10, text: 'Clamped' })]}
          currentTime={0}
          visible={true}
        />,
      );
      expect(screen.getByText('Clamped')).toBeDefined();
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringContaining('negative start'),
      );
    });

    it('clamps negative end to zero and discards if start >= end', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ start: 0, end: -1 })]}
          currentTime={0}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringContaining('negative end'),
      );
    });
  });

  describe('speaker colors', () => {
    it('applies colors from the palette to speaker badges', () => {
      const cues = [
        makeCue({ start: 0, end: 10, speaker: 'Alice', text: 'Hi' }),
        makeCue({ start: 0, end: 10, speaker: 'Bob', text: 'Hey' }),
      ];

      render(
        <CaptionRenderer cues={cues} currentTime={5} visible={true} />,
      );

      const cueElements = screen.getAllByTestId('caption-cue');
      expect(cueElements[0].style.backgroundColor).toBe(
        'rgb(26, 82, 118)', // #1a5276
      );
      expect(cueElements[1].style.backgroundColor).toBe(
        'rgb(123, 36, 28)', // #7b241c
      );
    });

    it('uses speakerColors prop when provided', () => {
      const customColors = new Map([['Alice', '#ff0000']]);
      const cues = [makeCue({ start: 0, end: 10, speaker: 'Alice', text: 'Hi' })];

      render(
        <CaptionRenderer
          cues={cues}
          currentTime={5}
          visible={true}
          speakerColors={customColors}
        />,
      );

      const cueElement = screen.getByTestId('caption-cue');
      expect(cueElement.style.backgroundColor).toBe('rgb(255, 0, 0)');
    });
  });

  describe('text wrapping', () => {
    it('applies word-wrap styles for long text', () => {
      const longText = 'A'.repeat(300);
      render(
        <CaptionRenderer
          cues={[makeCue({ start: 0, end: 10, text: longText })]}
          currentTime={5}
          visible={true}
        />,
      );

      const cueElement = screen.getByTestId('caption-cue');
      expect(cueElement.style.overflowWrap).toBe('break-word');
      expect(cueElement.style.whiteSpace).toBe('pre-wrap');
    });
  });

  describe('renders nothing when no active cues', () => {
    it('returns null when all cues are outside current time', () => {
      const { container } = render(
        <CaptionRenderer
          cues={[makeCue({ start: 10, end: 20 })]}
          currentTime={5}
          visible={true}
        />,
      );
      expect(container.innerHTML).toBe('');
    });

    it('returns null when cues array is empty', () => {
      const { container } = render(
        <CaptionRenderer cues={[]} currentTime={5} visible={true} />,
      );
      expect(container.innerHTML).toBe('');
    });
  });
});
