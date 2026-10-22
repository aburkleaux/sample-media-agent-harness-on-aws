/**
 * Byte transfer for the Upload_Client: the browser PUTs straight to the Ingest bucket with the presigned URLs
 * `POST /projects/:pid/uploads` issued, so no file bytes pass through the proxy.
 *
 *   * {@link xhrPut} — one presigned PUT through `XMLHttpRequest` (the only browser API that reports request-body
 *     progress). Sends exactly the signed headers, reads the `ETag` response header, aborts on the signal, and
 *     fails a transfer that moves no bytes for {@link STALL_MS}.
 *   * {@link uploadSingle} — a file under the multipart threshold: one PUT.
 *   * {@link uploadMultipart} — a larger file: its parts, at most {@link PART_CONCURRENCY} at a time, each retried
 *     up to 3 times (after 1, 2, and 4 s), then `complete` with every part's number and ETag in ascending order.
 *
 * Pure of React. The PUT and the sleep are injectable so the queue's state machine is testable without a network.
 */
import type { CompletedPart, MultipartUploadGrant, SingleUploadGrant } from '../api';

/** A transfer that moves no bytes for this long fails (Req 1.14). */
export const STALL_MS = 60_000;
/** At most this many parts of one file transfer at the same time (Req 1.35). */
export const PART_CONCURRENCY = 4;
/** Waits before the 1st, 2nd, and 3rd retry of a failed part (Req 1.36). */
export const PART_RETRY_DELAYS_MS: readonly number[] = [1000, 2000, 4000];

/** Raised when the caller cancelled the transfer. */
export class TransferAbortedError extends Error {
  constructor() {
    super('Upload cancelled');
    this.name = 'AbortError';
  }
}

/** Raised for any transfer failure other than a cancel. `message` is user-facing. */
export class TransferError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = 'TransferError';
    this.status = status;
  }
}

export interface PutOptions {
  headers?: Record<string, string>;
  /** Bytes of `body` sent so far. */
  onProgress?: (loaded: number) => void;
  signal?: AbortSignal;
}

export interface PutResult {
  /** The `ETag` response header, or null when the browser could not read it. */
  etag: string | null;
}

export type PutFn = (url: string, body: Blob, opts: PutOptions) => Promise<PutResult>;

export function isAbort(err: unknown): boolean {
  return err instanceof TransferAbortedError || (err instanceof Error && err.name === 'AbortError');
}

/** One presigned PUT through XHR. Resolves with the ETag on a 2xx; rejects otherwise. */
export const xhrPut: PutFn = (url, body, { headers, onProgress, signal }) =>
  new Promise<PutResult>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new TransferAbortedError());
      return;
    }
    const xhr = new XMLHttpRequest();
    let settled = false;
    let stallTimer: ReturnType<typeof setTimeout> | undefined;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      if (stallTimer !== undefined) clearTimeout(stallTimer);
      signal?.removeEventListener('abort', onAbort);
      fn();
    };
    const armStall = () => {
      if (stallTimer !== undefined) clearTimeout(stallTimer);
      stallTimer = setTimeout(() => {
        finish(() => reject(new TransferError('The upload stopped making progress.')));
        try { xhr.abort(); } catch { /* already closed */ }
      }, STALL_MS);
    };
    const onAbort = () => {
      finish(() => reject(new TransferAbortedError()));
      try { xhr.abort(); } catch { /* already closed */ }
    };

    xhr.open('PUT', url);
    for (const [k, v] of Object.entries(headers ?? {})) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e: ProgressEvent) => {
      armStall();
      if (e.lengthComputable || e.loaded >= 0) onProgress?.(Math.min(e.loaded, body.size));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(body.size);
        finish(() => resolve({ etag: xhr.getResponseHeader('ETag') }));
      } else {
        finish(() => reject(new TransferError(`The storage service refused the upload (${xhr.status}).`, xhr.status)));
      }
    };
    xhr.onerror = () => finish(() => reject(new TransferError('The upload failed: network error.')));
    xhr.onabort = () => finish(() => reject(new TransferAbortedError()));
    signal?.addEventListener('abort', onAbort);
    armStall();
    xhr.send(body);
  });

