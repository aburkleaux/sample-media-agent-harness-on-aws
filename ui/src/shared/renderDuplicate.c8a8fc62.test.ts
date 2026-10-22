/**
 * Reproduction harness for the "render image duplicates" report (session c8a8fc62).
 *
 * Uses the SESSION's REAL render manifests (fetched from S3):
 *   - 01a0aa84: render-image gallery, 4 imgs — d3bcdf16, fb9c219c, c4930667, 0cb6aaa2
 *   - 01a0aa97: render-image gallery, 5 imgs — d3bcdf16 (SHARED), c974768b, 1b87bd30, c9a5ec0e, 9936248
 *   - 01a0aa9d: render-image gallery, 4 imgs — 2147f939, b7b65c88, 40c25b62, 8f35a1b7
 *   - sgD:      nle-timeline video (stitched_final.mp4)
 *
 * d3bcdf16 appears in BOTH 01a0aa84 and 01a0aa97 with a byte-identical durable CloudFront src.
 * These tests localise the duplicate: is it the SERVER compose, or the CLIENT merge?
 */
import { describe, it, expect } from 'vitest';
import { composeRenderTabs } from '../../server/renderSession';
import { mergeServerRenders } from '../views/WorkspaceView';
import type { RenderHistoryEntry } from '../components/renderer/types';

const CDN = 'https://dexamplecdn002.cloudfront.net/artifacts/external';
const img = (h: string, ext = 'png') => ({ src: `${CDN}/${h}.${ext}`, label: h, annotations: [] });

const genManifest = (cover: string, ext: string, imgs: Array<{ src: string; label: string; annotations: never[] }>) => ({
  template: 'image-annotated', renderSkill: 'render-image', annotations: [], tracks: [], clips: [],
  sourceKey: `artifacts/external/${cover}.${ext}`, src: `${CDN}/${cover}.${ext}`, images: imgs,
});

const m1 = genManifest('d3bcdf1619b5afa78d474c49', 'png', [
  img('d3bcdf1619b5afa78d474c49'), img('fb9c219c645bbb7a2a9a6b82', 'webp'),
  img('c4930667da5b830fe05237fd', 'jpg'), img('0cb6aaa2a6435636fc959aeb', 'webp')]);
const m2 = genManifest('d3bcdf1619b5afa78d474c49', 'png', [
  img('d3bcdf1619b5afa78d474c49'), img('c974768b932e6a4a5ab6e42e'),
  img('1b87bd30e5e347a1785c8587'), img('c9a5ec0e2c76d3afd82d71e4'), img('9936248895d7d61db341df87')]);
const m3 = genManifest('2147f939281caaa7380f23c2', 'png', [
  img('2147f939281caaa7380f23c2'), img('b7b65c8816476c0f9074d035', 'webp'),
  img('40c25b628a7425fbbef8c5fc'), img('8f35a1b762dfa548c0e2582e', 'webp')]);
const mVid = { template: 'nle-timeline', renderSkill: 'render-video',
  sourceKey: 'c8a8fc62-e9a9-41d1-8d54-9fe2cd6be788/stitched_final.mp4',
  tracks: [{ type: 'video', src: `${CDN}/stitched_final.mp4` }] };

const serverRenders = [
  { key: 'artifacts/c8/render/01a0aa84.json', createdAt: '2026-09-16T09:00:02Z', manifest: m1 },
  { key: 'artifacts/c8/render/01a0aa97.json', createdAt: '2026-09-16T09:21:55Z', manifest: m2 },
  { key: 'artifacts/c8/render/01a0aa9d.json', createdAt: '2026-09-16T09:27:33Z', manifest: m3 },
  { key: 'artifacts/c8/render/sgD.json',      createdAt: '2026-09-16T10:40:49Z', manifest: mVid },
];

function galleryImageSrcs(tabs: Array<Record<string, unknown>>): string[] {
  const g = tabs.find(t => t.key === 'composite:generated-images');
  const imgs = ((g?.manifest as { images?: Array<{ src?: string }> })?.images) ?? [];
  return imgs.map(i => String(i.src));
}

describe('c8a8fc62 render image duplicates', () => {
  it('SERVER compose: collapses 3 galleries into one, dedups the shared image', () => {
    const tabs = composeRenderTabs(serverRenders);
    const srcs = galleryImageSrcs(tabs);
    const dupes = srcs.filter((s, i) => srcs.indexOf(s) !== i);
    // 4 + (5-1 shared) + 4 = 12 unique images, d3bcdf16 exactly once.
    expect(dupes).toEqual([]);
    expect(srcs.length).toBe(12);
    expect(srcs.filter(s => s.includes('d3bcdf16')).length).toBe(1);
  });

  it('CLIENT merge: server gallery is authoritative — no lingering client preview duplicate', () => {
    const serverTabs = composeRenderTabs(serverRenders) as unknown as Parameters<typeof mergeServerRenders>[1];
    // Simulate the per-turn client previews the SSE created during the session: each image-gen
    // dispatch produced a preview carrying artifactUrls (the image URLs) + an inline manifest block.
    const cached: RenderHistoryEntry[] = [m1, m2, m3].map((m, i) => ({
      turnIndex: i, timestamp: Date.parse(serverRenders[i].createdAt),
      artifactUrls: (m.images as Array<{ src: string }>).map(im => im.src),
      blocks: [{ template: 'manifest-router', title: 'Render Output',
        data: { manifest: m } }], // inline manifest, NO manifestUrl → entryManifestKey === ''
    } as RenderHistoryEntry));
    const merged = mergeServerRenders(cached, serverTabs);
    // Count how many tabs contain generated images (should be exactly ONE: the server gallery).
    const genTabs = merged.filter(e => {
      const d = e.blocks?.[0]?.data as { manifest?: { renderSkill?: string } } | undefined;
      return d?.manifest?.renderSkill === 'render-image' || (e.artifactUrls?.length ?? 0) > 0;
    });
    expect(genTabs.length).toBe(1);
  });

  it('CLIENT merge: PRESERVES a preview whose artifacts the server has NOT listed yet (be60b441)', () => {
    const serverTabs = composeRenderTabs(serverRenders) as unknown as Parameters<typeof mergeServerRenders>[1];
    // An uploaded source video whose own render manifest the server has not composed yet.
    const notYetListed: RenderHistoryEntry = {
      turnIndex: 9, timestamp: Date.parse('2026-09-16T11:00:00Z'),
      artifactUrls: ['https://dexamplecdn002.cloudfront.net/artifacts/c8/uploads/my_source.mp4'],
      blocks: [{ template: 'manifest-router', title: 'Upload', data: { manifest: { tracks: [] } } }],
    } as RenderHistoryEntry;
    const merged = mergeServerRenders([notYetListed], serverTabs);
    // Not absorbed (its src is in no server composite) → preserved.
    expect(merged.some(e => (e.artifactUrls ?? []).includes(notYetListed.artifactUrls![0]))).toBe(true);
  });
});
