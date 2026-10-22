// @vitest-environment jsdom
//
// Feature: media-harness-search-qa-rendering, Property 13: No URL in SPA state
//
// For any `MomentsBlock`, verified `CitationRecordWire`s, a set of minted presigned-shaped
// URLs, and a sequence of card and citation events (mount, mint, refresh, mark edit, clear,
// crop, Expand, `openCitation`), none of the minted URLs and no `X-Amz-` substring appears in
// any PERSISTED SPA state:
//
//   - the `RenderHistoryEntry` blocks that `WorkspaceView` appends (`asset-player` and
//     `manifest-router`) and `leanRenderHistory` of the history;
//   - the gallery's `marks`, `crops` and `status` state;
//   - the render-history block data a verified citation / Moment Expand produces;
//   - any `Storage.setItem` value written during the sequence.
//
// A `MomentItem` and a `CitationRecordWire` carry NO media URL by construction — only an
// Asset_Reference (`project_id`, `asset_id`, `frame_s3_key`) and typed fields. The SPA mints
// short-lived presigned URLs at view time and holds them ONLY in transient, never-persisted
// handoffs: the in-memory `assetUrlCache`, a media element's `src`, and the one-shot
// `lab:open-asset` event detail (design inconsistency row 22). This test drives the real
// `openCitation` / `openAssetAt` path so a minted URL genuinely flows through that transient
// handoff, then asserts the URL never lands in any persisted structure.
//
// `leanRenderHistory` and `WorkspaceView`'s `openAssetInPanel` listener are mirrored here
// (not imported) because the former lives in the proxy's `ui/server/app.ts` and the latter is
// inside a React component; the mirrors are kept byte-faithful to the production code they copy.
//
// **Validates: Requirements 2.12, 3.26**

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';

import {
  openAssetAt,
  OPEN_ASSET_EVENT,
  type OpenAssetEventDetail,
  type OpenCitationDeps,
  type OpenAssetTarget,
} from './openCitation';
import type { ProjectsResult, AssetDetail, AssetUrlsResponse } from './api';
import type { CitationBlock, CitationRecordWire } from '../api';
import type {
  CropBox,
  MomentItem,
  MomentsBlock,
  RenderBlock,
  RenderHistoryEntry,
} from '../components/renderer/types';

// --------------------------------------------------------------------------- mirrored production code

const RIGHT_COLLAPSED_KEY = 'lab-right-collapsed';

/**
 * Byte-faithful mirror of `ui/server/app.ts::leanRenderHistory`: drops an inlined `manifest`
 * from each block's `data`, keeping every other field (and the lean refs) intact.
 */
function leanRenderHistory(history: unknown[]): unknown[] {
  if (!Array.isArray(history)) return [];
  return history.map((entry) => {
    if (!entry || typeof entry !== 'object') return entry;
    const e = entry as Record<string, unknown>;
    if (!Array.isArray(e.blocks)) return e;
    return {
      ...e,
      blocks: (e.blocks as unknown[]).map((block) => {
        if (!block || typeof block !== 'object') return block;
        const b = block as Record<string, unknown>;
        const data = b.data;
        if (!data || typeof data !== 'object' || !('manifest' in (data as Record<string, unknown>))) return b;
        const { manifest, ...leanData } = data as Record<string, unknown>;
        void manifest;
        return { ...b, data: leanData };
      }),
    };
  });
}

/**
 * Mirror of `WorkspaceView.openAssetInPanel`: appends an `asset-player` tab built from the
 * `lab:open-asset` detail. Crucially it stores `ref`/`kind`/`displayName`/`startMs` and NOT
 * `detail.url` (Req 2.12). Returns the write made to right-rail collapse state, so the test can
 * capture every `Storage.setItem` the listener performs.
 */
