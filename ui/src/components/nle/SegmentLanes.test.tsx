/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SegmentLanes, { groupIntoLanes, laneColor } from './SegmentLanes';
import type { ManifestSegment } from './manifestParser';

const seg = (id: string, start: number, end: number, group?: string): ManifestSegment =>
  ({ id, name: group ?? id, start, end, ...(group ? { group } : {}) });

// The real diarization turns: two speakers, alternating.
const TURNS = [
  seg('s-0', 26.978, 29.318, 'SPEAKER_00'),
  seg('s-1', 30.018, 34.015, 'SPEAKER_01'),
  seg('s-2', 34.5, 36.0, 'SPEAKER_00'),
];

describe('groupIntoLanes', () => {
  it('creates one lane per group, not one row per segment', () => {
    const lanes = groupIntoLanes(TURNS);
    expect(lanes.map(l => l.name)).toEqual(['SPEAKER_00', 'SPEAKER_01']);
    expect(lanes[0].segments).toHaveLength(2);
  });

  it('orders lanes by first appearance and sorts within a lane', () => {
    const lanes = groupIntoLanes([seg('b', 9, 10, 'B'), seg('a2', 5, 6, 'A'), seg('a1', 1, 2, 'A')]);
    expect(lanes.map(l => l.name)).toEqual(['B', 'A']);
    expect(lanes[1].segments.map(s => s.start)).toEqual([1, 5]);
  });

  it('puts ungrouped segments in a single lane', () => {
    const lanes = groupIntoLanes([seg('x', 0, 1), seg('y', 2, 3)]);
    expect(lanes).toHaveLength(1);
    expect(lanes[0].name).toBe('Segments');
  });

  it('is stable in colour so a speaker keeps its colour', () => {
    expect(laneColor('SPEAKER_00', 0)).toBe(laneColor('SPEAKER_00', 0));
    expect(laneColor('SPEAKER_00', 0)).not.toBe(laneColor('SPEAKER_01', 1));
  });
});

describe('SegmentLanes', () => {
  it('renders a labelled lane per speaker', () => {
    render(<SegmentLanes segments={TURNS} duration={60} currentTime={0} onSeek={() => {}} />);
    expect(screen.getByText('SPEAKER_00')).toBeTruthy();
    expect(screen.getByText('SPEAKER_01')).toBeTruthy();
    expect(screen.getByText(/2 lanes \(3 segments\)/)).toBeTruthy();
  });

  it('seeks to a turn start when its block is clicked', () => {
    const onSeek = vi.fn();
    render(<SegmentLanes segments={TURNS} duration={60} currentTime={0} onSeek={onSeek} />);
    fireEvent.click(screen.getByRole('button', { name: /from 30\.0 to 34\.0 seconds/ }));
    expect(onSeek).toHaveBeenCalledWith(30.018);
  });

  it('positions blocks proportionally to the timeline', () => {
    const { container } = render(
      <SegmentLanes segments={[seg('s', 30, 60, 'A')]} duration={60} currentTime={0} onSeek={() => {}} />,
    );
    const block = container.querySelector('button') as HTMLElement;
    expect(block.style.left).toBe('50%');
    expect(block.style.width).toBe('50%');
  });

  it('keeps a very short turn clickable instead of sub-pixel', () => {
    const { container } = render(
      <SegmentLanes segments={[seg('s', 0, 0.01, 'A')]} duration={600} currentTime={0} onSeek={() => {}} />,
    );
    expect(parseFloat((container.querySelector('button') as HTMLElement).style.width)).toBeGreaterThanOrEqual(0.6);
  });

  it('lays out without dividing by zero when duration is missing', () => {
    const { container } = render(
      <SegmentLanes segments={TURNS} duration={0} currentTime={0} onSeek={() => {}} />,
    );
    for (const b of Array.from(container.querySelectorAll('button'))) {
      expect((b as HTMLElement).style.width).not.toContain('NaN');
      expect((b as HTMLElement).style.left).not.toContain('NaN');
    }
  });

  it('marks the turn under the playhead as active', () => {
    render(<SegmentLanes segments={TURNS} duration={60} currentTime={31} onSeek={() => {}} />);
    const active = screen.getByRole('button', { name: /from 30\.0 to 34\.0 seconds/ });
    expect(active.style.opacity).toBe('1');
  });

  it('announces each lane for assistive tech', () => {
    render(<SegmentLanes segments={TURNS} duration={60} currentTime={0} onSeek={() => {}} />);
    expect(screen.getByRole('group', { name: /SPEAKER_00: 2 segments totalling 3\.8 seconds/ })).toBeTruthy();
  });

  it('renders nothing when there are no segments', () => {
    const { container } = render(<SegmentLanes segments={[]} duration={60} currentTime={0} onSeek={() => {}} />);
    expect(container.firstChild).toBeNull();
  });
});
