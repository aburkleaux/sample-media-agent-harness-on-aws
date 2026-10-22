/**
 * @vitest-environment jsdom
 *
 * Upload transfers: the single presigned PUT (signed Content-Type, ETag, abort, progress) and the multipart driver
 * (at most 4 parts in flight, per-part retry x3 with backoff, ETags completed in ascending part order, one failed
 * part stops the rest, cancel).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  PART_CONCURRENCY,
  TransferAbortedError,
  TransferError,
  uploadMultipart,
  uploadSingle,
  xhrPut,
  type PutFn,
} from './transfer';
import type { MultipartUploadGrant, SingleUploadGrant } from '../api';

function blob(size: number): Blob {
  return new Blob([new Uint8Array(size)]);
}

function multipartGrant(size: number, partSize: number): MultipartUploadGrant {
  const n = Math.ceil(size / partSize);
  return {
    index: 0,
    assetId: 'video:1',
    key: 'projects/p1/video:1/big.mp4',
    mode: 'multipart',
    uploadId: 'U1',
    partSize,
    // Deliberately out of order: the driver sorts by part number.
    parts: Array.from({ length: n }, (_, i) => ({ partNumber: n - i, url: `https://s3/part${n - i}` })),
  };
}

const noSleep = () => Promise.resolve();

describe('uploadSingle', () => {
  it('PUTs the file once to the grant URL with exactly the signed headers', async () => {
    const put = vi.fn<PutFn>().mockResolvedValue({ etag: '"e"' });
    const grant: SingleUploadGrant = {
      index: 0, assetId: 'image:1', key: 'k', mode: 'single', url: 'https://s3/put', headers: { 'Content-Type': 'image/png' },
    };
    const file = blob(10);
    const onProgress = vi.fn();
    await uploadSingle(grant, file, { onProgress }, { put });
    expect(put).toHaveBeenCalledTimes(1);
    expect(put.mock.calls[0][0]).toBe('https://s3/put');
    expect(put.mock.calls[0][1]).toBe(file);
    expect(put.mock.calls[0][2].headers).toEqual({ 'Content-Type': 'image/png' });
    expect(put.mock.calls[0][2].onProgress).toBe(onProgress);
  });
});

describe('uploadMultipart', () => {
  it('slices the file per part, keeps at most 4 in flight, and completes with ETags in ascending order', async () => {
    const size = 10 * 5 + 3; // 11 parts of 5 bytes, the last holding 3
    const grant = multipartGrant(size, 5);
    let inFlight = 0;
    let peak = 0;
    const bodies = new Map<string, number>();
    const put: PutFn = async (url, body) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      bodies.set(url, body.size);
      await new Promise((r) => setTimeout(r, 1));
      inFlight--;
      return { etag: `"etag-${url.slice(-2)}"` };
    };
    const complete = vi.fn().mockResolvedValue(undefined);
    const progress: number[] = [];
    await uploadMultipart(grant, blob(size), { complete, onProgress: (n) => progress.push(n) }, { put, sleep: noSleep });

    expect(peak).toBeLessThanOrEqual(PART_CONCURRENCY);
    expect(peak).toBe(4);
    expect(bodies.get('https://s3/part1')).toBe(5);
    expect(bodies.get('https://s3/part11')).toBe(3);
    const parts = complete.mock.calls[0][0] as { partNumber: number; etag: string }[];
    expect(parts.map((p) => p.partNumber)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    expect(parts[0].etag).toBe('"etag-t1"');
    expect(progress[progress.length - 1]).toBe(size);
  });

  it('retries a failed part up to 3 times with 1, 2, and 4 s backoff, then succeeds', async () => {
    const grant = multipartGrant(10, 5);
    let part2Attempts = 0;
    const put: PutFn = async (url) => {
      if (url.endsWith('part2') && ++part2Attempts <= 3) throw new TransferError('boom', 500);
      return { etag: `"${url}"` };
    };
    const sleep = vi.fn().mockResolvedValue(undefined);
    const complete = vi.fn().mockResolvedValue(undefined);
    await uploadMultipart(grant, blob(10), { complete }, { put, sleep });
    expect(part2Attempts).toBe(4);
    expect(sleep.mock.calls.map((c) => c[0])).toEqual([1000, 2000, 4000]);
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it('fails the file when a part still fails after its third retry, and never completes', async () => {
    const grant = multipartGrant(20, 5);
    const put: PutFn = async (url) => {
      if (url.endsWith('part3')) throw new TransferError('boom', 500);
      return { etag: '"e"' };
    };
    const complete = vi.fn();
    await expect(uploadMultipart(grant, blob(20), { complete }, { put, sleep: noSleep })).rejects.toThrow(
      'Part 3 failed after 3 retries.',
    );
    expect(complete).not.toHaveBeenCalled();
  });

  it('treats a part with no readable ETag as failed', async () => {
    const grant = multipartGrant(5, 5);
    const put: PutFn = async () => ({ etag: null });
    await expect(
      uploadMultipart(grant, blob(5), { complete: vi.fn() }, { put, sleep: noSleep, retryDelaysMs: [] }),
    ).rejects.toThrow('Part 1 failed after 0 retries.');
  });

  it('stops every part and rejects as aborted when the caller cancels', async () => {
    const grant = multipartGrant(40, 5);
    const controller = new AbortController();
    const started: string[] = [];
    const put: PutFn = (url, _body, { signal }) =>
      new Promise((_resolve, reject) => {
        started.push(url);
        signal?.addEventListener('abort', () => reject(new TransferAbortedError()));
      });
    const complete = vi.fn();
    const run = uploadMultipart(grant, blob(40), { complete, signal: controller.signal }, { put, sleep: noSleep });
    await Promise.resolve();
    expect(started).toHaveLength(4);
    controller.abort();
    await expect(run).rejects.toBeInstanceOf(TransferAbortedError);
    expect(started).toHaveLength(4);
    expect(complete).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------------------------------ xhrPut

class FakeXhr {
  static last: FakeXhr | null = null;
  method = '';
  url = '';
  headers: Record<string, string> = {};
  status = 0;
  responseHeaders: Record<string, string> = {};
  upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  sent: unknown = null;
  aborted = false;
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }
  getResponseHeader(k: string) {
    return this.responseHeaders[k] ?? null;
  }
  send(body: unknown) {
    this.sent = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
}

describe('xhrPut', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeXhr.last = null;
  });

  it('PUTs with the signed Content-Type, reports progress, and resolves with the ETag header', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const body = blob(8);
    const onProgress = vi.fn();
    const p = xhrPut('https://s3/put', body, { headers: { 'Content-Type': 'video/mp4' }, onProgress });
    const xhr = FakeXhr.last as FakeXhr;
    expect(xhr.method).toBe('PUT');
    expect(xhr.headers).toEqual({ 'Content-Type': 'video/mp4' });
    expect(xhr.sent).toBe(body);
    xhr.upload.onprogress?.({ loaded: 4, lengthComputable: true } as ProgressEvent);
    expect(onProgress).toHaveBeenLastCalledWith(4);
    xhr.status = 200;
    xhr.responseHeaders.ETag = '"abc"';
    xhr.onload?.();
    await expect(p).resolves.toEqual({ etag: '"abc"' });
    expect(onProgress).toHaveBeenLastCalledWith(8);
  });

  it('rejects a non-2xx with a TransferError and aborts on the signal', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const p1 = xhrPut('https://s3/put', blob(1), {});
    const x1 = FakeXhr.last as FakeXhr;
    x1.status = 403;
    x1.onload?.();
    await expect(p1).rejects.toBeInstanceOf(TransferError);

    const controller = new AbortController();
    const p2 = xhrPut('https://s3/put', blob(1), { signal: controller.signal });
    const x2 = FakeXhr.last as FakeXhr;
    controller.abort();
    await expect(p2).rejects.toBeInstanceOf(TransferAbortedError);
    expect(x2.aborted).toBe(true);
  });
});
