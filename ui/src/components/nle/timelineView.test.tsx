/**
 * @vitest-environment jsdom
 */
// The editor's ruler and the analysis lanes share ONE zoom and scroll (Lab 1: on the 12-minute film the
// lanes stayed at 1× while the ruler zoomed, so a shot was a few pixels wide and impossible to pick).
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import TimelineEditor from './TimelineEditor';
import UnifiedTimelineLanes, { LANE_GUTTER } from './UnifiedTimelineLanes';
import { TimelineEngine } from './TimelineEngine';
import { useTimelineView } from './timelineView';
import { theme } from '../../styles/tokens';

const markers = Array.from({ length: 40 }, (_, i) => ({ id: `s${i}`, time: i * 15, endTime: i * 15 + 15, label: String(i), category: 'content' }));

function Both({ engine }: { engine: TimelineEngine }) {
  const view = useTimelineView();
  return (
    <>
      <TimelineEditor engine={engine} state={{ ...engine.getState(), duration: 600 }} duration={600} src="https://cdn/x.mp4" theme={theme} view={view} gutter={LANE_GUTTER} />
      <UnifiedTimelineLanes view={view} audioTracks={[]} peaks={{}} markers={markers as never} segments={[]} duration={600} currentTime={0}
        onSeek={vi.fn()} trackLabel={() => 't'} mediaRef={{ current: null }} />
    </>
  );
}

/** jsdom has no layout: give a scroll container the geometry it has in a 1000px-wide panel — the lanes
 *  span it, the ruler's viewport sits behind a gutter of the same width, so both tracks are 896px × zoom. */
function layout(el: HTMLElement, zoomOf: () => number, gutter: number) {
  const track = 1000 - LANE_GUTTER;
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => track + gutter });
  Object.defineProperty(el, 'scrollWidth', { configurable: true, get: () => gutter + track * zoomOf() });
}

describe('shared timeline view', () => {
  it('wheel over the ruler zooms the lanes too; the lane buttons zoom the ruler', () => {
    render(<Both engine={new TimelineEngine(600)} />);
    const lanes = screen.getByTestId('unified-timeline');
    expect(within(lanes).getByTestId('zoom-level')).toHaveTextContent('1×');
    fireEvent.wheel(screen.getByTestId('timeline-ruler-viewport'), { deltaY: -100 });
    expect(within(lanes).getByTestId('zoom-level')).toHaveTextContent('1.3×');
    expect(screen.getByTestId('timeline-ruler')).toHaveStyle({ width: '125%' });
    fireEvent.click(within(lanes).getByTestId('zoom-in'));
    expect(screen.getByTestId('timeline-ruler')).toHaveStyle({ width: '188%' });
  });

  it('wheel over the lanes zooms; a horizontal swipe is left to scroll natively', () => {
    render(<Both engine={new TimelineEngine(600)} />);
    const scroll = screen.getByTestId('lanes-scroll');
    fireEvent.wheel(scroll, { deltaY: 10, deltaX: 80 });
    expect(screen.getByTestId('timeline-ruler')).toHaveStyle({ width: '100%' });
    fireEvent.wheel(scroll, { deltaY: -100 });
    expect(screen.getByTestId('timeline-ruler')).toHaveStyle({ width: '125%' });
  });

  it("the lane TRACK, not the row, is zoomed — so it has the ruler's width", () => {
    render(<Both engine={new TimelineEngine(600)} />);
    fireEvent.click(within(screen.getByTestId('unified-timeline')).getByTestId('zoom-in'));
    expect(screen.getByTestId('lanes-inner').style.width).toBe(`calc(${LANE_GUTTER}px + 1.5 * (100% - ${LANE_GUTTER}px))`);
    expect(screen.getByTestId('timeline-ruler-gutter')).toHaveStyle({ width: `${LANE_GUTTER}px` });
  });

  it('scrolling either one scrolls the other to the same time', () => {
    render(<Both engine={new TimelineEngine(600)} />);
    const ruler = screen.getByTestId('timeline-ruler-viewport');
    const lanes = screen.getByTestId('lanes-scroll');
    let z = 1;
    layout(ruler, () => z, 0);
    layout(lanes, () => z, LANE_GUTTER);
    z = 4;
    fireEvent.click(within(screen.getByTestId('unified-timeline')).getByTestId('zoom-in')); // state only; geometry faked
    ruler.scrollLeft = 1000;
    fireEvent.scroll(ruler);
    // The scroll range is (4-1)×896 in both, so the same scrollLeft = the same time.
    expect(lanes.scrollLeft).toBeCloseTo(1000, 0);
    lanes.scrollLeft = 300;
    fireEvent.scroll(lanes);
    expect(ruler.scrollLeft).toBeCloseTo(300, 0);
  });

  it('shot numbers appear once a shot is wide enough when zoomed', () => {
    render(<Both engine={new TimelineEngine(600)} />);
    const shots = screen.getByTestId('lane-shots');
    expect(within(shots).queryByText('7')).not.toBeInTheDocument(); // 15 s of 600 = 2.5% at 1×
    fireEvent.click(within(screen.getByTestId('unified-timeline')).getByTestId('zoom-in'));
    expect(within(shots).getByText('7')).toBeInTheDocument();
  });
});
