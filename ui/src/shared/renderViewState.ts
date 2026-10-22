/**
 * renderViewState — the persisted timeline state of a render view (R8.2): the selected range, the
 * frame marker and the crop box. It lives on the render manifest itself (`manifest.viewState`), so a
 * view restores after a refresh from the same S3 object that defines the tab.
 *
 * SHARED between the proxy (`PATCH /sessions/:id/renders?key=` validates and merges it) and the SPA
 * (the timeline restores it on mount and reports changes). Pure, no runtime deps.
 *
 * Shape (every field optional; times in the media's own clock):
 *   range   {start, end}   seconds, 0 <= start < end
 *   frameMs number         milliseconds, >= 0
 *   crop    {x, y, w, h}   fractions of the frame, w and h > 0, box inside the frame
 *   search  {history, index}  a Moments tab's own searches (2026-10-11): `history` is the user's queries
 *                          in order (at most 10, each `{q, kinds?}`), `index` the one on screen — 0 is the
 *                          agent's original results, `i` is `history[i - 1]`. Queries only: results are
 *                          re-fetched when the tab reopens (they would go stale and blow the 4 KB PATCH).
 */

import { SEARCH_KINDS, type SearchKind } from './searchKinds';

export interface RenderViewRange { start: number; end: number }
export interface RenderViewCrop { x: number; y: number; w: number; h: number }
export type RenderViewSearchKind = SearchKind;
/** One query image of an image search (design 2026-10-18 §2): an image asset, or a frame of a project video. */
export interface RenderViewQueryImage { assetId?: string; frameKey?: string; sourceAssetId?: string; atMs?: number; label: string }
/** A text search `{q}` or an image search `{images}` (1..3, "match any"); `kinds` filters either. */
export type RenderViewSearchEntry =
  | { q: string; kinds?: RenderViewSearchKind[]; images?: undefined }
  | { images: RenderViewQueryImage[]; kinds?: RenderViewSearchKind[]; q?: undefined };
export interface RenderViewSearch { history: RenderViewSearchEntry[]; index: number }
export interface RenderViewState {
  range?: RenderViewRange;
  frameMs?: number;
  crop?: RenderViewCrop;
  search?: RenderViewSearch;
}

/** Most searches a Moments tab remembers, and the longest query (both keep the PATCH under 4 KB). */
export const VIEW_SEARCH_HISTORY_MAX = 10;
export const VIEW_SEARCH_QUERY_MAX = 200;
/** An image search's bounds: at most 3 images, each key and label short, so 10 entries still fit the PATCH. */
export const VIEW_SEARCH_IMAGES_MAX = 3;
export const VIEW_SEARCH_IMAGE_KEY_MAX = 400;
export const VIEW_SEARCH_IMAGE_LABEL_MAX = 80;
/** The serialized `search` the SPA keeps under (it drops the oldest entries past it); the PATCH cap is 4 KB. */
export const VIEW_SEARCH_JSON_BUDGET = 3_000;

const ASSET_ID = /^(video|image|audio|document):[0-9a-f-]{36}$/;

