import { describe, it, expect, vi } from 'vitest';
// resolveSourceUrl reads the manifest through authFetch (the manifest is served by /api/artifact,
// which is authenticated when artifacts are private). Delegate to the test's fetch stub so the
// token plumbing is not exercised here.
const authFetch = vi.hoisted(() => vi.fn((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init)));
vi.mock('../auth/authFetch', () => ({ authFetch }));
import { prettyTabLabel, tabSourceUrl } from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

describe('prettyTabLabel', () => {
  it('falls back to an ordinal for a gateway tool id', () => {
    // The exact shape that leaked into the tab strip: a user-registered
    // gateway target `usr-<uuid>` (and its hyphen-title-cased form).
    expect(prettyTabLabel('usr-8ddf64af24be45c894a7a8c1f254c83f', 0)).toBe('Result 1');
    expect(prettyTabLabel('Usr 8ddf64af24be45c894a7a8c1f254c83f', 2)).toBe('Result 3');
  });

  it('falls back for a bare hash', () => {
    expect(prettyTabLabel('0aa61fc5e0b7d4c3', 0)).toBe('Result 1');
    expect(prettyTabLabel('', 4)).toBe('Result 5');
    expect(prettyTabLabel(undefined, 1)).toBe('Result 2');
  });

  it('keeps genuinely human labels', () => {
    expect(prettyTabLabel('Detect Faces', 0)).toBe('Detect Faces');
    expect(prettyTabLabel('Shot Segmentation', 1)).toBe('Shot Segmentation');
    expect(prettyTabLabel('flux-1 result', 0)).toBe('flux-1 result');
    expect(prettyTabLabel('Run Model', 0)).toBe('Run Model');
  });
});

describe('tabSourceUrl (per-tab copy-source)', () => {
  const mk = (over: Partial<RenderHistoryEntry>): RenderHistoryEntry =>
    ({ turnIndex: 0, blocks: [], timestamp: 0, ...over } as RenderHistoryEntry);

  it('prefers the canonical artifactUrls[0]', () => {
    expect(tabSourceUrl(mk({ artifactUrls: ['https://cdn/x.mp4', 'https://cdn/y.mp4'] }))).toBe('https://cdn/x.mp4');
  });

  it('falls back to the first media src in the composed manifest', () => {
    const entry = mk({ blocks: [{ template: 'nle', title: 't', data: { tracks: [{ src: 'https://cdn/clip.mp4' }] } }] });
    expect(tabSourceUrl(entry)).toBe('https://cdn/clip.mp4');
  });

  it('resolves the source from a server composite inline manifest (prefers the video track)', () => {
    // Server composites store the timeline under data.manifest (not data.tracks); the copy-source
    // button was hidden for them until tabSourceUrl learned to read data.manifest. Prefer the video.
    const entry = mk({ blocks: [{ template: 'manifest-router', title: 't', data: { sourceKey: 'sid/x.mp4', manifest: { tracks: [
      { id: 'a', type: 'audio', src: 'https://cdn/audio.wav' },
      { id: 'v', type: 'video', src: 'https://cdn/source.mp4' },
    ] } } }] });
    expect(tabSourceUrl(entry)).toBe('https://cdn/source.mp4');
  });

  it('returns undefined when there is no http source', () => {
    expect(tabSourceUrl(mk({ artifactUrls: ['s3://not-http'] }))).toBeUndefined();
    expect(tabSourceUrl(mk({}))).toBeUndefined();
  });
});

describe('resolveSourceUrl (copy-source resolution)', () => {
  it('returns the sync url when the entry has one', async () => {
    const { resolveSourceUrl } = await import('./SessionRendererPanel');
    const e = { turnIndex: 0, blocks: [], timestamp: 0, artifactUrls: ['https://cdn/x.mp4'] } as any;
    expect(await resolveSourceUrl(e)).toBe('https://cdn/x.mp4');
  });

  it('fetches the manifest-router manifest and pulls tracks[0].src', async () => {
    const { resolveSourceUrl } = await import('./SessionRendererPanel');
    const orig = globalThis.fetch;
    globalThis.fetch = (async () => ({ ok: true, json: async () => ({ tracks: [{ src: 'https://cdn/clip.mp4' }] }) })) as any;
    try {
      const e = { turnIndex: 0, timestamp: 0, blocks: [{ template: 'manifest-router', title: 't', data: { manifestUrl: '/api/artifact?key=k' } }] } as any;
      expect(await resolveSourceUrl(e)).toBe('https://cdn/clip.mp4');
      expect(authFetch).toHaveBeenCalledWith('/api/artifact?key=k');
    } finally { globalThis.fetch = orig; }
  });
});

describe('shortTabLabel', () => {
  it('keeps short names; cuts long ones in the middle so the distinct end and extension show', async () => {
    const { shortTabLabel, TAB_LABEL_MAX } = await import('./SessionRendererPanel');
    expect(shortTabLabel('Moments')).toBe('Moments');
    const long = 'Netflix_Open_Content_Meridian_clip_0023-0029_a810000_crop_srx056.mp4';
    const s = shortTabLabel(long);
    expect([...s]).toHaveLength(TAB_LABEL_MAX);
    expect(s.startsWith('Netflix_Open')).toBe(true);
    expect(s.endsWith('crop_srx056.mp4')).toBe(true);
    expect(s).toContain('…');
  });
});
