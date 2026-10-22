import { describe, expect, it } from 'vitest';
import { CENTER_MIN, LEFT_MIN, RIGHT_MIN, fitPanelWidths } from './layout';

describe('fitPanelWidths', () => {
  it('draws the saved widths when the window has room', () => {
    expect(fitPanelWidths(1920, 300, 900, false, false, 48)).toEqual({ left: 300, right: 900 });
  });
  it('the right panel gives way first, so the center keeps CENTER_MIN', () => {
    const f = fitPanelWidths(1440, 280, 990, false, false, 48);
    expect(f.left).toBe(280);
    expect(1440 - f.left - f.right).toBe(CENTER_MIN);
  });
  it('then the sidebar (the 1100px window that crushed the composer)', () => {
    const f = fitPanelWidths(1100, 415, 440, false, false, 48);
    expect(f.right).toBe(RIGHT_MIN);
    expect(f.left).toBe(1100 - RIGHT_MIN - CENTER_MIN);
  });
  it('never below the minimums: the center shrinks last', () => {
    expect(fitPanelWidths(800, 415, 440, false, false, 48)).toEqual({ left: LEFT_MIN, right: RIGHT_MIN });
  });
  it('a collapsed sidebar keeps its rail and a collapsed panel takes no width', () => {
    expect(fitPanelWidths(1100, 415, 990, true, false, 48)).toEqual({ left: 48, right: 1100 - 48 - CENTER_MIN });
    expect(fitPanelWidths(1100, 415, 990, false, true, 48)).toEqual({ left: 415, right: 0 });
  });
});