function parseQueryImage(v: unknown): RenderViewQueryImage | undefined {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined;
  const { assetId, frameKey, sourceAssetId, atMs, label } = v as Record<string, unknown>;
  if (typeof label !== 'string' || !label.trim() || [...label].length > VIEW_SEARCH_IMAGE_LABEL_MAX) return undefined;
  const out: RenderViewQueryImage = { label };
  if (typeof assetId === 'string') {
    if (!ASSET_ID.test(assetId) || frameKey !== undefined) return undefined;
    out.assetId = assetId;
  } else if (typeof frameKey === 'string') {
    if (!frameKey || frameKey.length > VIEW_SEARCH_IMAGE_KEY_MAX || /[%?#]/.test(frameKey)) return undefined;
    if (typeof sourceAssetId !== 'string' || !ASSET_ID.test(sourceAssetId)) return undefined;
    out.frameKey = frameKey;
    out.sourceAssetId = sourceAssetId;
  } else {
    return undefined;
  }
  if (atMs !== undefined) {
    if (!finite(atMs) || atMs < 0) return undefined;
    out.atMs = atMs;
  }
  return out;
}
const SEARCH_KIND_SET = new Set<string>(SEARCH_KINDS);

const MAX_SECONDS = 86_400; // the manifest parser's duration ceiling
const EPS = 1e-6;
const FIELDS = new Set(['range', 'frameMs', 'crop', 'search']);

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

function parseRange(v: unknown): RenderViewRange | undefined {
  if (!v || typeof v !== 'object') return undefined;
  const { start, end } = v as { start?: unknown; end?: unknown };
  if (!finite(start) || !finite(end)) return undefined;
  if (start < 0 || end <= start || end > MAX_SECONDS) return undefined;
  return { start, end };
}

function parseFrameMs(v: unknown): number | undefined {
  return finite(v) && v >= 0 && v <= MAX_SECONDS * 1000 ? v : undefined;
}

function parseCrop(v: unknown): RenderViewCrop | undefined {
  if (!v || typeof v !== 'object') return undefined;
  const { x, y, w, h } = v as { x?: unknown; y?: unknown; w?: unknown; h?: unknown };
  if (!finite(x) || !finite(y) || !finite(w) || !finite(h)) return undefined;
  if (x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > 1 + EPS || y + h > 1 + EPS) return undefined;
  return { x, y, w, h };
}

function parseSearch(v: unknown): RenderViewSearch | undefined {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined;
  const { history, index } = v as { history?: unknown; index?: unknown };
  if (!Array.isArray(history) || history.length > VIEW_SEARCH_HISTORY_MAX) return undefined;
  const out: RenderViewSearchEntry[] = [];
  for (const e of history) {
    if (!e || typeof e !== 'object') return undefined;
    const { q, kinds, images } = e as { q?: unknown; kinds?: unknown; images?: unknown };
    let entry: RenderViewSearchEntry;
    if (images !== undefined) {
      if (q !== undefined || !Array.isArray(images) || images.length < 1 || images.length > VIEW_SEARCH_IMAGES_MAX) return undefined;
      const parsed = images.map(parseQueryImage);
      if (parsed.some((i) => !i)) return undefined;
      entry = { images: parsed as RenderViewQueryImage[] };
    } else {
      if (typeof q !== 'string' || !q.trim() || [...q].length > VIEW_SEARCH_QUERY_MAX) return undefined;
      entry = { q };
    }
    if (kinds !== undefined) {
      if (!Array.isArray(kinds) || kinds.length === 0 || kinds.some((k) => !SEARCH_KIND_SET.has(k as string))) return undefined;
      entry.kinds = [...new Set(kinds as RenderViewSearchKind[])];
    }
    out.push(entry);
  }
  if (!Number.isInteger(index) || (index as number) < 0 || (index as number) > out.length) return undefined;
  return { history: out, index: index as number };
}

/** The valid fields of an untrusted viewState (invalid ones dropped). `undefined` when none remain. */
export function normalizeViewState(raw: unknown): RenderViewState | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const r = raw as Record<string, unknown>;
  const out: RenderViewState = {};
  const range = parseRange(r.range);
  if (range) out.range = range;
  const frameMs = parseFrameMs(r.frameMs);
  if (frameMs !== undefined) out.frameMs = frameMs;
  const crop = parseCrop(r.crop);
  if (crop) out.crop = crop;
  const search = parseSearch(r.search);
  if (search) out.search = search;
  return Object.keys(out).length ? out : undefined;
}

/**
 * Merge a PATCH into the stored viewState. A field set to `null` is cleared; a present field must be
 * valid (an invalid or unknown field rejects the whole patch, so a client bug never half-applies).
 */
export function mergeViewState(
  existing: unknown,
  patch: unknown,
): { ok: true; viewState: RenderViewState } | { ok: false; error: string } {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
    return { ok: false, error: 'viewState must be an object' };
  }
  const next: RenderViewState = { ...(normalizeViewState(existing) ?? {}) };
  for (const [field, value] of Object.entries(patch as Record<string, unknown>)) {
    if (!FIELDS.has(field)) return { ok: false, error: `unknown viewState field: ${field}` };
    if (value === null) {
      delete next[field as keyof RenderViewState];
      continue;
    }
    if (field === 'range') {
      const range = parseRange(value);
      if (!range) return { ok: false, error: 'range must be {start, end} seconds with 0 <= start < end' };
      next.range = range;
    } else if (field === 'frameMs') {
      const frameMs = parseFrameMs(value);
      if (frameMs === undefined) return { ok: false, error: 'frameMs must be a non-negative number' };
      next.frameMs = frameMs;
    } else if (field === 'search') {
      const search = parseSearch(value);
      if (!search) return { ok: false, error: `search must be {history: [{q | images, kinds?}] (at most ${VIEW_SEARCH_HISTORY_MAX}), index}` };
      next.search = search;
    } else {
      const crop = parseCrop(value);
      if (!crop) return { ok: false, error: 'crop must be {x, y, w, h} fractions inside the frame' };
      next.crop = crop;
    }
  }
  return { ok: true, viewState: next };
}

/** Value equality of two viewStates (absent ≡ empty). */
export function sameViewState(a: RenderViewState | undefined, b: RenderViewState | undefined): boolean {
  const ra = a?.range, rb = b?.range;
  const ca = a?.crop, cb = b?.crop;
  return (
    (ra?.start ?? null) === (rb?.start ?? null) && (ra?.end ?? null) === (rb?.end ?? null) &&
    (a?.frameMs ?? null) === (b?.frameMs ?? null) &&
    (ca?.x ?? null) === (cb?.x ?? null) && (ca?.y ?? null) === (cb?.y ?? null) &&
    (ca?.w ?? null) === (cb?.w ?? null) && (ca?.h ?? null) === (cb?.h ?? null) &&
    JSON.stringify(a?.search ?? null) === JSON.stringify(b?.search ?? null)
  );
}

/** The PATCH body that stores exactly `vs`: every field present, absent ones as `null` (cleared). */
export function viewStatePatch(vs: RenderViewState): Record<string, unknown> {
  return { range: vs.range ?? null, frameMs: vs.frameMs ?? null, crop: vs.crop ?? null, search: vs.search ?? null };
}
