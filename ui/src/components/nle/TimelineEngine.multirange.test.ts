/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { TimelineEngine } from './TimelineEngine';

describe('TimelineEngine — multi-range / multi-point', () => {
  it('addRange normalizes, clamps, and rejects zero-length', () => {
    const e = new TimelineEngine(100);
    const id = e.addRange(30, 10); // reversed → normalized to 10–30
    expect(id).not.toBeNull();
    expect(e.getState().ranges[0]).toMatchObject({ in: 10, out: 30 });
    // both endpoints clamp to 100 → zero-length → rejected
    expect(e.addRange(200, 300)).toBeNull();
    expect(e.addRange(50, 50)).toBeNull();
    expect(e.getState().ranges).toHaveLength(1);
  });

  it('commitDraftRange moves the draft into ranges and clears it', () => {
    const e = new TimelineEngine(100);
    e.setMarkIn(10); e.setMarkOut(20);
    const id = e.commitDraftRange();
    expect(id).not.toBeNull();
    expect(e.getState().ranges).toHaveLength(1);
    expect(e.getState().markIn).toBeNull();
    expect(e.getState().markOut).toBeNull();
    // nothing to commit now
    expect(e.commitDraftRange()).toBeNull();
  });

  it('updateRange and removeRange edit the committed set', () => {
    const e = new TimelineEngine(100);
    const id = e.addRange(10, 20)!;
    e.updateRange(id, 12, 25);
    expect(e.getState().ranges[0]).toMatchObject({ in: 12, out: 25 });
    e.removeRange(id);
    expect(e.getState().ranges).toHaveLength(0);
  });

  it('addPoint uses the playhead by default and clamps; removePoint/clearPoints work', () => {
    const e = new TimelineEngine(100);
    e.seek(42);
    const p = e.addPoint();
    expect(e.getState().points[0]).toMatchObject({ t: 42 });
    e.addPoint(200); // clamps to 100
    expect(e.getState().points[1].t).toBe(100);
    e.removePoint(p);
    expect(e.getState().points).toHaveLength(1);
    e.clearPoints();
    expect(e.getState().points).toHaveLength(0);
  });

  it('does not regress single mark-in/out or default state', () => {
    const e = new TimelineEngine(100);
    expect(e.getState().ranges).toEqual([]);
    expect(e.getState().points).toEqual([]);
    e.setMarkIn(50); e.setMarkOut(80);
    expect(e.getState().markIn).toBe(50);
    expect(e.getState().markOut).toBe(80);
  });
});
