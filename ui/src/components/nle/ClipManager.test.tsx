/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ClipManager } from './ClipManager';
import type { ManifestClip, ManifestTrack } from './manifestParser';

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const mockTracks: ManifestTrack[] = [
  { id: 'track-1', type: 'video', src: 'https://example.com/video.mp4', start: 0, end: 120 },
  { id: 'track-2', type: 'audio', src: 'https://example.com/audio.mp3', start: 0, end: 120 },
];

const mockClips: ManifestClip[] = [
  { id: 'clip-1', name: 'Clip 01', start: 0, end: 63.833, sourceTrack: 'track-1' },
  { id: 'clip-2', name: 'Clip 02', start: 63.833, end: 100, sourceTrack: 'track-2' },
];

const defaultProps = {
  clips: mockClips,
  tracks: mockTracks,
  onSeek: vi.fn(),
  onOpenRender: vi.fn(),
  onSelect: vi.fn(),
  selectedClipId: null,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ClipManager', () => {
  describe('Rendering clips', () => {
    it('renders clips with name, start/end timecodes, and duration', () => {
      render(<ClipManager {...defaultProps} />);

      expect(screen.getByText('Clip 01')).toBeInTheDocument();
      expect(screen.getByText('Clip 02')).toBeInTheDocument();
      // Timecodes: 00:00.000 → 01:03.833 for clip 1
      expect(screen.getByText('00:00.000 → 01:03.833')).toBeInTheDocument();
      // Duration for clip 1: 63.833s = 01:03.833
      expect(screen.getAllByText('01:03.833').length).toBeGreaterThanOrEqual(1);
    });

    it('displays clips in manifest array order', () => {
      render(<ClipManager {...defaultProps} />);
      const items = screen.getAllByRole('listitem');
      expect(items).toHaveLength(2);
      expect(items[0]).toHaveTextContent('Clip 01');
      expect(items[1]).toHaveTextContent('Clip 02');
    });

    it('highlights selected clip', () => {
      render(<ClipManager {...defaultProps} selectedClipId="clip-1" />);
      const row = screen.getByTestId('clip-row-clip-1');
      expect(row.style.borderLeft).toContain('3px solid');
    });
  });

  describe('Click-to-seek', () => {
    it('calls onSeek with clip start time on click', () => {
      const onSeek = vi.fn();
      const onSelect = vi.fn();
      render(<ClipManager {...defaultProps} onSeek={onSeek} onSelect={onSelect} />);

      fireEvent.click(screen.getByTestId('clip-row-clip-1'));
      expect(onSeek).toHaveBeenCalledWith(0);
      expect(onSelect).toHaveBeenCalledWith('clip-1');
    });

    it('calls onSeek with correct start for second clip', () => {
      const onSeek = vi.fn();
      const onSelect = vi.fn();
      render(<ClipManager {...defaultProps} onSeek={onSeek} onSelect={onSelect} />);

      fireEvent.click(screen.getByTestId('clip-row-clip-2'));
      expect(onSeek).toHaveBeenCalledWith(63.833);
      expect(onSelect).toHaveBeenCalledWith('clip-2');
    });
  });

  describe('Dangling sourceTrack references', () => {
    it('shows warning icon and disables seek for clips with invalid sourceTrack', () => {
      const danglingClip: ManifestClip = {
        id: 'clip-dangling',
        name: 'Dangling Clip',
        start: 10,
        end: 20,
        sourceTrack: 'non-existent-track',
      };
      const onSeek = vi.fn();
      const onSelect = vi.fn();

      render(
        <ClipManager
          {...defaultProps}
          clips={[danglingClip]}
          onSeek={onSeek}
          onSelect={onSelect}
        />,
      );

      // Warning icon should be present
      expect(screen.getByLabelText('Warning: source track not found')).toBeInTheDocument();

      // Click should NOT trigger seek
      fireEvent.click(screen.getByTestId('clip-row-clip-dangling'));
      expect(onSeek).not.toHaveBeenCalled();
      expect(onSelect).not.toHaveBeenCalled();
    });

    it('marks dangling clip row as aria-disabled', () => {
      const danglingClip: ManifestClip = {
        id: 'clip-dangling',
        name: 'Dangling Clip',
        start: 10,
        end: 20,
        sourceTrack: 'non-existent-track',
      };

      render(<ClipManager {...defaultProps} clips={[danglingClip]} />);
      const row = screen.getByTestId('clip-row-clip-dangling');
      expect(row).toHaveAttribute('aria-disabled', 'true');
    });
  });

  describe('Open Render action', () => {
    it('calls onOpenRender with clip data when button is clicked', () => {
      const onOpenRender = vi.fn();
      render(<ClipManager {...defaultProps} onOpenRender={onOpenRender} />);

      const buttons = screen.getAllByTitle('Open Render');
      fireEvent.click(buttons[0]);

      expect(onOpenRender).toHaveBeenCalledWith(mockClips[0]);
    });

    it('does not trigger seek when Open Render button is clicked', () => {
      const onSeek = vi.fn();
      const onOpenRender = vi.fn();
      render(<ClipManager {...defaultProps} onSeek={onSeek} onOpenRender={onOpenRender} />);

      const buttons = screen.getAllByTitle('Open Render');
      fireEvent.click(buttons[0]);

      expect(onOpenRender).toHaveBeenCalled();
      expect(onSeek).not.toHaveBeenCalled();
    });
  });

  describe('Empty state', () => {
    it('displays empty-state message when clips array is empty', () => {
      render(<ClipManager {...defaultProps} clips={[]} />);
      expect(screen.getByText('No clips available')).toBeInTheDocument();
    });

    it('displays empty-state when all clips are invalid', () => {
      const invalidClips: ManifestClip[] = [
        { id: 'inv-1', name: 'Invalid', start: -1, end: 5, sourceTrack: 'track-1' },
        { id: 'inv-2', name: 'Invalid', start: 10, end: 5, sourceTrack: 'track-1' },
      ];
      render(<ClipManager {...defaultProps} clips={invalidClips} />);
      expect(screen.getByText('No clips available')).toBeInTheDocument();
    });
  });

  describe('Invalid clip filtering', () => {
    it('omits clips with negative start', () => {
      const clips: ManifestClip[] = [
        { id: 'valid', name: 'Valid', start: 0, end: 10, sourceTrack: 'track-1' },
        { id: 'neg-start', name: 'Neg Start', start: -5, end: 10, sourceTrack: 'track-1' },
      ];
      render(<ClipManager {...defaultProps} clips={clips} />);
      expect(screen.getByText('Valid')).toBeInTheDocument();
      expect(screen.queryByText('Neg Start')).not.toBeInTheDocument();
    });

    it('omits clips where end <= start', () => {
      const clips: ManifestClip[] = [
        { id: 'valid', name: 'Valid', start: 5, end: 10, sourceTrack: 'track-1' },
        { id: 'equal', name: 'Equal', start: 5, end: 5, sourceTrack: 'track-1' },
        { id: 'reversed', name: 'Reversed', start: 10, end: 5, sourceTrack: 'track-1' },
      ];
      render(<ClipManager {...defaultProps} clips={clips} />);
      expect(screen.getByText('Valid')).toBeInTheDocument();
      expect(screen.queryByText('Equal')).not.toBeInTheDocument();
      expect(screen.queryByText('Reversed')).not.toBeInTheDocument();
    });

    it('preserves order of valid clips when some are filtered', () => {
      const clips: ManifestClip[] = [
        { id: 'a', name: 'First', start: 0, end: 10, sourceTrack: 'track-1' },
        { id: 'b', name: 'Invalid', start: -1, end: 5, sourceTrack: 'track-1' },
        { id: 'c', name: 'Third', start: 20, end: 30, sourceTrack: 'track-1' },
      ];
      render(<ClipManager {...defaultProps} clips={clips} />);
      const items = screen.getAllByRole('listitem');
      expect(items).toHaveLength(2);
      expect(items[0]).toHaveTextContent('First');
      expect(items[1]).toHaveTextContent('Third');
    });
  });

  describe('Panel header', () => {
    it('renders the CLIPS header', () => {
      render(<ClipManager {...defaultProps} />);
      expect(screen.getByText('CLIPS')).toBeInTheDocument();
    });
  });
});
