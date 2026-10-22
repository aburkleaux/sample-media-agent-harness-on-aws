/**
 * @vitest-environment jsdom
 *
 * openCitation unit tests (Component 5, media-harness-search-qa-rendering; Req 2.8, 2.9, 2.12).
 *
 * `openCitation` reads the asset status and mints a URL in parallel, maps every Req 2.9 refusal to its
 * exact inline message naming the Display_Name, preflight-loads the media (one fresh-URL retry on
 * failure), and on success dispatches `lab:open-asset` carrying the Asset_Reference. The API, the
 * preflight loader, the clock, the cache, and the dispatch are all injected, so these tests touch no
 * network and no real media element.
 *
 * Covers Requirements 2.8, 2.9, 2.12.
 */
import { describe, it, expect, vi } from 'vitest';
import {
  openCitation,
  openAssetAt,
  OPEN_ASSET_EVENT,
  type OpenCitationDeps,
  type VerifiedCitationRecord,
} from './openCitation';
import type { ProjectsResult, AssetDetail, AssetUrlsResponse } from './api';

function record(): VerifiedCitationRecord {
  return {
    key: 'nba.mp4|5000|10000',
    text: '[nba.mp4 @ 00:05\u201300:10]',
    display_name: 'nba.mp4',
    start_ms: 5000,
    end_ms: 10000,
    verified: true,
    ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null },
    kind: 'video',
    asset_display_name: 'nba.mp4',
  };
}

function assetOk(status: AssetDetail['status'] = 'indexed'): ProjectsResult<AssetDetail> {
  return { ok: true, value: { assetId: 'a1', displayName: 'nba.mp4', kind: 'video', status } };
}

function urlsOk(url = 'https://cdn.example/u1'): ProjectsResult<AssetUrlsResponse> {
  return { ok: true, value: { urls: { a1: { source: { url, expiresAt: 0 } } }, errors: {} } };
}

function makeDeps(over: Partial<OpenCitationDeps> = {}): OpenCitationDeps {
  return {
    getAsset: vi.fn(async () => assetOk()),
    getAssetUrls: vi.fn(async () => urlsOk()),
    preloadMedia: vi.fn(async () => true),
    dispatch: vi.fn(),
    now: () => 1000,
    cache: new Map(),
    ...over,
  };
}

describe('openCitation — Req 2.9 refusals', () => {
  it('refuses with "no longer have access" when the status read is not_found, and opens no tab', async () => {
    const deps = makeDeps({ getAsset: vi.fn(async () => ({ ok: false as const, kind: 'not_found' as const })) });
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: false, message: "Can't open nba.mp4: you no longer have access to this project." });
    expect(deps.dispatch).not.toHaveBeenCalled();
  });

  it('refuses with "no longer have access" when the URL route returns a per-id project_not_found', async () => {
    const deps = makeDeps({
      getAssetUrls: vi.fn(async () => ({
        ok: true as const,
        value: { urls: {}, errors: { a1: { code: 'project_not_found', message: 'gone' } } },
      })),
    });
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: false, message: "Can't open nba.mp4: you no longer have access to this project." });
    expect(deps.dispatch).not.toHaveBeenCalled();
  });

  it('refuses with "was removed" when the asset status is removed', async () => {
    const deps = makeDeps({ getAsset: vi.fn(async () => assetOk('removed')) });
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: false, message: "Can't open nba.mp4: it was removed from the project." });
    expect(deps.dispatch).not.toHaveBeenCalled();
  });

  it('refuses with "could not be loaded" when the URL route errors', async () => {
    const deps = makeDeps({
      getAssetUrls: vi.fn(async () => ({
        ok: false as const,
        kind: 'error' as const,
        status: 500,
        code: 'boom',
        message: 'server error',
      })),
    });
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: false, message: "Can't open nba.mp4: it could not be loaded." });
    expect(deps.dispatch).not.toHaveBeenCalled();
  });
});

