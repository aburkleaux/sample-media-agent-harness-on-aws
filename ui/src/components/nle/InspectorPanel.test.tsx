/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';
import { InspectorPanel, type InspectorPanelProps } from './InspectorPanel';
import type { ManifestClip, CaptionCue } from './manifestParser';

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const sampleClip: ManifestClip = {
  id: 'clip-1',
  name: 'Introduction',
  start: 5.5,
  end: 30.25,
  sourceTrack: 'video-main',
};

const sampleCue: CaptionCue = {
  start: 10.123,
  end: 15.456,
  speaker: 'Alice',
  text: 'Hello, welcome to the presentation.',
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InspectorPanel', () => {
  describe('empty state (Requirement 9.9)', () => {
    it('shows empty-state message when selection is null', () => {
      render(<InspectorPanel selection={null} />);
      expect(
        screen.getByText('Select an item on the timeline to view its details'),
      ).toBeDefined();
    });

    it('renders the INSPECTOR header', () => {
      render(<InspectorPanel selection={null} />);
      expect(screen.getByText('INSPECTOR')).toBeDefined();
    });
  });

  describe('clip selection (Requirement 9.2)', () => {
    it('displays SELECTION section header', () => {
      render(<InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />);
      expect(screen.getByText('SELECTION')).toBeDefined();
    });

    it('displays In timecode (start)', () => {
      render(<InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />);
      // 5.5s = 00:05.500
      expect(screen.getByText('00:05.500')).toBeDefined();
    });

    it('displays Out timecode (end)', () => {
      render(<InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />);
      // 30.25s = 00:30.250
      expect(screen.getByText('00:30.250')).toBeDefined();
    });

    it('displays Duration (end - start)', () => {
      render(<InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />);
      // 30.25 - 5.5 = 24.75s = 00:24.750
      expect(screen.getByText('00:24.750')).toBeDefined();
    });

    it('displays Source Track', () => {
      render(<InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />);
      expect(screen.getByText('video-main')).toBeDefined();
    });

    it('does not show empty-state message', () => {
      render(<InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />);
      expect(
        screen.queryByText('Select an item on the timeline to view its details'),
      ).toBeNull();
    });
  });

  describe('cue selection (Requirement 9.3)', () => {
    it('displays ACTIVE SENTENCE section header', () => {
      render(<InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />);
      expect(screen.getByText('ACTIVE SENTENCE')).toBeDefined();
    });

    it('displays speaker label with badge', () => {
      render(<InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />);
      const badge = screen.getByText('Alice');
      expect(badge).toBeDefined();
      expect(badge.className).toContain('speaker-badge');
    });

    it('displays cue text', () => {
      render(<InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />);
      expect(
        screen.getByText('Hello, welcome to the presentation.'),
      ).toBeDefined();
    });

    it('displays time range with formatted timecodes', () => {
      render(<InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />);
      // 10.123s = 00:10.123, 15.456s = 00:15.456
      expect(screen.getByText('00:10.123 → 00:15.456')).toBeDefined();
    });

    it('does not show empty-state message', () => {
      render(<InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />);
      expect(
        screen.queryByText('Select an item on the timeline to view its details'),
      ).toBeNull();
    });
  });

  describe('speaker badge color', () => {
    it('applies a background color from the WCAG-compliant palette', () => {
      const { container } = render(
        <InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />,
      );
      const badge = container.querySelector('.inspector-panel__speaker-badge');
      expect(badge).not.toBeNull();
      const style = (badge as HTMLElement).style;
      expect(style.backgroundColor).toBeTruthy();
      expect(style.color).toBe('rgb(255, 255, 255)');
    });
  });

  describe('responsiveness to selection changes', () => {
    it('updates from null to clip selection', () => {
      const { rerender } = render(<InspectorPanel selection={null} />);
      expect(
        screen.getByText('Select an item on the timeline to view its details'),
      ).toBeDefined();

      rerender(
        <InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />,
      );
      expect(screen.getByText('SELECTION')).toBeDefined();
      expect(
        screen.queryByText('Select an item on the timeline to view its details'),
      ).toBeNull();
    });

    it('updates from clip to cue selection', () => {
      const { rerender } = render(
        <InspectorPanel selection={{ type: 'clip', clip: sampleClip }} />,
      );
      expect(screen.getByText('SELECTION')).toBeDefined();

      rerender(
        <InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />,
      );
      expect(screen.getByText('ACTIVE SENTENCE')).toBeDefined();
      expect(screen.queryByText('SELECTION')).toBeNull();
    });

    it('updates from cue to null selection', () => {
      const { rerender } = render(
        <InspectorPanel selection={{ type: 'cue', cue: sampleCue }} />,
      );
      expect(screen.getByText('ACTIVE SENTENCE')).toBeDefined();

      rerender(<InspectorPanel selection={null} />);
      expect(
        screen.getByText('Select an item on the timeline to view its details'),
      ).toBeDefined();
    });
  });
});
