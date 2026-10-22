/**
 * The Moments tab's own search (2026-10-11; image queries 2026-10-18): edit the query, search again, step back
 * and forward.
 *
 * - A query is TEXT (`{q}`) or IMAGES (`{images}`, 1..3, "match any": one M2C search per image, merged; each card
 *   says which image(s) found it). The search bar shows images as chips; typing over them asks first, because
 *   M2C cannot combine words and an image in one search.
 * - Entry 0 is the agent's original results (the manifest's cards, with the turn's query — `queryRef` — in the
 *   bar); entry `i` is the user's `i`-th search. A new search from the middle of the history drops what was ahead
 *   of it (browser semantics) and keeps at most {@link VIEW_SEARCH_HISTORY_MAX} searches and
 *   {@link VIEW_SEARCH_JSON_BUDGET} serialized bytes.
 * - The history and the position persist in the manifest's `viewState.search`; RESULTS do not — they are
 *   re-fetched when a tab reopens on a search (~1–2 s, no LLM).
 * - Each fetched result set gets its own card-id prefix, so per-card URL/mark state can never carry over.
 * - An image whose search rendition is still being made (a just-dropped picture) is retried until it is ready.
 * - The agent is not told about these searches (v1).
 */
import { SEARCH_KINDS as SHARED_SEARCH_KINDS, isSearchKind } from '../../../shared/searchKinds';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { MomentItem, MomentsBlock, MomentsQueryImage } from '../types';
import { searchMoments, searchMomentsByImages, type MomentsSearchImage } from '../../../projects/api';
import { getActiveSessionId } from '../../../services/activeSession';
import {
  VIEW_SEARCH_HISTORY_MAX, VIEW_SEARCH_IMAGES_MAX, VIEW_SEARCH_IMAGE_LABEL_MAX, VIEW_SEARCH_JSON_BUDGET, VIEW_SEARCH_QUERY_MAX,
  type RenderViewQueryImage, type RenderViewSearchEntry, type RenderViewSearchKind, type RenderViewState,
} from '../../../shared/renderViewState';

export const SEARCH_KINDS: RenderViewSearchKind[] = [...SHARED_SEARCH_KINDS];
// ONE image per search in the UI: M2C searches one image per call, and "match any" across several read as a
// different feature than the one-picture search people expect. (Stored searches may still hold up to
// VIEW_SEARCH_IMAGES_MAX, and still re-run.)
export const QUERY_IMAGES_MAX = 1;
/** How long a not-yet-searchable image (409 image_not_ready) is retried, and how often. */
export const NOT_READY_RETRY_MS = 5_000;
export const NOT_READY_RETRIES = 24;

export type TabResult =
  | { status: 'loading' }
  | { status: 'preparing' }
  | { status: 'ok'; items: MomentItem[] }
  | { status: 'error'; message: string };

export interface MomentsTabSearch {
  /** The search box text. */
  query: string;
  setQuery: (q: string) => void;
  /** The query images in the bar (chips), in order. */
  images: MomentsQueryImage[];
  /** Selected kind chips; empty = every kind. */
  kinds: RenderViewSearchKind[];
  toggleKind: (k: RenderViewSearchKind) => void;
  clearKinds: () => void;
  /** Search the box text (words mode: the bar holds words OR an image, never both). */
  submit: () => void;
  /** Leave image mode for words: drop the image, empty box (the search on screen stays until the next one). */
  clearImages: () => void;
  /** How many cards a history position found, when known (0 = the agent's own cards). */
  countAt: (index: number) => number | null;
  /** Search by exactly these images (a new search). */
  searchImages: (images: MomentsQueryImage[]) => void;
  /** Add an image to the current image search (match any); at most {@link QUERY_IMAGES_MAX}. */
  addImage: (image: MomentsQueryImage) => void;
  replaceImage: (index: number, image: MomentsQueryImage) => void;
  removeImage: (index: number) => void;
  /** Find similar on a card: a new search by that card's image — here, not in a chat turn. Null if it has none. */
  imageOfMoment: (m: MomentItem) => MomentsQueryImage | null;
  back: () => void;
  forward: () => void;
  canBack: boolean;
  canForward: boolean;
  /** 0 = the agent's results. */
  index: number;
  /** The entry on screen (null at index 0). */
  entry: RenderViewSearchEntry | null;
  history: RenderViewSearchEntry[];
  goTo: (index: number) => void;
  /** The result for the entry on screen; the agent's own cards at index 0. */
  result: TabResult;
  retry: () => void;
}