function openAssetInPanel(
  prev: RenderHistoryEntry[],
  detail: OpenAssetEventDetail,
  setItem: (key: string, value: string) => void,
  ts: number,
): RenderHistoryEntry[] {
  const { ref, kind, displayName, startMs } = detail;
  setItem(RIGHT_COLLAPSED_KEY, 'false');
  const entry: RenderHistoryEntry = {
    turnIndex: prev.length,
    blocks: [
      {
        template: 'asset-player',
        title: displayName,
        data: { ref, kind, displayName, startMs, origin: 'citation' },
      },
    ],
    timestamp: ts,
    label: displayName,
    status: 'ready',
  };
  return [...prev, entry];
}

// --------------------------------------------------------------------------------------- generators

// A safe alphabet for all structured text (ids, names, hit text): it can never spell the
// presigned markers, so any marker found in persisted state must have leaked from a minted URL.
const SAFE = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.,'.split('');
const safeChar = fc.constantFrom(...SAFE);
const safeText = fc
  .array(safeChar, { maxLength: 40 })
  .map((a) => a.join(''))
  .filter((s) => !s.includes('X-Amz-') && !s.includes('://'));
const safeIdent = fc
  .array(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789'.split('')), {
    minLength: 1,
    maxLength: 12,
  })
  .map((a) => a.join(''));

const PROJECT_ID = 'proj-fixture';

// Presigned-shaped URL: `https://` scheme and the exact `X-Amz-Signature` / `X-Amz-Credential`
// markers the invariant forbids anywhere in persisted state.
const mintedUrlArb: fc.Arbitrary<string> = fc
  .record({ bucket: safeIdent, key: safeIdent, cred: safeIdent, sig: safeIdent })
  .map(
    ({ bucket, key, cred, sig }) =>
      `https://${bucket}.s3.us-west-2.amazonaws.com/${key}` +
      `?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=${cred}%2F20260101&X-Amz-Signature=${sig}`,
  );

const kindArb = fc.constantFrom<'video' | 'image' | 'audio'>('video', 'image', 'audio');

const momentItemArb: fc.Arbitrary<MomentItem> = fc.record({
  id: safeIdent,
  ref: fc.record({
    project_id: fc.constant(PROJECT_ID),
    asset_id: safeIdent,
    frame_s3_key: fc.option(safeIdent, { nil: null }),
  }),
  kind: kindArb,
  display_name: safeText,
  start_ms: fc.integer({ min: 0, max: 3_600_000 }),
  end_ms: fc.integer({ min: 0, max: 3_600_000 }),
  source_index: fc.option(fc.constantFrom('shot', 'scene', 'utterance', 'topic'), { nil: null }),
  final_score: fc.double({ min: 0, max: 1, noNaN: true }),
  text: safeText,
  text_truncated: fc.boolean(),
  highlight_terms: fc.array(safeText, { maxLength: 4 }),
  citation: fc.option(safeText, { nil: null }),
});

const momentsBlockArb: fc.Arbitrary<MomentsBlock> = fc.record({
  v: fc.constant(1 as const),
  project_id: fc.constant(PROJECT_ID),
  turn_id: safeIdent,
  basis: fc.constantFrom<'cited' | 'searched'>('cited', 'searched'),
  truncated: fc.boolean(),
  items: fc.array(momentItemArb, { minLength: 1, maxLength: 6 }),
});

const verifiedCitationArb: fc.Arbitrary<CitationRecordWire> = fc.record({
  key: safeIdent,
  text: safeText,
  display_name: safeText,
  start_ms: fc.integer({ min: 0, max: 3_600_000 }),
  end_ms: fc.integer({ min: 0, max: 3_600_000 }),
  verified: fc.constant(true),
  ref: fc.record({
    project_id: fc.constant(PROJECT_ID),
    asset_id: safeIdent,
    frame_s3_key: fc.option(safeIdent, { nil: null }),
  }),
  kind: kindArb,
  asset_display_name: safeText,
});

const citationBlockArb: fc.Arbitrary<CitationBlock> = fc.record({
  v: fc.constant(1 as const),
  project_id: fc.constant(PROJECT_ID),
  truncated: fc.boolean(),
  items: fc.array(verifiedCitationArb, { minLength: 1, maxLength: 6 }),
});

