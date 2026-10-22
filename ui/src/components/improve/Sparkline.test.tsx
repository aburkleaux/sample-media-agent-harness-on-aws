/**
 * @vitest-environment jsdom
 *
 * The line's own rules, at the two edges its callers cannot reach.
 *
 * Everything a reader actually meets is asserted through `OutcomeBand`, which is what mounts this and
 * supplies the series — a component tested only in isolation can be perfectly correct and wired to
 * the wrong field. What is here instead is the drawing bound and the degenerate inputs, neither of
 * which the wire produces today and both of which this component decides alone.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required.
 */
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Sparkline } from './Sparkline';

/** The bound the component declares. Not read from it — a test importing the constant would pass
 *  whatever the constant said, including a typo. */
const MAX_POINTS = 30;

const points = (): string[] => (screen.getByTestId('sparkline-line')
  .getAttribute('points') ?? '').split(' ');

describe('Sparkline', () => {
  it('draws every reading up to its bound', () => {
    // The producer retains a rolling 30 day rows, so this is the whole of a full history.
    const month = Array.from({ length: MAX_POINTS }, (_, i) => i);
    render(<Sparkline points={month} label="thirty readings" />);

    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', String(MAX_POINTS));
    expect(points()).toHaveLength(MAX_POINTS);
  });

  it('drops the OLDEST readings past that bound, never the newest', () => {
    /*
     * A drawing bound and not a second retention policy: the producer already evicts old rows into a
     * separate bucket. This is here so that if its bound ever moves, the line stays legible instead
     * of becoming a smear at sub-pixel spacing — and it has to cut the far end, because a picture
     * that silently dropped the latest reading would disagree with the figure beside it.
     */
    const tooMany = Array.from({ length: MAX_POINTS + 5 }, (_, i) => i);
    render(<Sparkline points={tooMany} label="more than a month" />);

    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', String(MAX_POINTS));
    // The last point is at the top of the range, which only holds if the tail survived.
    const [, lastY] = points()[MAX_POINTS - 1].split(',');
    expect(Number(lastY)).toBe(2);
  });

  it('renders nothing rather than an empty box when there is no reading', () => {
    // A framed empty band reads as a measurement of nothing. Whatever mounts this says the absence
    // in words instead.
    const { container } = render(<Sparkline points={[]} label="nothing" />);
    expect(container.firstChild).toBeNull();
  });

  it('drops a value that cannot be placed on an axis instead of losing the whole picture', () => {
    // `Math.min` of a NaN is NaN, so one bad figure would take every coordinate with it and the
    // line would vanish — which reads as no history rather than as one bad reading.
    render(<Sparkline points={[3, Number.NaN, 5, Number.POSITIVE_INFINITY]} label="two good" />);

    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', '2');
    expect(points()).toHaveLength(2);
  });

  it('centres a single reading rather than pinning it to either end', () => {
    // One point is one point. Its position along the line carries no information, so it goes in the
    // middle rather than at a start or an end it does not have.
    render(<Sparkline points={[7]} label="one reading" />);

    const dot = screen.getByTestId('sparkline-point');
    expect(dot.getAttribute('cx')).toBe('32');
    expect(dot.getAttribute('cy')).toBe('9');
    expect(screen.queryByTestId('sparkline-line')).toBeNull();
  });
});