describe('openCitation — preflight and open', () => {
  it('retries the URL mint exactly once when the first preflight fails, then opens', async () => {
    const preloadMedia = vi.fn(async () => false).mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const deps = makeDeps({ preloadMedia });
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: true });
    expect(deps.getAssetUrls).toHaveBeenCalledTimes(2); // initial mint + one refresh
    expect(preloadMedia).toHaveBeenCalledTimes(2);
    expect(deps.dispatch).toHaveBeenCalledTimes(1);
  });

  it('refuses after the single refresh if the second preflight also fails', async () => {
    const deps = makeDeps({ preloadMedia: vi.fn(async () => false) });
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: false, message: "Can't open nba.mp4: it could not be loaded." });
    expect(deps.getAssetUrls).toHaveBeenCalledTimes(2);
    expect(deps.dispatch).not.toHaveBeenCalled();
  });

  it('on success dispatches lab:open-asset carrying the Asset_Reference', async () => {
    const deps = makeDeps();
    const r = await openCitation(record(), 's1', deps);
    expect(r).toEqual({ ok: true });
    expect(deps.dispatch).toHaveBeenCalledTimes(1);
    const ev = (deps.dispatch as ReturnType<typeof vi.fn>).mock.calls[0][0] as CustomEvent;
    expect(ev.type).toBe(OPEN_ASSET_EVENT);
    expect(ev.detail.ref).toEqual(record().ref);
    expect(ev.detail.startMs).toBe(5000);
    expect(ev.detail.sessionId).toBe('s1');
  });

  it('carries a Moment\'s selected range through to lab:open-asset, and none for a Citation', async () => {
    const deps = makeDeps();
    const target = { ref: record().ref, kind: 'video' as const, displayName: 'nba.mp4', startMs: 74000 };
    await openAssetAt({ ...target, range: { start: 74, end: 96 } }, 's1', deps);
    const withRange = (deps.dispatch as ReturnType<typeof vi.fn>).mock.calls[0][0] as CustomEvent;
    expect(withRange.detail.range).toEqual({ start: 74, end: 96 });
    await openCitation(record(), 's1', deps);
    const citation = (deps.dispatch as ReturnType<typeof vi.fn>).mock.calls[1][0] as CustomEvent;
    expect(citation.detail).not.toHaveProperty('range');
  });

  it('never dispatches a chat:send (no agent turn starts)', async () => {
    const deps = makeDeps();
    await openCitation(record(), 's1', deps);
    const types = (deps.dispatch as ReturnType<typeof vi.fn>).mock.calls.map((c) => (c[0] as Event).type);
    expect(types).not.toContain('chat:send');
    expect(types).toEqual([OPEN_ASSET_EVENT]);
  });

  it('caches the minted URL under "<pid>/<assetId>" on a successful open', async () => {
    const cache = new Map();
    const deps = makeDeps({ cache });
    await openCitation(record(), 's1', deps);
    expect(cache.get('p1/a1')?.url).toBe('https://cdn.example/u1');
  });
});

// A document (PDF page) opens INSIDE the harness: an asset-player tab (lab:open-asset) that embeds the PDF at the
// hit's page. It used to open a new browser tab, which left the harness.
describe('openAssetAt — documents', () => {
  const docTarget = (startMs: number) => ({
    ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null }, kind: 'document' as const,
    displayName: 'report.pdf', startMs,
  });

  it('dispatches lab:open-asset (an in-harness tab) with the page span, without a media preflight', async () => {
    const deps = makeDeps();
    expect(await openAssetAt(docTarget(2 * 10_000 + 5), 's1', deps)).toEqual({ ok: true });
    expect(deps.preloadMedia).not.toHaveBeenCalled();
    const ev = (deps.dispatch as ReturnType<typeof vi.fn>).mock.calls[0][0] as CustomEvent;
    expect(ev.type).toBe(OPEN_ASSET_EVENT);
    expect(ev.detail).toMatchObject({ kind: 'document', displayName: 'report.pdf', startMs: 20_005, url: 'https://cdn.example/u1' });
  });

  it('refuses with the shared messages and opens nothing', async () => {
    const deps = makeDeps({ getAsset: vi.fn(async () => assetOk('removed')) });
    expect(await openAssetAt(docTarget(0), 's1', deps))
      .toEqual({ ok: false, message: "Can't open report.pdf: it was removed from the project." });
    expect(deps.dispatch).not.toHaveBeenCalled();
  });

  it('a verified page citation opens its document at the cited page', async () => {
    const rec: VerifiedCitationRecord = {
      ...record(), key: 'report.pdf|p4', text: '[report.pdf p.4]', display_name: 'report.pdf',
      start_ms: 30_000, end_ms: 30_000, page: 4, kind: 'document', asset_display_name: 'report.pdf',
    };
    const deps = makeDeps();
    expect(await openCitation(rec, 's1', deps)).toEqual({ ok: true });
    const ev = (deps.dispatch as ReturnType<typeof vi.fn>).mock.calls[0][0] as CustomEvent;
    expect(ev.detail).toMatchObject({ kind: 'document', startMs: 30_000 });
  });
});