export interface TransferDeps {
  put?: PutFn;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  retryDelaysMs?: readonly number[];
  concurrency?: number;
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new TransferAbortedError());
      return;
    }
    const t = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new TransferAbortedError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/** Transfer a file with its single presigned PUT. `onProgress` receives bytes sent. */
export async function uploadSingle(
  grant: SingleUploadGrant,
  file: Blob,
  opts: { onProgress?: (loaded: number) => void; signal?: AbortSignal },
  deps: TransferDeps = {},
): Promise<void> {
  const put = deps.put ?? xhrPut;
  await put(grant.url, file, { headers: grant.headers, onProgress: opts.onProgress, signal: opts.signal });
}

/**
 * Transfer a file as the parts of its multipart upload, then call `complete` with every part's ETag in ascending
 * part-number order. Rejects with {@link TransferError} when a part still fails after its retries or a part's ETag
 * cannot be read, and with {@link TransferAbortedError} on cancel. Aborting the multipart upload on failure is the
 * caller's job (it owns the upload id's lifetime).
 */
export async function uploadMultipart(
  grant: MultipartUploadGrant,
  file: Blob,
  opts: {
    onProgress?: (loaded: number) => void;
    signal?: AbortSignal;
    complete: (parts: CompletedPart[]) => Promise<void>;
  },
  deps: TransferDeps = {},
): Promise<void> {
  const put = deps.put ?? xhrPut;
  const sleep = deps.sleep ?? defaultSleep;
  const delays = deps.retryDelaysMs ?? PART_RETRY_DELAYS_MS;
  const concurrency = Math.max(1, deps.concurrency ?? PART_CONCURRENCY);
  // One failed part stops the others: an inner controller follows the caller's and is aborted on failure.
  const inner = new AbortController();
  const outer = opts.signal;
  if (outer?.aborted) throw new TransferAbortedError();
  const followOuter = () => inner.abort();
  outer?.addEventListener('abort', followOuter, { once: true });
  const signal = inner.signal;

  const parts = [...grant.parts].sort((a, b) => a.partNumber - b.partNumber);
  const loaded = new Map<number, number>();
  const report = () => {
    let sum = 0;
    for (const v of loaded.values()) sum += v;
    opts.onProgress?.(Math.min(sum, file.size));
  };
  const etags = new Map<number, string>();
  let failure: unknown = null;
  let next = 0;

  const runPart = async (partNumber: number, url: string) => {
    const start = (partNumber - 1) * grant.partSize;
    const end = Math.min(file.size, start + grant.partSize);
    const body = file.slice(start, end);
    for (let attempt = 0; ; attempt++) {
      if (signal?.aborted) throw new TransferAbortedError();
      try {
        const res = await put(url, body, {
          signal,
          onProgress: (n) => {
            loaded.set(partNumber, n);
            report();
          },
        });
        if (!res.etag) throw new TransferError('The upload could not be confirmed: the storage service returned no ETag.');
        etags.set(partNumber, res.etag);
        loaded.set(partNumber, body.size);
        report();
        return;
      } catch (err) {
        if (isAbort(err) || signal?.aborted) throw new TransferAbortedError();
        loaded.set(partNumber, 0);
        report();
        if (attempt >= delays.length) {
          throw new TransferError(`Part ${partNumber} failed after ${delays.length} retries.`);
        }
        await sleep(delays[attempt], signal);
      }
    }
  };

  const worker = async () => {
    while (failure === null) {
      const i = next++;
      if (i >= parts.length) return;
      try {
        await runPart(parts[i].partNumber, parts[i].url);
      } catch (err) {
        if (failure === null) {
          failure = err;
          inner.abort();
        }
        return;
      }
    }
  };

  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, parts.length) }, worker));
  } finally {
    outer?.removeEventListener('abort', followOuter);
  }
  if (outer?.aborted) throw new TransferAbortedError();
  if (failure !== null) throw failure;
  await opts.complete(parts.map((p) => ({ partNumber: p.partNumber, etag: etags.get(p.partNumber) as string })));
}