const cropBoxArb: fc.Arbitrary<CropBox> = fc.record({
  x: fc.double({ min: 0, max: 1, noNaN: true }),
  y: fc.double({ min: 0, max: 1, noNaN: true }),
  w: fc.double({ min: 0, max: 1, noNaN: true }),
  h: fc.double({ min: 0, max: 1, noNaN: true }),
});

type EvSpec =
  | { kind: 'expand'; item: number }
  | { kind: 'openCitation'; item: number }
  | { kind: 'markEdit'; item: number; mIn: number; mOut: number }
  | { kind: 'markClear'; item: number }
  | { kind: 'crop'; item: number; box: CropBox }
  | { kind: 'status'; item: number; status: string };

const evSpecArb: fc.Arbitrary<EvSpec> = fc.oneof(
  fc.record({ kind: fc.constant('expand' as const), item: fc.nat() }),
  fc.record({ kind: fc.constant('openCitation' as const), item: fc.nat() }),
  fc.record({
    kind: fc.constant('markEdit' as const),
    item: fc.nat(),
    mIn: fc.integer({ min: 0, max: 100_000 }),
    mOut: fc.integer({ min: 0, max: 100_000 }),
  }),
  fc.record({ kind: fc.constant('markClear' as const), item: fc.nat() }),
  fc.record({ kind: fc.constant('crop' as const), item: fc.nat(), box: cropBoxArb }),
  fc.record({
    kind: fc.constant('status' as const),
    item: fc.nat(),
    status: fc.constantFrom('passed', 'flagged', 'pending'),
  }),
);

// --------------------------------------------------------------------------------------- open deps

function assetOk(assetId: string): ProjectsResult<AssetDetail> {
  return {
    ok: true,
    value: { assetId, displayName: 'fixture', kind: 'video', status: 'indexed' },
  };
}

/** getAssetUrls stub that always mints `url` for the requested id. */
function urlsOkFor(assetId: string, url: string): ProjectsResult<AssetUrlsResponse> {
  return { ok: true, value: { urls: { [assetId]: { source: { url, expiresAt: 0 } } }, errors: {} } };
}

// ----------------------------------------------------------------------------------------- helpers

/** JSON of a value, with Map support (the transient cache is excluded deliberately). */
function dump(value: unknown): string {
  return JSON.stringify(value);
}

