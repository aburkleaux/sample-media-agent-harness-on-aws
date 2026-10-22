import { describe, it, expect } from 'vitest';
import { renderTabKey, manifestKeyOfUrl, sessionOfRenderKey } from './renderTabKey';
import type { RenderHistoryEntry } from './types';

const entry = (over: Partial<RenderHistoryEntry>, data: Record<string, unknown> = {}): RenderHistoryEntry => ({
  turnIndex: 0, timestamp: 5, blocks: [{ template: 'manifest-router', title: 't', data }], ...over,
});

describe('renderTabKey', () => {
  it('prefers the explicit key', () => {
    expect(renderTabKey(entry({ key: 'client:1' }, { manifestUrl: '/api/artifact?key=a' }))).toBe('client:1');
  });
  it('legacy entries: manifest key, then composite key / sourceKey, then request id, then urls', () => {
    expect(renderTabKey(entry({}, { manifestUrl: '/api/artifact?key=artifacts%2Fs%2Frender%2Fa.json' }))).toBe('artifacts/s/render/a.json');
    expect(renderTabKey(entry({}, { compositeKey: 'composite:asset:v1' }))).toBe('composite:asset:v1');
    expect(renderTabKey(entry({}, { manifest: {}, sourceKey: 's/v.mp4' }))).toBe('composite:s/v.mp4');
    expect(renderTabKey({ ...entry({ renderRequestId: 'r1' }), blocks: [] })).toBe('req:r1');
    expect(renderTabKey({ ...entry({ artifactUrls: ['u1', 'u2'] }), blocks: [] })).toBe('urls:u1|u2');
  });
});

describe('render keys', () => {
  it('manifestKeyOfUrl decodes the key query param', () => {
    expect(manifestKeyOfUrl('/api/artifact?key=artifacts%2Fs%2Frender%2Fa.json')).toBe('artifacts/s/render/a.json');
    expect(manifestKeyOfUrl('/api/artifact')).toBe('');
  });
  it('sessionOfRenderKey accepts only artifacts/<sid>/render/<name>.json', () => {
    expect(sessionOfRenderKey('artifacts/s-1/render/a.json')).toBe('s-1');
    expect(sessionOfRenderKey('artifacts/s-1/output/a.json')).toBeNull();
    expect(sessionOfRenderKey('artifacts/../render/a.json')).toBeNull();
  });
});
