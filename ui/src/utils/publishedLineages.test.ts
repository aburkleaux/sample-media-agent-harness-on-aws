import { describe, expect, it } from 'vitest';
import type { PublishedAssetSummary } from '../lab-api';
import { collapseToLineages, pickRepresentative } from './publishedLineages';

function version(
  assetId: string,
  v: number,
  extra: Partial<PublishedAssetSummary> = {},
): PublishedAssetSummary {
  return {
    assetId,
    name: `skill-${assetId}`,
    version: v,
    current: false,
    scope: 'private',
    createdAt: v,
    updatedAt: v,
    ...extra,
  };
}

describe('pickRepresentative', () => {
  it('prefers the version the backend marks current', () => {
    const rows = [version('a', 1), version('a', 3), version('a', 2, { current: true })];

    // Not the highest: `current` is the backend's own answer to "which version
    // is live", and a higher number can exist without being it.
    expect(pickRepresentative(rows).version).toBe(2);
  });

  it('falls back to the highest version when none is marked current', () => {
    expect(pickRepresentative([version('a', 1), version('a', 7), version('a', 3)]).version)
      .toBe(7);
  });

  it('does not depend on input order', () => {
    const rows = [version('a', 5), version('a', 9), version('a', 2)];
    const reversed = [...rows].reverse();

    expect(pickRepresentative(rows)).toEqual(pickRepresentative(reversed));
  });
});

describe('collapseToLineages', () => {
  it('groups every version of a lineage into one entry', () => {
    const lineages = collapseToLineages([
      version('a', 2, { current: true }),
      version('a', 1),
      version('b', 1, { current: true }),
    ]);

    expect(lineages).toHaveLength(2);
    const a = lineages.find(l => l.summary.assetId === 'a')!;
    expect(a.versionCount).toBe(2);
    expect(a.summary.version).toBe(2);
    expect(lineages.find(l => l.summary.assetId === 'b')!.versionCount).toBe(1);
  });

  it('sorts lineages by their representative, newest first', () => {
    const lineages = collapseToLineages([
      version('old', 1, { updatedAt: 10 }),
      version('new', 1, { updatedAt: 99 }),
    ]);

    expect(lineages.map(l => l.summary.assetId)).toEqual(['new', 'old']);
  });

  it('returns nothing for no versions', () => {
    expect(collapseToLineages([])).toEqual([]);
  });

  it('keeps lineages that share a name but not an assetId separate', () => {
    // `assetId` is the lineage identity; two skills may legitimately be named
    // the same, and collapsing on name would silently merge them.
    const lineages = collapseToLineages([
      version('a', 1, { name: 'duplicate' }),
      version('b', 1, { name: 'duplicate' }),
    ]);

    expect(lineages).toHaveLength(2);
  });
});
