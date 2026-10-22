import { describe, expect, it } from 'vitest';
import {
  activeBoxesAt,
  entityColor,
  formatTimecode,
  groupAnnotationsByEntity,
  mergeAppearances,
  type Annotation,
} from './annotations';

const box = (x: number, y: number) => ({ x, y, width: 0.1, height: 0.2 });

// Sampled ~1s apart, mirroring the live face-recognition output shape
// (facegroup.json: {timestamp, box:{l,t,w,h}}).
const annotations: Annotation[] = [
  { entityId: 'entity-0', start: 30.03, end: 31.031, box: box(0.2, 0.1), confidence: 98 },
  { entityId: 'entity-0', start: 31.031, end: 32.031, box: box(0.4, 0.1) },
  { entityId: 'entity-1', start: 37.037, end: 38.038, box: box(0.6, 0.5) },
];

describe('activeBoxesAt', () => {
  it('returns no boxes before any detection', () => {
    expect(activeBoxesAt(annotations, 10)).toEqual([]);
  });

  it('returns the box active at the given time', () => {
    const active = activeBoxesAt(annotations, 30.03);
    expect(active).toHaveLength(1);
    expect(active[0].entityId).toBe('entity-0');
    expect(active[0].box.x).toBeCloseTo(0.2, 5);
    expect(active[0].confidence).toBe(98);
  });

  it('interpolates box motion between nearby samples', () => {
    const midpoint = 30.03 + (31.031 - 30.03) / 2;
    const active = activeBoxesAt(annotations, midpoint);
    expect(active[0].box.x).toBeGreaterThan(0.2);
    expect(active[0].box.x).toBeLessThan(0.4);
  });

  it('drops boxes once their interval ends', () => {
    expect(activeBoxesAt(annotations, 33)).toEqual([]);
  });

  it('only returns entities that are visible', () => {
    const visible = new Set(['entity-1']);
    expect(activeBoxesAt(annotations, 30.03, visible)).toEqual([]);
    expect(activeBoxesAt(annotations, 37.5, visible)).toHaveLength(1);
  });

  it('returns one box per entity when several overlap', () => {
    const overlapping: Annotation[] = [
      { entityId: 'a', start: 0, end: 5, box: box(0.1, 0.1) },
      { entityId: 'b', start: 0, end: 5, box: box(0.5, 0.5) },
    ];
    expect(activeBoxesAt(overlapping, 1).map((a) => a.entityId).sort()).toEqual(['a', 'b']);
  });
});

describe('groupAnnotationsByEntity', () => {
  it('groups and orders annotations per entity', () => {
    const grouped = groupAnnotationsByEntity([
      { entityId: 'a', start: 5, end: 6, box: box(0, 0) },
      { entityId: 'a', start: 1, end: 2, box: box(0, 0) },
    ]);
    expect(grouped.get('a')?.map((a) => a.start)).toEqual([1, 5]);
  });
});

describe('mergeAppearances', () => {
  it('merges adjacent intervals for one entity', () => {
    const merged = mergeAppearances(
      [
        { entityId: 'a', start: 0, end: 2 },
        { entityId: 'a', start: 2, end: 4 },
        { entityId: 'a', start: 9, end: 10 },
        { entityId: 'b', start: 0, end: 8 },
      ],
      'a',
    );
    expect(merged).toEqual([
      { entityId: 'a', start: 0, end: 4 },
      { entityId: 'a', start: 9, end: 10 },
    ]);
  });
});

describe('formatTimecode', () => {
  it('formats minutes and seconds', () => {
    expect(formatTimecode(0)).toBe('0:00');
    expect(formatTimecode(65)).toBe('1:05');
    expect(formatTimecode(3725)).toBe('1:02:05');
  });

  it('is defensive about invalid input', () => {
    expect(formatTimecode(Number.NaN)).toBe('0:00');
    expect(formatTimecode(-5)).toBe('0:00');
  });
});

describe('entityColor', () => {
  it('is stable per index and wraps around', () => {
    expect(entityColor(0)).toBe(entityColor(0));
    expect(entityColor(0)).not.toBe(entityColor(1));
    expect(entityColor(10)).toBe(entityColor(0));
  });
});
