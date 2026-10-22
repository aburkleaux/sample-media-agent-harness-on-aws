/**
 * The ingestion client routes under `/projects/:pid` (capabilities, uploads, complete/abort, registration,
 * S3 locations, remove, chat-copy, the asset list): method, path, body, unwrapping, and the error mapping
 * (`409 object_missing` stays an `error` with its code; the uniform 404 is `not_found`).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../auth/authFetch', () => ({ authFetch: vi.fn() }));
vi.mock('../lab-api', () => ({ getConfig: vi.fn() }));

import { authFetch } from '../auth/authFetch';
import { getConfig } from '../lab-api';
import {
  abortUpload,
  chatCopyAsset,
  completeUpload,
  createUploads,
  getIngestionCapabilities,
  listAssets,
  registerAsset,
  registerS3Location,
  removeAsset,
} from './api';

const mockFetch = authFetch as unknown as ReturnType<typeof vi.fn>;
const mockConfig = getConfig as unknown as ReturnType<typeof vi.fn>;
const PID = 'p1';

function res(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}
function lastCall(): { path: string; method: string; body: unknown } {
  const [path, init] = mockFetch.mock.calls[mockFetch.mock.calls.length - 1] as [string, RequestInit];
  return { path, method: init.method ?? 'GET', body: init.body ? JSON.parse(String(init.body)) : undefined };
}

beforeEach(() => {
  mockFetch.mockReset();
  mockConfig.mockReset();
  mockConfig.mockResolvedValue({ projectsEnabled: true });
});

describe('ingestion routes', () => {
  it('GET …/ingestion/capabilities', async () => {
    const caps = { maxFilesPerBatch: 20, limits: { video: 1, audio: 1, image: 1, document: 1 }, multipartThresholdBytes: 1, partSizeBytes: 1 };
    mockFetch.mockResolvedValue(res(200, caps));
    expect(await getIngestionCapabilities(PID)).toEqual({ ok: true, value: caps });
    expect(lastCall()).toMatchObject({ path: '/projects/p1/ingestion/capabilities', method: 'GET' });
  });

  it('POST …/uploads sends the files and returns grants and per-index errors', async () => {
    const grant = { index: 0, assetId: 'video:1', key: 'k', mode: 'single', url: 'u', headers: { 'Content-Type': 'video/mp4' } };
    mockFetch.mockResolvedValue(res(200, { uploads: [grant], errors: { '1': { code: 'too_large' } } }));
    const files = [{ name: 'a.mp4', size: 1, contentType: 'video/mp4' }, { name: 'b.mp4', size: 9, contentType: 'video/mp4' }];
    const r = await createUploads(PID, files);
    expect(lastCall()).toEqual({ path: '/projects/p1/uploads', method: 'POST', body: { files } });
    expect(r).toEqual({ ok: true, value: { uploads: [grant], errors: { '1': { code: 'too_large' } } } });
  });

  it('POST …/uploads/:assetId/complete and /abort', async () => {
    mockFetch.mockResolvedValue(res(200, { ok: true }));
    await completeUpload(PID, 'video:1', 'U1', [{ partNumber: 1, etag: '"e"' }]);
    expect(lastCall()).toEqual({
      path: '/projects/p1/uploads/video%3A1/complete',
      method: 'POST',
      body: { uploadId: 'U1', parts: [{ partNumber: 1, etag: '"e"' }] },
    });
    await abortUpload(PID, 'video:1', 'U1');
    expect(lastCall()).toEqual({ path: '/projects/p1/uploads/video%3A1/abort', method: 'POST', body: { uploadId: 'U1' } });
  });

  it('POST …/assets registers, and a 409 keeps its code', async () => {
    const view = { assetId: 'video:1', kind: 'video', displayName: 'a.mp4', status: 'queued' };
    mockFetch.mockResolvedValueOnce(res(200, view));
    const body = { assetId: 'video:1', fileName: 'a.mp4', contentType: 'video/mp4', size: 1 };
    expect(await registerAsset(PID, body)).toEqual({ ok: true, value: view });
    expect(lastCall()).toEqual({ path: '/projects/p1/assets', method: 'POST', body });
    mockFetch.mockResolvedValueOnce(res(409, { error: { code: 'object_missing' } }));
    expect(await registerAsset(PID, body)).toMatchObject({ ok: false, kind: 'error', status: 409, code: 'object_missing' });
  });

  it('POST …/s3-locations, …/remove, and …/chat-copy', async () => {
    mockFetch.mockResolvedValueOnce(res(200, { registered: [], skipped: [{ key: 'k', reason: 'r' }], errors: [] }));
    expect(await registerS3Location(PID, 's3://b/p/')).toEqual({
      ok: true,
      value: { registered: [], skipped: [{ key: 'k', reason: 'r' }], errors: [] },
    });
    expect(lastCall()).toEqual({ path: '/projects/p1/s3-locations', method: 'POST', body: { uri: 's3://b/p/' } });

    mockFetch.mockResolvedValueOnce(res(200, { ok: true }));
    expect(await removeAsset(PID, 'image:2')).toEqual({ ok: true, value: undefined });
    expect(lastCall()).toMatchObject({ path: '/projects/p1/assets/image%3A2/remove', method: 'POST' });

    const copy = { key: 'artifacts/S1/input/1-a.png', url: 'https://cdn/artifacts/S1/input/1-a.png', name: 'a.png', size: 3, contentType: 'image/png' };
    mockFetch.mockResolvedValueOnce(res(200, copy));
    expect(await chatCopyAsset(PID, 'image:2', 'S1')).toEqual({ ok: true, value: copy });
    expect(lastCall()).toEqual({ path: '/projects/p1/assets/image%3A2/chat-copy', method: 'POST', body: { sessionId: 'S1' } });

    mockFetch.mockResolvedValueOnce(res(404, { error: { code: 'project_not_found', message: 'Project not found' } }));
    expect(await chatCopyAsset(PID, 'image:2', 'S9')).toEqual({ ok: false, kind: 'not_found' });
  });

  it('GET …/assets pages with the cursor', async () => {
    mockFetch.mockResolvedValueOnce(res(200, { assets: [{ assetId: 'a' }], cursor: 'c 1' }));
    expect(await listAssets(PID)).toEqual({ ok: true, value: { assets: [{ assetId: 'a' }], cursor: 'c 1' } });
    expect(lastCall().path).toBe('/projects/p1/assets');
    mockFetch.mockResolvedValueOnce(res(200, { assets: [] }));
    expect(await listAssets(PID, 'c 1')).toEqual({ ok: true, value: { assets: [] } });
    expect(lastCall().path).toBe('/projects/p1/assets?cursor=c%201');
  });
});