const identity = (i: MomentsQueryImage) => i.assetId ?? `${i.sourceAssetId}|${i.frameKey}`;

// Results are cached per QUERY: a search always fetches every kind, and the kind chips filter the cards on
// screen (see `result`), so toggling a chip is instant and never re-runs the search.
const keyOf = (e: RenderViewSearchEntry) =>
  e.images ? `i:${e.images.map(identity).join(',')}` : `q:${e.q}`;

const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

const cut = (label: string) => [...label].slice(0, VIEW_SEARCH_IMAGE_LABEL_MAX).join('');

/** A card's image as a query: an image moment by its asset, a video moment by its frame. Audio/documents: none. */
export function imageOfMoment(m: MomentItem): MomentsQueryImage | null {
  if (m.kind === 'image') return { assetId: m.ref.asset_id, label: cut(m.display_name) };
  if (m.kind === 'video' && m.ref.frame_s3_key) {
    return { frameKey: m.ref.frame_s3_key, sourceAssetId: m.ref.asset_id, atMs: m.start_ms,
      label: cut(`Frame ${mmss(m.start_ms)} · ${m.display_name}`) };
  }
  return null;
}

const toWire = (i: MomentsQueryImage): MomentsSearchImage | null =>
  i.assetId ? { assetId: i.assetId } : i.frameKey && i.sourceAssetId ? { frameKey: i.frameKey, sourceAssetId: i.sourceAssetId } : null;

const toEntryImage = (i: MomentsQueryImage): RenderViewQueryImage => {
  const out: RenderViewQueryImage = { label: cut(i.label || 'Image') };
  if (i.assetId) out.assetId = i.assetId;
  else { out.frameKey = i.frameKey; out.sourceAssetId = i.sourceAssetId; }
  if (typeof i.atMs === 'number') out.atMs = i.atMs;
  return out;
};

/** The cards of one search response, validated like a manifest's and given a per-search id prefix. */
export function cardsFromResponse(projectId: string, raw: unknown[], prefix: string): MomentItem[] {
  const out: MomentItem[] = [];
  for (const r of raw) {
    const m = r as Partial<MomentItem> | null;
    if (!m || typeof m !== 'object' || typeof m.id !== 'string') continue;
    const ref = m.ref;
    if (!ref || ref.project_id !== projectId || typeof ref.asset_id !== 'string' || !ref.asset_id) continue;
    if (!isSearchKind(m.kind)) continue;
    if (!Number.isInteger(m.start_ms) || !Number.isInteger(m.end_ms) || !((m.start_ms as number) >= 0 && (m.start_ms as number) < (m.end_ms as number))) continue;
    if (typeof m.display_name !== 'string' || !m.display_name) continue;
    out.push({
      id: `${prefix}-${m.id}`,
      ref: { project_id: ref.project_id, asset_id: ref.asset_id, frame_s3_key: typeof ref.frame_s3_key === 'string' ? ref.frame_s3_key : null },
      kind: m.kind,
      display_name: m.display_name,
      start_ms: m.start_ms as number,
      end_ms: m.end_ms as number,
      source_index: typeof m.source_index === 'string' ? m.source_index : null,
      final_score: typeof m.final_score === 'number' && Number.isFinite(m.final_score) ? m.final_score : 0,
      text: typeof m.text === 'string' ? m.text : '',
      text_truncated: m.text_truncated === true,
      highlight_terms: Array.isArray(m.highlight_terms) ? m.highlight_terms.filter((x): x is string => typeof x === 'string') : [],
      citation: null,
      ...(Array.isArray(m.matched_by) ? { matched_by: m.matched_by.filter((x): x is number => Number.isInteger(x)) } : {}),
    });
  }
  return out;
}

