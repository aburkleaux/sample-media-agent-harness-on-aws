// Property tests for ui/src/components/renderer/templates/momentText.ts.
//
// Feature: media-harness-search-qa-rendering.
//
// This file is appended to by a sibling task (Property 18: card timecode & rank score). Each
// property lives in its own self-contained block delimited by the marker comments below so the
// blocks can be added without touching one another.

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';

import { segmentHighlights, type TextSegment } from './momentText';
// Property 18 (card timecode & rank score) — kept as separate import lines so the Property 17
// import above stays untouched.
import { formatRankScore, momentTimecode } from './momentText';
import { formatCitation } from '../../chat/citations';

// ================================================================================================
// Property 17: Highlight segmentation
// Validates: Requirements 3.10
//
// For any text (including `<b>`, `&amp;`, regex metacharacters and non-ASCII) and list of
// non-empty terms (overlapping, nested, differing only by case, containing metacharacters),
// `segmentHighlights(text, terms)` satisfies all of:
//   - the concatenation of the segment texts equals `text`;
//   - every highlighted segment equals some term under `toLocaleLowerCase('und')`;
//   - no unhighlighted segment contains a whole occurrence of any term under that comparison;
//   - `terms = []` gives one unhighlighted segment.
//
// The MomentCard rendering invariant stated by Property 17 — a matched-text element whose
// `textContent` equals `text`, with `<mark className="mh-moment-hit">` as its only child
// elements — follows directly from the concatenation property: MomentCard maps the segments to
// plain text nodes and `<mark>` runs (MomentCard.tsx), so `textContent` is exactly the segment
// concatenation and every element child is one highlighted segment.
// ================================================================================================

// --- Generators ---------------------------------------------------------------------------------

// A pool mixing letters, case variants, markup-looking characters, regex metacharacters, and
// non-ASCII. These characters do not change length under toLocaleLowerCase('und') (we avoid
// length-changing folds such as U+0130), which keeps the oracle's window comparison aligned with
// the implementation's.
const CHARS = [
  'a', 'A', 'b', 'B', 'c', 'C',
  'é', 'É', 'ß', 'Ø', 'ø',
  '<', '>', '&', ';', '/',
  '.', '*', '+', '?', '[', ']', '(', ')', '\\', '^', '$', '|',
  '日', '本',
  ' ', '1',
];

const charArb = fc.constantFrom(...CHARS);
const textArb = fc.array(charArb, { maxLength: 40 }).map((a) => a.join(''));
// Every term is non-empty (minLength 1 over a non-empty alphabet).
const termArb = fc.array(charArb, { minLength: 1, maxLength: 5 }).map((a) => a.join(''));
const termsArb = fc.array(termArb, { maxLength: 6 });

// --- Helpers ------------------------------------------------------------------------------------

const lower = (s: string) => s.toLocaleLowerCase('und');

/** True when some window of `segment` of length `term.length` lower-matches `term`, mirroring the
 *  exact comparison `segmentHighlights` performs so "occurrence" means the same thing here. */
function containsOccurrence(segment: string, term: string): boolean {
  const needle = lower(term);
  for (let p = 0; p + term.length <= segment.length; p += 1) {
    if (lower(segment.slice(p, p + term.length)) === needle) {
      return true;
    }
  }
  return false;
}

// --- Property -----------------------------------------------------------------------------------

describe('segmentHighlights — Property 17: Highlight segmentation', () => {
  it('concatenation of segment texts equals the input text', () => {
    fc.assert(
      fc.property(textArb, termsArb, (text, terms) => {
        const segments = segmentHighlights(text, terms);
        expect(segments.map((s) => s.text).join('')).toBe(text);
      }),
      { numRuns: 2000 },
    );
  });

  it('every highlighted segment lower-equals some non-empty term', () => {
    fc.assert(
      fc.property(textArb, termsArb, (text, terms) => {
        const nonEmpty = terms.filter((t) => t.length > 0);
        const segments: TextSegment[] = segmentHighlights(text, terms);
        for (const seg of segments) {
          if (seg.highlighted) {
            expect(nonEmpty.some((t) => lower(t) === lower(seg.text))).toBe(true);
          }
        }
      }),
      { numRuns: 2000 },
    );
  });

  it('no unhighlighted segment contains a whole occurrence of any term', () => {
    fc.assert(
      fc.property(textArb, termsArb, (text, terms) => {
        const nonEmpty = terms.filter((t) => t.length > 0);
        const segments = segmentHighlights(text, terms);
        for (const seg of segments) {
          if (!seg.highlighted) {
            for (const t of nonEmpty) {
              expect(containsOccurrence(seg.text, t)).toBe(false);
            }
          }
        }
      }),
      { numRuns: 2000 },
    );
  });

  it('empty terms yield a single unhighlighted segment equal to the text', () => {
    fc.assert(
      fc.property(textArb, (text) => {
        expect(segmentHighlights(text, [])).toEqual([{ text, highlighted: false }]);
      }),
      { numRuns: 500 },
    );
    // Terms that are all empty strings contribute no match either (filtered as empty).
    fc.assert(
      fc.property(textArb, fc.array(fc.constant(''), { maxLength: 4 }), (text, empties) => {
        expect(segmentHighlights(text, empties)).toEqual([{ text, highlighted: false }]);
      }),
      { numRuns: 500 },
    );
  });

  it('handles overlapping, nested, case-differing and metacharacter terms (examples)', () => {
    // Longest term wins at each position, left to right, non-overlapping.
    expect(segmentHighlights('abcabc', ['abc', 'bc'])).toEqual([
      { text: 'abc', highlighted: true },
      { text: 'abc', highlighted: true },
    ]);
    // Case-insensitive match keeps the original (un-lowered) text in the segment.
    expect(segmentHighlights('AbCdef', ['abc'])).toEqual([
      { text: 'AbC', highlighted: true },
      { text: 'def', highlighted: false },
    ]);
    // Regex metacharacters are matched literally, and surrounding markup stays literal plain text.
    expect(segmentHighlights('x<b>a.*b</b>y', ['a.*b'])).toEqual([
      { text: 'x<b>', highlighted: false },
      { text: 'a.*b', highlighted: true },
      { text: '</b>y', highlighted: false },
    ]);
  });
});

