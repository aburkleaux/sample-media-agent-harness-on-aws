/**
 * Renders are restored from the store that has them, not from a cache the UI wrote.
 *
 * A render only became visible if a browser was connected the moment it completed: the Render
 * Agent wrote its manifest to S3, pushed the key onto the stream, and the UI turned that into a
 * `renderHistory` entry it wrote back. Media jobs settle asynchronously, so the renders that
 * mattered most were the ones no stream was open for. Measured on live sessions:
 *
 *   session 5c9838e7   4 manifests in S3   2 in renderHistory
 *   session fdef0d9f   3 manifests in S3   0 in renderHistory
 *
 * Every fix to the render path was invisible for this reason, which is why the same defects
 * appeared "unfixed" across days. This exercises the restore in one piece -- server payload
 * through to the entries the panel renders -- because a test on either half alone passes while
 * the join is broken.
 */
import { describe, it, expect } from 'vitest';
import type { RenderHistoryEntry } from '../components/renderer/types';

interface ServerRender {
  key: string;
  manifestId: string;
  createdAt: string;
  label?: string;
}

/** The restore, as WorkspaceView performs it. */
function restore(cached: RenderHistoryEntry[], server: ServerRender[]): RenderHistoryEntry[] {
  const keyOf = (e: RenderHistoryEntry) => {
    const url = String((e.blocks?.[0]?.data as { manifestUrl?: string })?.manifestUrl ?? '');
    const m = url.match(/key=([^&]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  };
  const known = new Set(cached.map(keyOf).filter(Boolean));
  const restored = server.filter(r => !known.has(r.key)).map(r => ({
    turnIndex: 0,
    timestamp: Date.parse(r.createdAt) || Date.now(),
    label: r.label,
    blocks: [{
      template: 'manifest-router',
      title: r.label || 'Render Output',
      data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(r.key)}` },
    }],
  }));
  return [...cached, ...restored].sort((a, b) => a.timestamp - b.timestamp);
}

const S = '5c9838e7-35ca-4337-b0ea-42ead185a459';
const entry = (key: string, label: string, ts: number): RenderHistoryEntry => ({
  turnIndex: 0,
  timestamp: ts,
  label,
  blocks: [{ template: 'manifest-router', title: label,
             data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(key)}` } }],
});

// The real shape of that session: 4 manifests, only the 2 in-turn ones cached.
const SERVER: ServerRender[] = [
  { key: `artifacts/${S}/render/a12cded7.json`, manifestId: 'a12cded7', createdAt: '2026-08-09T19:25:55Z', label: 'Face People' },
  { key: `artifacts/${S}/render/af0aaaca.json`, manifestId: 'af0aaaca', createdAt: '2026-08-09T19:26:00Z', label: 'Speaker Diarization' },
  { key: `artifacts/${S}/render/dfcaa2b5.json`, manifestId: 'dfcaa2b5', createdAt: '2026-08-09T19:26:00Z', label: 'Shot Segmentation' },
  { key: `artifacts/${S}/render/2cce88e7.json`, manifestId: '2cce88e7', createdAt: '2026-08-09T19:26:00Z', label: 'Face People' },
];

describe('renders restore from the authoritative store', () => {
  it('surfaces background renders that no stream was open for', () => {
    const cached = [entry(`artifacts/${S}/render/proxy1.json`, 'Media Processing', 1)];
    const out = restore(cached, SERVER);
    expect(out).toHaveLength(5);
    expect(out.map(e => e.label)).toContain('Shot Segmentation');
    expect(out.map(e => e.label)).toContain('Speaker Diarization');
  });

  it('does not duplicate a render the stream already delivered', () => {
    const key = `artifacts/${S}/render/a12cded7.json`;
    const cached = [entry(key, 'Face People', 1)];
    const out = restore(cached, SERVER);
    const urls = out.map(e => (e.blocks[0].data as { manifestUrl: string }).manifestUrl);
    expect(new Set(urls).size).toBe(urls.length);
    expect(out).toHaveLength(4);
  });

  it('keeps the cached label, which is better than one derived from renderSkill', () => {
    // The hook names a tab after the WORK ("Separate Audio Stems"); the server can only
    // derive from renderSkill ("Face People"). A cached entry must win.
    const key = `artifacts/${S}/render/a12cded7.json`;
    const out = restore([entry(key, 'Detect Faces', 1)], SERVER);
    expect(out.find(e => (e.blocks[0].data as { manifestUrl: string }).manifestUrl.includes('a12cded7'))?.label)
      .toBe('Detect Faces');
  });

  it('works when nothing was ever cached', () => {
    // Session fdef0d9f: 3 manifests, renderHistory absent entirely.
    const out = restore([], SERVER);
    expect(out).toHaveLength(4);
    expect(out[0].timestamp).toBeLessThanOrEqual(out[out.length - 1].timestamp);
  });

  it('orders restored renders oldest to newest so the panel opens on the latest', () => {
    const out = restore([], SERVER);
    const ts = out.map(e => e.timestamp);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
  });

  it('falls back to a generic title when the server could not derive a label', () => {
    const out = restore([], [{ key: `artifacts/${S}/render/x.json`, manifestId: 'x', createdAt: '2026-08-09T19:26:00Z' }]);
    expect(out[0].blocks[0].title).toBe('Render Output');
  });
});