/** `history` cut from the oldest end until its serialized `search` fits the PATCH budget; `index` shifted with it. */
export function fitHistory(history: RenderViewSearchEntry[], index: number): { history: RenderViewSearchEntry[]; index: number } {
  let h = history.length > VIEW_SEARCH_HISTORY_MAX ? history.slice(history.length - VIEW_SEARCH_HISTORY_MAX) : history;
  let i = index - (history.length - h.length);
  while (h.length > 1 && JSON.stringify({ history: h, index: i }).length > VIEW_SEARCH_JSON_BUDGET) {
    h = h.slice(1);
    i -= 1;
  }
  return { history: h, index: Math.max(0, i) };
}

/** The turn's starting query from the manifest: its typed `queryRef`, else the old text `query`. */
function originalOf(moments: MomentsBlock): { q: string; images: MomentsQueryImage[] } {
  const ref = moments.queryRef;
  if (ref && ref.type === 'image' && Array.isArray(ref.images)) {
    const images = ref.images.filter((i) => i && typeof i.label === 'string' && (i.assetId || (i.frameKey && i.sourceAssetId)))
      .slice(0, QUERY_IMAGES_MAX);
    if (images.length) return { q: '', images };
  }
  if (ref && ref.type === 'text' && typeof ref.q === 'string') return { q: ref.q, images: [] };
  return { q: typeof moments.query === 'string' ? moments.query : '', images: [] };
}

