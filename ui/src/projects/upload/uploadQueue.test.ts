/**
 * @vitest-environment jsdom
 *
 * The upload queue's per-file state machine: batch validation (any count, chunked 20 per grant request; empty,
 * per-kind size, folder rules) before any request,
 * queued → uploading(pct) → registering → ready for a single PUT and a multipart upload, refusals, unverified
 * registration, Cancel (no registration, multipart aborted), Retry (a new grant and asset id), the batch summary,
 * the beforeunload guard, and a "this chat only" batch (session visibility, hidden from the Project list).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  getIngestionCapabilities: vi.fn(),
  createUploads: vi.fn(),
  completeUpload: vi.fn(),
  abortUpload: vi.fn(),
  registerAsset: vi.fn(),
}));
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  ...mocks,
}));

import {
  __resetUploadQueueForTests,
  __setTransferDepsForTests,
  batchSummary,
  cancel,
  getEntry,
  getProjectEntries,
  isTransferring,
  retry,
  MAX_PARALLEL_FILES,
  retryFailed,
  startBatch,
} from './uploadQueue';
import { setFolderOf } from './folders';
import { DEFAULT_CAPABILITIES } from './kinds';
import type { PutFn } from './transfer';

const PID = 'p1';

function file(name: string, size: number, type = 'video/mp4'): File {
  return new File([new Uint8Array(size)], name, { type });
}

function singleGrant(index: number, assetId = `video:${index}`) {
  return { index, assetId, key: `projects/${PID}/${assetId}/f`, mode: 'single', url: `https://s3/put/${index}`, headers: { 'Content-Type': 'video/mp4' } };
}

async function settle() {
  for (let i = 0; i < 30; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  for (let i = 0; i < 30; i++) await Promise.resolve();
}

let put: ReturnType<typeof vi.fn<PutFn>>;

beforeEach(() => {
  vi.clearAllMocks();
  __resetUploadQueueForTests();
  put = vi.fn<PutFn>().mockImplementation(async (_url, body, opts) => {
    opts.onProgress?.(body.size);
    return { etag: '"e"' };
  });
  __setTransferDepsForTests({ put, sleep: () => Promise.resolve() });
  mocks.getIngestionCapabilities.mockResolvedValue({ ok: true, value: DEFAULT_CAPABILITIES });
  mocks.completeUpload.mockResolvedValue({ ok: true, value: undefined });
  mocks.abortUpload.mockResolvedValue({ ok: true, value: undefined });
  mocks.registerAsset.mockImplementation(async (_pid: string, f: { assetId: string; fileName: string }) => ({
    ok: true,
    value: { assetId: f.assetId, displayName: f.fileName, kind: 'video', status: 'queued' },
  }));
});
afterEach(() => {
  __resetUploadQueueForTests();
});

describe('batch validation', () => {
  const grantAll = () => mocks.createUploads.mockImplementation(async (_pid: string, specs: unknown[]) => ({
    ok: true,
    value: { uploads: specs.map((_s, i) => singleGrant(i, `video:${Math.random().toString(36).slice(2)}`)), errors: {} },
  }));

  it('accepts any number of files and sends the grant requests in chunks of 20 (45 → 20/20/5), each file with its folder', async () => {
    grantAll();
    const files = Array.from({ length: 45 }, (_, i) =>
      setFolderOf(file(`f${i}.mp4`, 10), i % 2 ? `Footage/Day ${i % 3}` : undefined));
    const r = await startBatch(PID, files);
    expect(r.ok).toBe(true);
    expect(getProjectEntries(PID)).toHaveLength(45);
    await settle();
    await settle();
    const calls = mocks.createUploads.mock.calls.map((c) => c[1] as { name: string; folder?: string }[]);
    expect(calls.map((c) => c.length)).toEqual([20, 20, 5]);
    const sent = calls.flat();
    expect(sent.map((s) => s.name)).toEqual(files.map((f) => f.name));
    sent.forEach((s, i) => {
      if (i % 2) expect(s.folder).toBe(`Footage/Day ${i % 3}`);
      else expect('folder' in s).toBe(false);
    });
    expect(getProjectEntries(PID).every((e) => e.status === 'ready')).toBe(true);
    expect(getProjectEntries(PID)[1].folder).toBe('Footage/Day 1');
  });

  it('requests the next chunk only when the granted files are about to run out', async () => {
    grantAll();
    const releases: Array<() => void> = [];
    put.mockImplementation(() => new Promise((resolve) => { releases.push(() => resolve({ etag: '"e"' })); }));
    await startBatch(PID, Array.from({ length: 45 }, (_, i) => file(`f${i}.mp4`, 10)));
    await settle();
    expect(mocks.createUploads).toHaveBeenCalledTimes(1); // 20 granted, 3 transferring, 17 waiting
    expect(releases).toHaveLength(MAX_PARALLEL_FILES);
    // Drain until fewer than 3 granted files wait: then (and not before) the second chunk is asked for.
    for (let i = 0; i < 14; i++) { releases.shift()?.(); await settle(); }
    expect(mocks.createUploads).toHaveBeenCalledTimes(1); // 3 still waiting
    releases.shift()?.();
    await settle();
    expect(mocks.createUploads).toHaveBeenCalledTimes(2);
  });

  it('chunks by the Project’s maxFilesPerBatch when it is lower than 20', async () => {
    grantAll();
    mocks.getIngestionCapabilities.mockResolvedValue({ ok: true, value: { ...DEFAULT_CAPABILITIES, maxFilesPerBatch: 2 } });
    const r = await startBatch(PID, [file('a.mp4', 1), file('b.mp4', 1), file('c.mp4', 1)]);
    expect(r.ok).toBe(true);
    await settle();
    await settle();
    expect(mocks.createUploads.mock.calls.map((c) => (c[1] as unknown[]).length)).toEqual([2, 1]);
  });

  it('uploads into the folder open in the grid; a picked folder nests under it', async () => {
    grantAll();
    const files = [file('a.mp4', 10), setFolderOf(file('b.mp4', 10), 'Day 1')];
    expect((await startBatch(PID, files, undefined, 'Documentation/Storyboard')).ok).toBe(true);
    await settle();
    await settle();
    const sent = mocks.createUploads.mock.calls.flatMap((c) => c[1] as { name: string; folder?: string }[]);
    expect(sent.map((s) => [s.name, s.folder])).toEqual([
      ['a.mp4', 'Documentation/Storyboard'], ['b.mp4', 'Documentation/Storyboard/Day 1']]);
  });

  it('rejects a file whose folder breaks the server’s rules, with the reason, and requests only the rest', async () => {
    grantAll();
    const deep = Array.from({ length: 9 }, (_, i) => `d${i}`).join('/');
    await startBatch(PID, [
      setFolderOf(file('up.mp4', 1), '../x'),
      setFolderOf(file('deep.mp4', 1), deep),
      setFolderOf(file('ok.mp4', 1), 'Footage'),
    ]);
    const [up, tooDeep, ok] = getProjectEntries(PID);
    expect(up).toMatchObject({ status: 'failed', rejected: true });
    expect(up.error).toMatch(/^Folder not allowed: ".." is not a folder name/);
    expect(tooDeep).toMatchObject({ status: 'failed', rejected: true });
    expect(tooDeep.error).toMatch(/8 levels/);
    await settle();
    expect(mocks.createUploads).toHaveBeenCalledTimes(1);
    expect(mocks.createUploads.mock.calls[0][1]).toEqual([{ name: 'ok.mp4', size: 1, contentType: 'video/mp4', folder: 'Footage' }]);
    expect(ok.status).not.toBe('failed');
  });

  it('Retry failed re-queues every failed (not rejected) file of the Project', async () => {
    mocks.createUploads.mockResolvedValueOnce({ ok: false, kind: 'error', status: 503, code: 'x', message: 'Try later.' });
    await startBatch(PID, [file('a.mp4', 1), file('b.mp4', 1), file('', 0)]);
    await settle();
    expect(getProjectEntries(PID).map((e) => e.status)).toEqual(['failed', 'failed', 'failed']);
    grantAll();
    retryFailed(PID);
    await settle();
    await settle();
    expect(getProjectEntries(PID).map((e) => e.status)).toEqual(['ready', 'ready', 'failed']);
    expect(mocks.createUploads).toHaveBeenCalledTimes(2);
    expect((mocks.createUploads.mock.calls[1][1] as unknown[]).length).toBe(2); // one chunk for both
  });

  it('rejects empty and over-limit files before any request and requests only the rest', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [singleGrant(0)], errors: {} } });
    const r = await startBatch(PID, [
      file('empty.mp4', 0),
      file('huge.png', DEFAULT_CAPABILITIES.limits.image + 1, 'image/png'),
      file('ok.mp4', 10),
    ]);
    expect(r.ok).toBe(true);
    const [empty, huge, ok] = getProjectEntries(PID);
    expect(empty).toMatchObject({ status: 'failed', rejected: true, error: 'The file is empty.' });
    expect(huge).toMatchObject({ status: 'failed', rejected: true, kind: 'image' });
    expect(huge.error).toMatch(/over the 50 MB limit for image files/);
    expect(ok.status).not.toBe('failed');
    expect(mocks.createUploads).toHaveBeenCalledTimes(1);
    expect(mocks.createUploads.mock.calls[0][1]).toEqual([{ name: 'ok.mp4', size: 10, contentType: 'video/mp4' }]);
  });

  it('classifies an unknown type as a document with the document limit', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    await startBatch(PID, [file('notes.xyz', DEFAULT_CAPABILITIES.limits.document + 1, '')]);
    const [e] = getProjectEntries(PID);
    expect(e).toMatchObject({ kind: 'document', status: 'failed', rejected: true, contentType: 'application/octet-stream' });
  });
});

describe('single PUT', () => {
  it('goes queued → uploading → registering → ready and registers with the grant asset id', async () => {
    let releasePut: () => void = () => {};
    put.mockImplementation(
      (_url, body, opts) =>
        new Promise((resolve) => {
          opts.onProgress?.(Math.floor(body.size / 2));
          releasePut = () => resolve({ etag: '"e"' });
        }),
    );
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [singleGrant(0, 'video:A')], errors: {} } });
    const r = await startBatch(PID, [file('clip.mp4', 100)]);
    const id = r.ok ? r.fileIds[0] : '';
    expect(getEntry(id)?.status).toBe('queued');
    await settle();
    expect(getEntry(id)).toMatchObject({ status: 'uploading', pct: 50, assetId: 'video:A' });
    expect(put.mock.calls[0][0]).toBe('https://s3/put/0');
    expect(put.mock.calls[0][2].headers).toEqual({ 'Content-Type': 'video/mp4' });
    expect(isTransferring()).toBe(true);
    releasePut();
    await settle();
    expect(mocks.registerAsset).toHaveBeenCalledWith(PID, {
      assetId: 'video:A', fileName: 'clip.mp4', contentType: 'video/mp4', size: 100,
    });
    expect(getEntry(id)).toMatchObject({ status: 'ready', pct: 100, asset: { assetId: 'video:A' } });
    expect(isTransferring()).toBe(false);
    expect(batchSummary(r.ok ? r.batchId : '')).toEqual({ total: 1, ready: 1, failed: 0, rejected: 0, canceled: 0, done: true });
  });

  it('marks a refused index failed (too_large is rejected: no retry) and uploads the others', async () => {
    mocks.createUploads.mockResolvedValue({
      ok: true,
      value: { uploads: [singleGrant(1)], errors: { '0': { code: 'too_large' } } },
    });
    const r = await startBatch(PID, [file('a.mp4', 5), file('b.mp4', 5)]);
    await settle();
    const [a, b] = getProjectEntries(PID);
    expect(a).toMatchObject({ status: 'failed', rejected: true });
    expect(a.error).toMatch(/Too large/);
    expect(b.status).toBe('ready');
    expect(batchSummary(r.ok ? r.batchId : '')).toMatchObject({ ready: 1, rejected: 1, done: true });
  });

  it('fails with "could not be verified" when registration answers 409 object_missing', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [singleGrant(0)], errors: {} } });
    mocks.registerAsset.mockResolvedValue({ ok: false, kind: 'error', status: 409, code: 'object_missing', message: 'missing' });
    await startBatch(PID, [file('a.mp4', 5)]);
    await settle();
    expect(getProjectEntries(PID)[0]).toMatchObject({ status: 'failed', error: 'The upload could not be verified.' });
  });

  it('fails every file when the grant request is denied, naming the lost access', async () => {
    mocks.createUploads.mockResolvedValue({ ok: false, kind: 'not_found' });
    await startBatch(PID, [file('a.mp4', 5), file('b.mp4', 5)]);
    await settle();
    expect(getProjectEntries(PID).map((e) => [e.status, e.error])).toEqual([
      ['failed', 'You no longer have access to this project.'],
      ['failed', 'You no longer have access to this project.'],
    ]);
  });
});

describe('multipart', () => {
  const SIZE = 25;
  function multipart(index: number, assetId: string, uploadId: string) {
    return {
      index, assetId, key: `projects/${PID}/${assetId}/big.mp4`, mode: 'multipart', uploadId, partSize: 10,
      parts: [1, 2, 3].map((n) => ({ partNumber: n, url: `https://s3/${uploadId}/part${n}` })),
    };
  }

  it('uploads the parts, completes with their ETags, then registers', async () => {
    put.mockImplementation(async (url) => ({ etag: `"${url.slice(-5)}"` }));
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [multipart(0, 'video:M', 'U1')], errors: {} } });
    await startBatch(PID, [file('big.mp4', SIZE)]);
    await settle();
    expect(put).toHaveBeenCalledTimes(3);
    expect(mocks.completeUpload).toHaveBeenCalledWith(PID, 'video:M', 'U1', [
      { partNumber: 1, etag: '"part1"' },
      { partNumber: 2, etag: '"part2"' },
      { partNumber: 3, etag: '"part3"' },
    ]);
    expect(mocks.registerAsset).toHaveBeenCalledTimes(1);
    expect(getProjectEntries(PID)[0].status).toBe('ready');
  });

  it('fails after a part exhausts its retries, aborts the upload, and Retry gets a new grant and asset id', async () => {
    put.mockImplementation(async (url) => {
      if (url.includes('U1') && url.endsWith('part2')) throw new Error('network');
      return { etag: '"e"' };
    });
    mocks.createUploads
      .mockResolvedValueOnce({ ok: true, value: { uploads: [multipart(0, 'video:M1', 'U1')], errors: {} } })
      .mockResolvedValueOnce({ ok: true, value: { uploads: [multipart(0, 'video:M2', 'U2')], errors: {} } });
    const r = await startBatch(PID, [file('big.mp4', SIZE)]);
    const id = r.ok ? r.fileIds[0] : '';
    await settle();
    expect(getEntry(id)).toMatchObject({ status: 'failed', error: 'Part 2 failed after 3 retries.' });
    expect(put.mock.calls.filter((c) => String(c[0]).endsWith('U1/part2'))).toHaveLength(4);
    expect(mocks.abortUpload).toHaveBeenCalledWith(PID, 'video:M1', 'U1');
    expect(mocks.completeUpload).not.toHaveBeenCalled();
    expect(mocks.registerAsset).not.toHaveBeenCalled();

    retry(id);
    expect(getEntry(id)).toMatchObject({ status: 'queued', assetId: undefined });
    await settle();
    expect(mocks.createUploads).toHaveBeenCalledTimes(2);
    expect(getEntry(id)).toMatchObject({ status: 'ready', assetId: 'video:M2' });
    expect(mocks.completeUpload).toHaveBeenCalledWith(PID, 'video:M2', 'U2', expect.any(Array));
  });

  it('Cancel stops the transfer, aborts the multipart upload, and never registers', async () => {
    put.mockImplementation(
      (_url, _body, { signal }) =>
        new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(Object.assign(new Error('x'), { name: 'AbortError' })));
        }),
    );
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [multipart(0, 'video:C', 'UC')], errors: {} } });
    const r = await startBatch(PID, [file('big.mp4', SIZE)]);
    const id = r.ok ? r.fileIds[0] : '';
    await settle();
    expect(getEntry(id)?.status).toBe('uploading');
    cancel(id);
    await settle();
    expect(getEntry(id)?.status).toBe('canceled');
    expect(mocks.abortUpload).toHaveBeenCalledWith(PID, 'video:C', 'UC');
    expect(mocks.registerAsset).not.toHaveBeenCalled();
    // Retry is for failed files only.
    retry(id);
    expect(getEntry(id)?.status).toBe('canceled');
    expect(batchSummary(r.ok ? r.batchId : '')).toMatchObject({ canceled: 1, done: true });
  });
});

describe('cancel before transfer and the leave-page guard', () => {
  it('a file cancelled while its grant is being issued is never uploaded', async () => {
    let grant: (v: unknown) => void = () => {};
    mocks.createUploads.mockReturnValue(new Promise((r) => { grant = r; }));
    const r = await startBatch(PID, [file('a.mp4', 5)]);
    const id = r.ok ? r.fileIds[0] : '';
    cancel(id);
    grant({ ok: true, value: { uploads: [singleGrant(0)], errors: {} } });
    await settle();
    expect(getEntry(id)?.status).toBe('canceled');
    expect(put).not.toHaveBeenCalled();
  });

  it('arms beforeunload exactly while a file is in progress', async () => {
    let release: () => void = () => {};
    put.mockImplementation(() => new Promise((resolve) => { release = () => resolve({ etag: '"e"' }); }));
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [singleGrant(0)], errors: {} } });
    const idle = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(idle);
    expect(idle.defaultPrevented).toBe(false);

    await startBatch(PID, [file('a.mp4', 5)]);
    await settle();
    const busy = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(busy);
    expect(busy.defaultPrevented).toBe(true);

    release();
    await settle();
    const done = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(done);
    expect(done.defaultPrevented).toBe(false);
  });
});

describe('a "this chat only" batch (session visibility, asset-lineage D2)', () => {
  beforeEach(() => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [singleGrant(0, 'video:s1')], errors: {} } });
  });

  it('registers the asset with the session visibility and keeps the file out of the Project list', async () => {
    const r = await startBatch(PID, [file('a.mp4', 5)], { visibility: 'session', sessionId: 'S1' });
    expect(r.ok).toBe(true);
    await settle();
    expect(mocks.registerAsset).toHaveBeenCalledWith(PID, {
      assetId: 'video:s1',
      fileName: 'a.mp4',
      contentType: 'video/mp4',
      size: 5,
      visibility: 'session',
      sessionId: 'S1',
    });
    const id = (r as { fileIds: string[] }).fileIds[0];
    expect(getEntry(id)).toMatchObject({ status: 'ready', hidden: true });
    // The transfer itself is unchanged, and the Project's upload list never shows it.
    expect(put).toHaveBeenCalledTimes(1);
    expect(getProjectEntries(PID)).toEqual([]);
  });

  it('carries the session fields through a Retry', async () => {
    mocks.registerAsset.mockResolvedValueOnce({ ok: false, kind: 'error', status: 500, code: 'boom', message: 'nope' });
    const r = await startBatch(PID, [file('a.mp4', 5)], { visibility: 'session', sessionId: 'S1' });
    await settle();
    const id = (r as { fileIds: string[] }).fileIds[0];
    expect(getEntry(id)?.status).toBe('failed');
    retry(id);
    await settle();
    expect(getEntry(id)?.status).toBe('ready');
    expect(mocks.registerAsset).toHaveBeenCalledTimes(2);
    expect(mocks.registerAsset.mock.calls[1][1]).toMatchObject({ visibility: 'session', sessionId: 'S1' });
  });

  it('an ordinary batch sends no visibility and stays listed in the Project', async () => {
    const r = await startBatch(PID, [file('a.mp4', 5)]);
    await settle();
    expect(mocks.registerAsset.mock.calls[0][1]).toEqual({
      assetId: 'video:s1', fileName: 'a.mp4', contentType: 'video/mp4', size: 5,
    });
    expect(getEntry((r as { fileIds: string[] }).fileIds[0])?.hidden).toBeUndefined();
    expect(getProjectEntries(PID)).toHaveLength(1);
  });
});