// === END Property 17 (highlight segmentation) ===================================================
// Append new self-contained property blocks (e.g. Property 18) below this line.

// ================================================================================================
// Property 18: Moment card timecode and rank score
// Validates: Requirements 2.1, 3.9
//
// Two independent invariants over the two pure card helpers in momentText.ts:
//
//   (a) Timecode. For any 0 <= start_ms < end_ms below 100 hours, momentTimecode(start_ms, end_ms)
//       equals the `<start>–<end>` part of formatCitation(name, start_ms, end_ms) — i.e. the card
//       timecode is exactly what a Req 2.1 citation writes between ` @ ` and the closing `]`. This
//       pins the card's time label to the one citation grammar.
//
//   (b) Rank score. For any finite score in [0, 1000), formatRankScore(score) carries exactly
//       three significant digits, and Number(formatRankScore(score)) === Number(score.toPrecision(3))
//       (the printed string is a canonical 3-significant-figure rounding of the score).
// ================================================================================================

// --- Generators ---------------------------------------------------------------------------------

// 100 hours expressed in milliseconds; `end_ms` stays strictly below this.
const MAX_MS = 100 * 3600 * 1000; // 360_000_000

// A pair [start, end] with 0 <= start < end < MAX_MS (so formatCitation's 0 <= start <= end holds).
const spanArb = fc
  .tuple(fc.integer({ min: 0, max: MAX_MS - 1 }), fc.integer({ min: 0, max: MAX_MS - 1 }))
  .filter(([a, b]) => a !== b)
  .map(([a, b]) => (a < b ? [a, b] : [b, a]) as [number, number]);

// Any finite, non-negative score strictly below 1000 (normalized final_score never reaches 1000).
const scoreArb = fc.double({
  min: 0,
  max: 1000,
  maxExcluded: true,
  noNaN: true,
  noDefaultInfinity: true,
});

// A Display_Name with no `@` or `]`, so the citation's timecode slice is unambiguous.
const NAME = 'nba.mp4';

// --- Helpers ------------------------------------------------------------------------------------

/** The number of significant digits in a `toPrecision`-style decimal/exponent string: drop any
 *  exponent, remove sign and decimal point, then strip leading zeros. "0.510" → 3, "1.00" → 3,
 *  "100" → 3, "1.00e+3" → 3, "0.00" → 0 (the zero case, handled separately below). */
function significantDigits(formatted: string): number {
  const mantissa = formatted.split(/[eE]/)[0];
  const digits = mantissa.replace('-', '').replace('.', '');
  return digits.replace(/^0+/, '').length;
}

// --- Property -----------------------------------------------------------------------------------

describe('momentText — Property 18: card timecode and rank score', () => {
  it('momentTimecode equals the <start>–<end> part of the matching citation', () => {
    const prefix = `[${NAME} @ `;
    fc.assert(
      fc.property(spanArb, ([startMs, endMs]) => {
        const citation = formatCitation(NAME, startMs, endMs);
        // Everything between ` @ ` and the trailing `]` is the `<start>–<end>` timecode part.
        const timecodePart = citation.slice(prefix.length, -1);
        expect(momentTimecode(startMs, endMs)).toBe(timecodePart);
      }),
      { numRuns: 2000 },
    );
  });

  it('formatRankScore has three significant digits and round-trips through toPrecision(3)', () => {
    fc.assert(
      fc.property(scoreArb, (score) => {
        const out = formatRankScore(score);
        if (score === 0) {
          // The documented zero case: toPrecision(3) of 0 is "0.00".
          expect(out).toBe('0.00');
        } else {
          expect(significantDigits(out)).toBe(3);
        }
        expect(Number(out)).toBe(Number(score.toPrecision(3)));
      }),
      { numRuns: 2000 },
    );
  });

  it('matches the documented examples', () => {
    // Timecode: mm:ss below one hour, h:mm:ss from one hour, floor(start)/ceil(end) seconds.
    expect(momentTimecode(74_000, 96_000)).toBe('01:14\u201301:36');
    expect(momentTimecode(0, 1)).toBe('00:00\u201300:01'); // an image: @ 00:00–00:01
    expect(momentTimecode(3_600_000, 3_661_500)).toBe('1:00:00\u20131:01:02');

    // Rank score: three significant figures, en-dash-free plain decimals.
    expect(formatRankScore(0.510417)).toBe('0.510');
    expect(formatRankScore(1)).toBe('1.00');
    expect(formatRankScore(12.3456)).toBe('12.3');
    expect(formatRankScore(0)).toBe('0.00');
  });
});

// === END Property 18 (card timecode and rank score) =============================================
// Append new self-contained property blocks (e.g. Property 19) below this line.
