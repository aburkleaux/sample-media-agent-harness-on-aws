/** The Assets grid's session-structure rules and the bulk-delete runner (asset-lineage R10, R11). */
import { describe, expect, it, vi } from 'vitest';
import type { AssetView, ProjectsResult } from '../../projects/api';
import { assetRole } from '../../projects/api';
import {
  BULK_DELETE_CONCURRENCY,
  bulkDeleteNames,
  bulkDeleteTitle,
  deleteInParallel,
  isAnalysisFile,
  isMetadataFile,
  isSessionFolder,
  showsInGrid,
} from './assetSelection';

const a = (id: string, over: Partial<AssetView> = {}): AssetView =>
  ({ assetId: id, displayName: `${id}.mp4`, kind: 'video', status: 'ready', ...over });

describe('session structure', () => {
  it('treats a row without a role (or with an unknown one) as a source', () => {
    expect(assetRole({})).toBe('source');
    expect(assetRole({ role: 'mystery' })).toBe('source');
    expect(assetRole({ role: 'analysis' })).toBe('analysis');
    expect(isAnalysisFile(a('x'))).toBe(false);
    expect(isAnalysisFile(a('x', { role: 'intermediate' }))).toBe(true);
    expect(isAnalysisFile(a('x', { role: 'deliverable' }))).toBe(false);
  });

  it('isMetadataFile is the kind=data cut, independent of role', () => {
    expect(isMetadataFile(a('x', { kind: 'data', role: 'analysis' }))).toBe(true);
    expect(isMetadataFile(a('x', { kind: 'data' }))).toBe(true);
    expect(isMetadataFile(a('x', { kind: 'video' }))).toBe(false);
    expect(isMetadataFile(a('x', { kind: 'image', role: 'intermediate' }))).toBe(false); // analysis-role but not data
  });

  it('recognises Sessions/… folders and their subfolders, not Generated/… ones', () => {
    expect(isSessionFolder('Sessions/Cut · 2026-10-08')).toBe(true);
    expect(isSessionFolder('Sessions/Cut · 2026-10-08/Shot segmentation')).toBe(true);
    expect(isSessionFolder('Sessions')).toBe(true);
    expect(isSessionFolder('SessionsOld')).toBe(false);
    expect(isSessionFolder('Generated/Cut · 2026-10-08')).toBe(false);
    expect(isSessionFolder('')).toBe(false);
  });

  it('shows an analysis file only with the switch on or inside its opened Sessions folder', () => {
    const f = 'Sessions/Cut · 2026-10-08';
    const shots = a('data:1', { kind: 'data', role: 'analysis', folder: f });
    expect(showsInGrid(shots, false, null)).toBe(false);
    expect(showsInGrid(shots, true, null)).toBe(true);
    expect(showsInGrid(shots, false, f)).toBe(true);
    expect(showsInGrid(shots, false, 'Sessions/Other · 2026-10-08')).toBe(false);
    // An analysis file outside a Sessions folder is not shown by opening its (ordinary) folder.
    expect(showsInGrid(a('d', { role: 'analysis', folder: 'Footage' }), false, 'Footage')).toBe(false);
    expect(showsInGrid(a('v', { folder: f }), false, null)).toBe(true);
  });
});

describe('bulk delete', () => {
  it('titles and names the confirmation', () => {
    expect(bulkDeleteTitle(1)).toBe('Delete 1 file');
    expect(bulkDeleteTitle(1200)).toBe('Delete 1,200 files');
    expect(bulkDeleteNames(['a', 'b'])).toEqual({ named: ['a', 'b'], more: 0 });
    expect(bulkDeleteNames(['1', '2', '3', '4', '5', '6', '7'])).toEqual({ named: ['1', '2', '3', '4', '5'], more: 2 });
  });

  it(`keeps at most ${BULK_DELETE_CONCURRENCY} removes in flight and reports every one as it settles`, async () => {
    const assets = Array.from({ length: 20 }, (_, i) => a(`v:${i}`));
    let inFlight = 0;
    let peak = 0;
    const remove = vi.fn(async (): Promise<ProjectsResult<void>> => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 1));
      inFlight -= 1;
      return { ok: true, value: undefined };
    });
    const settled = vi.fn();
    expect(await deleteInParallel(assets, remove, { onSettled: settled })).toEqual([]);
    expect(remove).toHaveBeenCalledTimes(20);
    expect(peak).toBe(BULK_DELETE_CONCURRENCY);
    expect(settled).toHaveBeenCalledTimes(20);
  });

  it('counts not_found as deleted and returns the failures in input order, a thrown error included', async () => {
    const assets = [a('v:1'), a('v:2'), a('v:3'), a('v:4')];
    const answers: Record<string, () => Promise<ProjectsResult<void>>> = {
      'v:1': async () => ({ ok: false, kind: 'error', status: 500, code: 'x', message: 'Storage unavailable' }),
      'v:2': async () => ({ ok: false, kind: 'not_found' }),
      'v:3': async () => { throw new Error('network down'); },
      'v:4': async () => ({ ok: false, kind: 'disabled' }),
    };
    const failures = await deleteInParallel(assets, (id) => answers[id](), { concurrency: 2 });
    expect(failures.map((f) => [f.asset.assetId, f.message])).toEqual([
      ['v:1', 'Storage unavailable'],
      ['v:3', 'network down'],
      ['v:4', 'try again.'],
    ]);
  });

  it('does nothing for an empty selection', async () => {
    const remove = vi.fn();
    expect(await deleteInParallel([], remove)).toEqual([]);
    expect(remove).not.toHaveBeenCalled();
  });
});
