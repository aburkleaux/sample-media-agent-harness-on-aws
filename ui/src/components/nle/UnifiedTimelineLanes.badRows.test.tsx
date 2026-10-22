// @vitest-environment jsdom
/**
 * Session 61deed9b: the Shot Segmentation tab died with "Cannot read properties of undefined (reading 'toFixed')"
 * because its face annotations used the flat `{x, y, w, h, label, time}` sample shape. Fixture = the first rows of
 * that real manifest. Normalized, they render as a person lane; raw, the lanes now skip them instead of throwing.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import UnifiedTimelineLanes from './UnifiedTimelineLanes';
import { normalizeAnnotations } from '../renderer/annotations';
import fx from './__fixtures__/session-61deed9b-lanes.json';

const base = {
  audioTracks: [], peaks: {}, duration: fx.duration, currentTime: 0, onSeek: vi.fn(),
  trackLabel: () => 'track', mediaRef: { current: null },
  markers: fx.markers as never, segments: fx.segments as never,
};

describe('UnifiedTimelineLanes with the session 61deed9b manifest', () => {
  it('renders normalized flat annotations as a person lane', () => {
    render(<UnifiedTimelineLanes {...base} segments={[] as never} annotations={normalizeAnnotations(fx.annotations)} />);
    expect(screen.getAllByTestId('lane-person').length).toBeGreaterThan(0);
  });

  it('gives a person who is also a segment group ONE lane, not two (the real manifest has both)', () => {
    render(<UnifiedTimelineLanes {...base} annotations={normalizeAnnotations(fx.annotations)} />);
    const labels = [...screen.queryAllByTestId('lane-speaker'), ...screen.queryAllByTestId('lane-person')]
      .map((el) => el.textContent?.match(/Person_\d+/)?.[0]).filter(Boolean);
    expect(labels).toEqual([...new Set(labels)]);
    expect(labels).toContain('Person_0');
  });

  it('does not throw on raw flat annotations (skips them)', () => {
    expect(() => render(<UnifiedTimelineLanes {...base} annotations={fx.annotations as never} />)).not.toThrow();
    expect(screen.queryAllByTestId('lane-person')).toHaveLength(0);
  });

  it('skips a marker or segment without a numeric time', () => {
    const markers = [...fx.markers, { label: 'x', id: 'bad' }] as never;
    const segments = [...fx.segments, { id: 's', name: 'P', group: 'Person_0', start: null, end: 3 }] as never;
    expect(() => render(<UnifiedTimelineLanes {...base} markers={markers} segments={segments} />)).not.toThrow();
  });
});
