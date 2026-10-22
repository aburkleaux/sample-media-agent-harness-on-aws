/**
 * @vitest-environment jsdom
 */
// Where am I + which frame: with the video's frame rate known, timecodes read HH:MM:SS:FF, the hover shows a line
// and a frame peek with its exact timecode, and a mark made by dragging lands on a frame start. Without a rate the
// editor behaves exactly as before (milliseconds).
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import React from 'react';
import TimelineEditor from './TimelineEditor';
import UnifiedTimelineLanes from './UnifiedTimelineLanes';
import { TimelineEngine } from './TimelineEngine';
import { theme } from '../../styles/tokens';

const rect = (left: number, width: number, top = 100) =>
  ({ left, width, top, right: left + width, bottom: top + 38, height: 38, x: left, y: top, toJSON: () => ({}) }) as DOMRect;

function editor(fps: number | null, extra: Record<string, unknown> = {}) {
  const engine = new TimelineEngine(60);
  engine.seek(8.1);
  const ui = render(<TimelineEditor engine={engine} state={{ ...engine.getState(), duration: 60 }} duration={60}
    src="https://cdn/x.mp4" theme={theme} fps={fps} {...extra} />);
  const ruler = screen.getByTestId('timeline-ruler');
  vi.spyOn(ruler, 'getBoundingClientRect').mockReturnValue(rect(0, 600));
  return { engine, ruler, ...ui };
}

describe('TimelineEditor — frames', () => {
  it('reads HH:MM:SS:FF when the frame rate is known, milliseconds when not', () => {
    editor(24);
    expect(screen.getByTestId('timeline-timecode')).toHaveTextContent('00:00:08:02 / 00:01:00:00');
  });

  it('keeps the millisecond timecode without a frame rate', () => {
    editor(null);
    expect(screen.getByTestId('timeline-timecode')).toHaveTextContent('00:08.100 / 01:00.000');
  });

  it('hovering the ruler shows a line and the frame peek with the exact timecode; leaving hides both', () => {
    const { ruler } = editor(24);
    fireEvent.pointerMove(ruler, { clientX: 150 });               // 150/600 of 60 s = 15 s = frame 360
    expect(screen.getByTestId('timeline-hover-line')).toHaveStyle({ left: '25%' });
    expect(screen.getByTestId('timeline-hover-timecode')).toHaveTextContent('00:00:15:00 · frame 360');
    fireEvent.pointerLeave(ruler);
    expect(screen.queryByTestId('timeline-hover-line')).toBeNull();
    expect(screen.queryByTestId('timeline-hover-peek')).toBeNull();
  });

  it('a range dragged on the ruler lands on frame starts', () => {
    const { engine, ruler } = editor(24);
    fireEvent.pointerDown(ruler, { clientX: 101 });                // 10.1 s → frame 242 → 10.0833 s
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: 205 }));
    window.dispatchEvent(new PointerEvent('pointerup', { clientX: 205 }));
    const [r] = engine.getState().ranges;
    expect(r.in * 24).toBeCloseTo(Math.round(r.in * 24), 6);       // a whole frame
    expect(r.out * 24).toBeCloseTo(Math.round(r.out * 24), 6);
    expect(Math.round(r.in * 24)).toBe(242);
  });

  it('the shared hover (from the lanes) draws on the ruler too', () => {
    editor(24, { hoverTime: 30, onHover: vi.fn() });
    expect(screen.getByTestId('timeline-hover-line')).toHaveStyle({ left: '50%' });
    expect(screen.getByTestId('timeline-hover-timecode')).toHaveTextContent('00:00:30:00');
  });

  it('an audio-only timeline peeks the timecode alone (no frame)', () => {
    const { ruler } = editor(null, { peekFrames: false });
    fireEvent.pointerMove(ruler, { clientX: 300 });
    expect(screen.getByTestId('timeline-hover-peek').querySelector('canvas')).toBeNull();
    expect(screen.getByTestId('timeline-hover-timecode')).toHaveTextContent('00:30.000');
  });
});

describe('UnifiedTimelineLanes — the hover line across every lane', () => {
  const lanes = (props: Record<string, unknown>) => render(<UnifiedTimelineLanes audioTracks={[]} peaks={{}}
    markers={[{ id: 'm1', time: 5 } as never]} segments={[]} duration={60} currentTime={0} onSeek={vi.fn()}
    trackLabel={() => 'x'} mediaRef={{ current: null }} {...props} />);

  it('reports the frame under the pointer (past the label column) and draws the shared line', () => {
    const onHover = vi.fn();
    const { rerender } = lanes({ onHover, fps: 24, hoverTime: null });
    const inner = screen.getByTestId('lanes-inner');
    vi.spyOn(inner, 'getBoundingClientRect').mockReturnValue(rect(0, 704));   // 104 px labels + 600 px of time
    fireEvent.pointerMove(inner, { clientX: 104 + 300 });
    expect(onHover).toHaveBeenLastCalledWith(30);
    fireEvent.pointerMove(inner, { clientX: 50 });                              // over the labels: no time
    expect(onHover).toHaveBeenLastCalledWith(null);
    rerender(<UnifiedTimelineLanes audioTracks={[]} peaks={{}} markers={[{ id: 'm1', time: 5 } as never]} segments={[]}
      duration={60} currentTime={0} onSeek={vi.fn()} trackLabel={() => 'x'} mediaRef={{ current: null }}
      onHover={onHover} fps={24} hoverTime={15} />);
    expect(screen.getByTestId('lanes-hover-line')).toHaveStyle({ left: '25%' });
  });
});