export function useMomentsTabSearch(
  moments: MomentsBlock,
  viewState: RenderViewState | undefined,
  onViewStateChange: ((vs: RenderViewState) => void) | undefined,
): MomentsTabSearch {
  const original = useMemo(() => originalOf(moments), [moments]);
  const restored = viewState?.search;
  const [history, setHistory] = useState<RenderViewSearchEntry[]>(() => restored?.history ?? []);
  const [index, setIndex] = useState<number>(() => restored?.index ?? 0);
  const entryAt = (i: number, h: RenderViewSearchEntry[] = history) => (i > 0 ? h[i - 1] ?? null : null);
  const startEntry = entryAt(restored?.index ?? 0, restored?.history ?? []);
  const [query, setQueryState] = useState<string>(() => (startEntry ? startEntry.q ?? '' : original.q));
  const [images, setImages] = useState<MomentsQueryImage[]>(() => (startEntry ? startEntry.images ?? [] : original.images));
  const [kinds, setKinds] = useState<RenderViewSearchKind[]>(() => startEntry?.kinds ?? []);
  const [results, setResults] = useState<Map<string, TabResult>>(() => new Map());
  const seq = useRef(0);
  const inflight = useRef(new Set<string>());
  const retryTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => () => { for (const t of retryTimers.current.values()) clearTimeout(t); }, []);

  const entry = entryAt(index);

  const fetchEntry = useCallback((e: RenderViewSearchEntry, force = false, attempt = 0) => {
    const k = keyOf(e);
    if (inflight.current.has(k)) return;
    setResults((prev) => {
      if (!force && prev.get(k)?.status === 'ok') return prev;
      const next = new Map(prev); next.set(k, attempt > 0 ? { status: 'preparing' } : { status: 'loading' }); return next;
    });
    inflight.current.add(k);
    const prefix = `t${++seq.current}`;
    const wire = e.images ? e.images.map(toWire).filter((x): x is MomentsSearchImage => !!x) : null;
    const request = wire ? searchMomentsByImages(moments.project_id, wire, getActiveSessionId() || null) : searchMoments(moments.project_id, e.q ?? '');
    void request.then((r) => {
      inflight.current.delete(k);
      if (!r.ok && r.kind === 'error' && r.code === 'image_not_ready' && attempt < NOT_READY_RETRIES) {
        // A just-dropped picture: its search rendition is still being made. Say so, and try again shortly.
        setResults((prev) => { const next = new Map(prev); next.set(k, { status: 'preparing' }); return next; });
        retryTimers.current.set(k, setTimeout(() => fetchEntry(e, true, attempt + 1), NOT_READY_RETRY_MS));
        return;
      }
      const value: TabResult = r.ok
        ? { status: 'ok', items: cardsFromResponse(moments.project_id, r.value.items, prefix) }
        : { status: 'error', message: r.kind === 'error' ? r.message : 'This project is not available.' };
      setResults((prev) => { const next = new Map(prev); next.set(k, value); return next; });
    });
  }, [moments.project_id]);

  // A reopened tab (or a step back/forward to an uncached search) re-runs that query.
  useEffect(() => {
    if (entry && !results.has(keyOf(entry))) fetchEntry(entry);
  }, [entry, results, fetchEntry]);

  // Persist history + position (not on the restore itself).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    if (!onViewStateChange) return;
    const base: RenderViewState = { ...(viewState ?? {}) };
    if (history.length === 0) delete base.search;
    else base.search = { history, index };
    onViewStateChange(base);
    // viewState is the restore input, not a dependency: only the user's moves persist.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history, index, onViewStateChange]);

  const goTo = useCallback((i: number) => {
    setIndex(i);
    const e = entryAt(i);
    setQueryState(e ? e.q ?? '' : original.q);
    setImages(e ? e.images ?? [] : original.images);
    setKinds(e?.kinds ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history, original]);

  const push = useCallback((e: RenderViewSearchEntry) => {
    const fitted = fitHistory([...history.slice(0, index), e], index + 1);
    setHistory(fitted.history);
    setIndex(fitted.index);
    fetchEntry(e, true);
  }, [history, index, fetchEntry]);

  const withKinds = (e: RenderViewSearchEntry): RenderViewSearchEntry =>
    (kinds.length && kinds.length < SEARCH_KINDS.length ? { ...e, kinds: [...kinds] } : e);

  const searchText = useCallback((text: string) => {
    const q = [...text.trim()].slice(0, VIEW_SEARCH_QUERY_MAX).join('');
    if (!q) return;
    setImages([]);
    push(withKinds({ q }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [push, kinds]);

  const searchImages = useCallback((next: MomentsQueryImage[]) => {
    const list = next.slice(0, QUERY_IMAGES_MAX);
    setImages(list);
    setQueryState('');
    if (list.length) push(withKinds({ images: list.map(toEntryImage) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [push, kinds]);

  const submit = useCallback(() => {
    const q = query.trim();
    if (!q) return;
    searchText(q);
  }, [query, searchText]);

  // The kind chips FILTER the cards on screen -- the agent's results (index 0) as much as a tab search.
  const result: TabResult = useMemo(() => {
    const base: TabResult = !entry ? { status: 'ok', items: moments.items } : results.get(keyOf(entry)) ?? { status: 'loading' };
    if (base.status !== 'ok' || kinds.length === 0 || kinds.length >= SEARCH_KINDS.length) return base;
    return { status: 'ok', items: base.items.filter((m) => (kinds as string[]).includes(m.kind)) };
  }, [entry, results, moments.items, kinds]);

  return {
    query,
    setQuery: (q: string) => setQueryState(q),
    images,
    kinds,
    toggleKind: (k) => setKinds((prev) => (prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k])),
    clearKinds: () => setKinds([]),
    submit,
    clearImages: () => { setImages([]); setQueryState(''); },
    countAt: (i: number) => {
      if (i === 0) return moments.items.length;
      const e = entryAt(i);
      const r = e ? results.get(keyOf(e)) : undefined;
      return r && r.status === 'ok' ? r.items.length : null;
    },
    searchImages,
    addImage: (image) => {
      if (images.some((i) => identity(i) === identity(image))) return;
      if (images.length >= QUERY_IMAGES_MAX) return;
      searchImages([...images, image]);
    },
    replaceImage: (i, image) => searchImages(images.map((x, j) => (j === i ? image : x))),
    removeImage: (i) => {
      const rest = images.filter((_, j) => j !== i);
      if (rest.length) searchImages(rest);
      else setImages([]);
    },
    imageOfMoment,
    back: () => { if (index > 0) goTo(index - 1); },
    forward: () => { if (index < history.length) goTo(index + 1); },
    canBack: index > 0,
    canForward: index < history.length,
    index,
    entry,
    history,
    goTo,
    result,
    retry: () => { if (entry) fetchEntry(entry, true); },
  };
}
