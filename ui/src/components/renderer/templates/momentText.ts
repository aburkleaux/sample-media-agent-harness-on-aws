// Moment card text helpers (Component 7, media-harness-search-qa-rendering).
//
// Pure, dependency-free (no React) module backing the Moments_Gallery cards:
//   - segmentHighlights  — split matched text into highlighted / plain runs (Req 3.10)
//   - formatRankScore    — the `rank score 0.510` number (Req 3.9)
//   - momentTimecode     — the `<start>–<end>` card timecode (Req 3.9, reusing citations.ts)
//   - momentsBatchEntries — Moments bulk-action membership (Property 19; Req 3.12, 3.14, 3.15, 3.21, 3.28)
//
// The matched text is rendered as React text nodes, never through markdown or
// dangerouslySetInnerHTML, so any markup in a Hit's text stays literal (Req 3.10).

import { formatCitationTimecode } from '../../chat/citations';
import type { CropBox } from '../types';

/** One run of matched text: `highlighted` runs render as `<mark>`, the rest as plain text. */
export type TextSegment = { text: string; highlighted: boolean };

/**
 * Splits `text` at every case-insensitive occurrence of any term (longest term first at each
 * position, left to right, non-overlapping). Terms are matched literally (no regex meaning) and
 * compared with `toLocaleLowerCase('und')`. Empty terms (and `[]`) contribute no match, so `[]`
 * terms yield a single unhighlighted segment. The concatenation of the segment texts always
 * equals `text`: every boundary is an offset into the original `text`, so lengths cannot drift.
 */
export function segmentHighlights(text: string, terms: string[]): TextSegment[] {
  // Prepare non-empty terms, longest original length first (ties keep input order).
  const prepared = terms
    .map((term, idx) => ({ len: term.length, lower: term.toLocaleLowerCase('und'), idx }))
    .filter((t) => t.len > 0)
    .sort((a, b) => b.len - a.len || a.idx - b.idx);

  if (prepared.length === 0) {
    return [{ text, highlighted: false }];
  }

  const segments: TextSegment[] = [];
  let plainStart = 0;
  let i = 0;
  while (i < text.length) {
    let matchLen = 0;
    for (const t of prepared) {
      if (i + t.len > text.length) {
        continue;
      }
      // Lowercase a window of the ORIGINAL text of the term's length; a match therefore
      // consumes exactly `t.len` original code units, keeping all offsets in text-space.
      if (text.slice(i, i + t.len).toLocaleLowerCase('und') === t.lower) {
        matchLen = t.len;
        break;
      }
    }
    if (matchLen > 0) {
      if (plainStart < i) {
        segments.push({ text: text.slice(plainStart, i), highlighted: false });
      }
      segments.push({ text: text.slice(i, i + matchLen), highlighted: true });
      i += matchLen;
      plainStart = i;
    } else {
      i += 1;
    }
  }
  if (plainStart < text.length) {
    segments.push({ text: text.slice(plainStart), highlighted: false });
  }
  return segments;
}

/**
 * `Number(score.toPrecision(3))` printed with `toPrecision(3)`:
 * `0.510417 → "0.510"`, `1 → "1.00"`, `12345 → "1.23e+4"`. The round-trip through `Number`
 * drops a trailing-zero-only difference so the printed form is canonical. Exponent notation
 * appears only for scores of 1,000 or more, which normalized `final_score` values never reach.
 */
export function formatRankScore(score: number): string {
  return Number(score.toPrecision(3)).toPrecision(3);
}

/**
 * The `<start>–<end>` part of a Req 2.1 citation: `formatCitationTimecode` of `floor(start/1000)`
 * and `ceil(end/1000)`, joined by an en-dash (U+2013), exactly as `formatCitation` writes it.
 */
export function momentTimecode(startMs: number, endMs: number): string {
  const start = formatCitationTimecode(Math.floor(startMs / 1000));
  const end = formatCitationTimecode(Math.ceil(endMs / 1000));
  return `${start}\u2013${end}`;
}

/** A Moments bulk action. */
export type MomentAction = 'clip' | 'frame' | 'crop' | 'play';

/** URL phase of a Moment card, from `CardUrl` in useMomentUrls.ts (Req 3.28, 3.15). */
export type CardUrlPhase = 'minting' | 'ready' | 'no_access' | 'load_failed';

/** The state `momentsBatchEntries` reads for one Moment card. */
export interface MomentBatchCard {
  id: string;
  label: string;
  kind: 'video' | 'image' | 'audio';
  /** Names the asset without a URL; the gallery mints the `src` at action time. */
  ref: { project_id: string; asset_id: string; frame_s3_key: string | null };
  /** The current mark range in seconds, or `null` when cleared / not yet valid. */
  mark: { in: number; out: number } | null;
  /** A normalized crop box, or `null` when none is drawn. */
  box: CropBox | null;
  /** The card's media end in seconds; used by Play when marks are cleared. */
  end: number;
  /** URL phase; `no_access` and `load_failed` cards are excluded from every action. */
  phase: CardUrlPhase;
}

/** One membership entry, a URL-less subset of `BatchClipActionData.clips[]` (the gallery
 *  attaches `src` from a freshly minted URL before dispatching). */
export interface MomentBatchEntry {
  clipId: string;
  label: string;
  kind: 'video' | 'audio';
  ref: { project_id: string; asset_id: string; frame_s3_key: string | null };
  in?: number;
  out?: number;
  at?: number;
  box?: CropBox;
}

/**
 * Membership of a Moments bulk action, in card order (Property 19; Req 3.12, 3.14, 3.15, 3.21,
 * 3.28). Every action excludes `no_access` / `load_failed` cards and all image cards. Then:
 *   - `clip`:  video and audio cards with a valid mark → `in`/`out` = the current marks;
 *   - `frame`: video cards with a valid mark → `at` = the mark-in;
 *   - `crop`:  video cards with a crop box → `box`;
 *   - `play`:  video and audio cards → the mark range, or `0` to the card's end when cleared.
 */
export function momentsBatchEntries(
  cards: MomentBatchCard[],
  action: MomentAction,
): MomentBatchEntry[] {
  const entries: MomentBatchEntry[] = [];
  for (const card of cards) {
    if (card.phase === 'no_access' || card.phase === 'load_failed') {
      continue;
    }
    if (card.kind === 'image') {
      continue;
    }
    // card.kind is now narrowed to 'video' | 'audio'.
    const base = { clipId: card.id, label: card.label, kind: card.kind, ref: card.ref };
    switch (action) {
      case 'clip':
        if (card.mark) {
          entries.push({ ...base, in: card.mark.in, out: card.mark.out });
        }
        break;
      case 'frame':
        if (card.kind === 'video' && card.mark) {
          entries.push({ ...base, at: card.mark.in });
        }
        break;
      case 'crop':
        if (card.kind === 'video' && card.box) {
          entries.push({ ...base, box: card.box });
        }
        break;
      case 'play':
        if (card.mark) {
          entries.push({ ...base, in: card.mark.in, out: card.mark.out });
        } else {
          entries.push({ ...base, in: 0, out: card.end });
        }
        break;
    }
  }
  return entries;
}
