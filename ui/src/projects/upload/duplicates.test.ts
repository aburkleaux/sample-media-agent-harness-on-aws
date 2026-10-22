import { afterEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ authFetch: vi.fn(), getConfig: vi.fn() }));
vi.mock('../../auth/authFetch', () => ({ authFetch: mocks.authFetch }));
vi.mock('../../lab-api', () => ({ getConfig: mocks.getConfig }));

import { displayBase, findDuplicates, withoutSuffix } from './duplicates';

const file = (name: string, size: number) => new File([new Uint8Array(size)], name);
const asset = (displayName: string, sourceSize: number, status = 'indexed') =>
  ({ assetId: `video:${displayName}`, displayName, kind: 'video', status, sourceSize }) as never;

afterEach(() => vi.clearAllMocks());

describe('displayBase (TS twin of derive_display_name)', () => {
  it('drops [ ] @ # and control characters, collapses whitespace', () => {
    expect(displayBase('  my [final] @cut #2\u0007.mp4 ')).toBe('my final cut 2.mp4');
    expect(withoutSuffix('clip.mp4 (2)')).toBe('clip.mp4');
    expect(withoutSuffix('clip (2).mp4')).toBe('clip (2).mp4'); // only the registry's trailing suffix
  });
});

describe('findDuplicates', () => {
  it('makes ONE batched request with every name and size, and maps the matches back by index', async () => {
    const batch = vi.fn(async () => ({
      ok: true as const,
      value: { duplicates: [{ index: 2, asset: asset('c.mp4', 3) }, { index: 0, asset: asset('a.mp4', 1) }] },
    }));
    const files = [file('a.mp4', 1), file('b.mp4', 2), file('c.mp4', 3)];
    const found = await findDuplicates('p1', files, batch);
    expect(batch).toHaveBeenCalledTimes(1);
    expect(batch).toHaveBeenCalledWith('p1', [
      { name: 'a.mp4', size: 1 }, { name: 'b.mp4', size: 2 }, { name: 'c.mp4', size: 3 },
    ]);
    // In the order of the files, not of the answer.
    expect(found.map((d) => [d.file, d.existing.displayName])).toEqual([[files[0], 'a.mp4'], [files[2], 'c.mp4']]);
  });

  it('chunks by 1000 and offsets nothing: each chunk maps its own indexes', async () => {
    const files = Array.from({ length: 1001 }, (_, i) => file(`f${i}.mp4`, 1));
    const batch = vi.fn(async (_pid: string, chunk: { name: string }[]) => ({
      ok: true as const,
      value: { duplicates: chunk.length === 1 ? [{ index: 0, asset: asset('f1000.mp4', 1) }] : [] },
    }));
    const found = await findDuplicates('p1', files, batch);
    expect(batch.mock.calls.map((c) => c[1].length)).toEqual([1000, 1]);
    expect(found.map((d) => d.file)).toEqual([files[1000]]);
  });

  it('ignores a removed asset, an out-of-range index, and a repeated index', async () => {
    const batch = vi.fn(async () => ({
      ok: true as const,
      value: { duplicates: [
        { index: 0, asset: asset('a.mp4', 1, 'removed') },
        { index: 7, asset: asset('x.mp4', 1) },
        { index: 1, asset: asset('b.mp4', 1) },
        { index: 1, asset: asset('b.mp4 (2)', 1) },
      ] },
    }));
    const found = await findDuplicates('p1', [file('a.mp4', 1), file('b.mp4', 1)], batch);
    expect(found.map((d) => d.existing.displayName)).toEqual(['b.mp4']);
  });

  it('never blocks: a failing or refused request means no duplicates', async () => {
    const boom = vi.fn(async () => { throw new Error('offline'); });
    expect(await findDuplicates('p1', [file('a.mp4', 1)], boom)).toEqual([]);
    const refused = vi.fn(async () => ({ ok: false as const, kind: 'error' as const, status: 503, code: 'x', message: 'x' }));
    expect(await findDuplicates('p1', [file('a.mp4', 1)], refused)).toEqual([]);
  });

  it('by default calls POST /projects/:pid/assets/duplicates once', async () => {
    mocks.getConfig.mockResolvedValue({ projectsEnabled: true });
    mocks.authFetch.mockResolvedValue(new Response(JSON.stringify({ duplicates: [{ index: 1, asset: asset('b.mp4', 2) }] }), { status: 200 }));
    const files = [file('a.mp4', 1), file('b.mp4', 2)];
    const found = await findDuplicates('p 1', files);
    expect(mocks.authFetch).toHaveBeenCalledTimes(1);
    const [path, init] = mocks.authFetch.mock.calls[0];
    expect(path).toBe('/projects/p%201/assets/duplicates');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ files: [{ name: 'a.mp4', size: 1 }, { name: 'b.mp4', size: 2 }] });
    expect(found.map((d) => d.file)).toEqual([files[1]]);

    mocks.authFetch.mockResolvedValue(new Response(JSON.stringify({ error: { code: 'boom', message: 'x' } }), { status: 500 }));
    expect(await findDuplicates('p1', files)).toEqual([]);
  });
});
