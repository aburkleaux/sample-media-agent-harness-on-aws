// Feature: media-harness-search-qa-rendering, Property 19: Moments bulk-action membership
//
// For any list of card states (kind, mark valid or cleared, crop box or none, URL phase from
// `CardUrl`) and each action in {clip, frame, crop, play}, `momentsBatchEntries(cards, action)`
// keeps card order and equals:
//   - clip:  video and audio cards with a valid mark → in/out = current marks, kind kept;
//   - frame: video cards with a valid mark → at = mark-in;
//   - crop:  video cards with a crop box → box;
//   - play:  video and audio cards → the mark range, or 0..end when cleared.
// Every action also excludes cards whose phase is `no_access` or `load_failed`, and image cards
// appear in no action.
//
// **Validates: Requirements 3.12, 3.14, 3.15, 3.21, 3.28**

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';

import {
  momentsBatchEntries,
  type MomentAction,
  type MomentBatchCard,
  type MomentBatchEntry,
  type CardUrlPhase,
} from './momentText';
import type { CropBox } from '../types';

// --- Generators ---------------------------------------------------------------------------------

const kindArb = fc.constantFrom<'video' | 'image' | 'audio'>('video', 'image', 'audio');
const phaseArb = fc.constantFrom<CardUrlPhase>('minting', 'ready', 'no_access', 'load_failed');
const actionArb = fc.constantFrom<MomentAction>('clip', 'frame', 'crop', 'play');

const refArb = fc.record({
  project_id: fc.string({ maxLength: 8 }),
  asset_id: fc.string({ maxLength: 8 }),
  frame_s3_key: fc.option(fc.string({ maxLength: 8 }), { nil: null }),
});

const num = fc.double({ min: 0, max: 1_000_000, noNaN: true });

const markArb = fc.option(
  fc.record({ in: num, out: num }),
  { nil: null },
);

const boxArb = fc.option(
  fc.record({ x: num, y: num, w: num, h: num }) as fc.Arbitrary<CropBox>,
  { nil: null },
);

const cardArb: fc.Arbitrary<MomentBatchCard> = fc.record({
  id: fc.string({ maxLength: 10 }),
  label: fc.string({ maxLength: 10 }),
  kind: kindArb,
  ref: refArb,
  mark: markArb,
  box: boxArb,
  end: num,
  phase: phaseArb,
});

const cardsArb = fc.array(cardArb, { maxLength: 30 });

// --- Independent oracle -------------------------------------------------------------------------

function oracle(cards: MomentBatchCard[], action: MomentAction): MomentBatchEntry[] {
  const out: MomentBatchEntry[] = [];
  for (const card of cards) {
    // Every action excludes these phases and all image cards.
    if (card.phase === 'no_access' || card.phase === 'load_failed') continue;
    if (card.kind === 'image') continue;

    const kind = card.kind as 'video' | 'audio';
    const base = { clipId: card.id, label: card.label, kind, ref: card.ref };

    if (action === 'clip') {
      // video and audio cards with a valid mark.
      if (card.mark) out.push({ ...base, in: card.mark.in, out: card.mark.out });
    } else if (action === 'frame') {
      // video cards with a valid mark.
      if (kind === 'video' && card.mark) out.push({ ...base, at: card.mark.in });
    } else if (action === 'crop') {
      // video cards with a crop box.
      if (kind === 'video' && card.box) out.push({ ...base, box: card.box });
    } else {
      // play: video and audio cards; mark range, or 0..end when cleared.
      if (card.mark) out.push({ ...base, in: card.mark.in, out: card.mark.out });
      else out.push({ ...base, in: 0, out: card.end });
    }
  }
  return out;
}

// --- Property -----------------------------------------------------------------------------------

describe('momentsBatchEntries — Property 19: Moments bulk-action membership', () => {
  it('matches the oracle for every action, in card order', () => {
    fc.assert(
      fc.property(cardsArb, actionArb, (cards, action) => {
        expect(momentsBatchEntries(cards, action)).toEqual(oracle(cards, action));
      }),
      { numRuns: 2000 },
    );
  });

  it('never emits an image card and never emits a no_access/load_failed card', () => {
    fc.assert(
      fc.property(cardsArb, actionArb, (cards, action) => {
        const entries = momentsBatchEntries(cards, action);
        // Membership entries carry only video/audio kinds, never image.
        for (const e of entries) {
          expect(e.kind === 'video' || e.kind === 'audio').toBe(true);
        }
        // No entry's source card was excluded by phase. Compare in order: the emitted clipIds
        // must be a subsequence of the eligible (non-excluded, non-image) card ids.
        const eligibleIds = cards
          .filter((c) => c.phase !== 'no_access' && c.phase !== 'load_failed' && c.kind !== 'image')
          .map((c) => c.id);
        const emittedIds = entries.map((e) => e.clipId);
        let j = 0;
        for (const id of emittedIds) {
          while (j < eligibleIds.length && eligibleIds[j] !== id) j += 1;
          expect(j).toBeLessThan(eligibleIds.length);
          j += 1;
        }
      }),
      { numRuns: 1000 },
    );
  });
});