describe('Property 13: no URL in SPA state', () => {
  it('keeps every minted presigned URL and X-Amz- marker out of all persisted SPA state', async () => {
    await fc.assert(
      fc.asyncProperty(
        momentsBlockArb,
        citationBlockArb,
        fc.array(mintedUrlArb, { minLength: 1, maxLength: 6 }),
        fc.array(evSpecArb, { maxLength: 24 }),
        async (moments, citations, mintedUrls, specs) => {
          const verified = citations.items; // all verified by construction

          // Persisted structures under test.
          // The Moments tab: a clip-gallery block carrying the MomentsBlock, plus a
          // manifest-router block whose inlined `manifest` leanRenderHistory will strip.
          const momentsBlocks: RenderBlock[] = [
            { template: 'clip-gallery', title: 'Moments', data: { galleryMoments: moments } },
            {
              template: 'manifest-router',
              title: 'Moments',
              data: {
                manifest: { galleryMoments: moments },
                sourceKey: `renders/${moments.turn_id}/moments.json`,
                memberKeys: moments.items.map((m) => m.ref.asset_id),
              },
            },
          ];
          let renderHistory: RenderHistoryEntry[] = [
            { turnIndex: 0, blocks: momentsBlocks, timestamp: 1000, label: 'Moments', status: 'ready' },
          ];

          // Gallery state, keyed by Moment id — ids and numbers only.
          const marks: Record<string, { in: number; out: number }[]> = {};
          const crops: Record<string, CropBox> = {};
          const status: Record<string, string> = {};

          // Every Storage.setItem value written during the sequence.
          const storageWrites: string[] = [];
          const setItem = (_key: string, value: string) => {
            storageWrites.push(value);
          };

          // The transient, never-persisted in-memory cache the open path writes into.
          const cache = new Map<string, { url: string; obtainedAt: number }>();

          // Minted URLs that actually flowed through the transient open handoff this run.
          const usedUrls: string[] = [];
          // The `lab:open-asset` detail of each successful open (transient; carries the URL).
          const openDetails: OpenAssetEventDetail[] = [];

          let mintCursor = 0;

          async function drive(target: OpenAssetTarget) {
            const url = mintedUrls[mintCursor % mintedUrls.length];
            mintCursor += 1;
            usedUrls.push(url);
            let captured: OpenAssetEventDetail | null = null;
            const deps: OpenCitationDeps = {
              getAsset: async () => assetOk(target.ref.asset_id),
              getAssetUrls: async () => urlsOkFor(target.ref.asset_id, url),
              preloadMedia: async () => true,
              dispatch: (e: Event) => {
                captured = (e as CustomEvent<OpenAssetEventDetail>).detail;
              },
              now: () => 2000,
              cache,
            };
            const res = await openAssetAt(target, 's1', deps);
            expect(res.ok).toBe(true);
            // Sanity: the minted URL really did flow through the transient event detail,
            // so its absence from persisted state below is a meaningful result.
            expect(captured).not.toBeNull();
            const detail = captured as unknown as OpenAssetEventDetail;
            expect(detail.url).toBe(url);
            openDetails.push(detail);
            // The WorkspaceView listener persists an asset-player tab WITHOUT the URL.
            renderHistory = openAssetInPanel(renderHistory, detail, setItem, 3000);
          }

          for (const s of specs) {
            if (moments.items.length === 0) break;
            const m = moments.items[s.item % moments.items.length];
            switch (s.kind) {
              case 'expand':
                await drive({
                  ref: m.ref,
                  kind: m.kind,
                  displayName: m.display_name,
                  startMs: m.start_ms,
                });
                break;
              case 'openCitation': {
                const c = verified[s.item % verified.length];
                await drive({
                  ref: c.ref!,
                  kind: c.kind!,
                  displayName: c.asset_display_name!,
                  startMs: c.start_ms,
                });
                break;
              }
              case 'markEdit':
                (marks[m.id] ??= []).push({
                  in: Math.min(s.mIn, s.mOut),
                  out: Math.max(s.mIn, s.mOut),
                });
                break;
              case 'markClear':
                delete marks[m.id];
                break;
              case 'crop':
                crops[m.id] = s.box;
                break;
              case 'status':
                status[m.id] = s.status;
                break;
            }
          }

          // The SPA persists the lean render history and the citation block to Storage.
          const lean = leanRenderHistory(renderHistory);
          setItem('lab-render-s1', dump(lean));
          setItem('lab-citations', dump(citations));

          // Everything considered persisted SPA state.
          const persisted = dump({
            renderHistory,
            lean,
            gallery: { marks, crops, status },
            citations,
            openedRefs: openDetails.map((d) => d.ref), // what the listener keeps of each open
          });

          // 1. No minted URL that flowed through the transient handoff appears in persisted state.
          for (const url of usedUrls) {
            expect(persisted.includes(url)).toBe(false);
            for (const w of storageWrites) expect(w.includes(url)).toBe(false);
          }

          // 2. No presigned marker appears in persisted state, nor in any Storage write.
          for (const marker of ['X-Amz-', 'X-Amz-Signature', 'X-Amz-Credential']) {
            expect(persisted.includes(marker)).toBe(false);
            for (const w of storageWrites) expect(w.includes(marker)).toBe(false);
          }

          // 3. The manifest-router block's inlined manifest was stripped by leanRenderHistory,
          //    while the no-URL Moments data survives — so persistence carries references, not URLs.
          const leanFirst = (lean[0] as RenderHistoryEntry).blocks;
          expect((leanFirst[1].data as Record<string, unknown>).manifest).toBeUndefined();
          expect((leanFirst[0].data as Record<string, unknown>).galleryMoments).toBeDefined();
        },
      ),
      { numRuns: 200 },
    );
  });
});
