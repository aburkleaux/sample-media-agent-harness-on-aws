// Property tests for the TypeScript Citation_Formatter / Citation_Parser (task 3.6).
//
// Feature: media-harness-search-qa-rendering, Component 3.
//
// Property 6 (Citation format and parse round trip), TypeScript half. The Python
// twin lives in agent/tests/test_citations.py (task 3.5); this file mirrors its two
// statements (round trip and format shape) against the same grammar.
//
// Property 7 (malformed bracketed text stays plain) is task 3.8 and will be APPENDED
// to this file in its own clearly separated block below Property 6; keep this block
// self-contained so that addition does not conflict.
//
// **Validates: Requirements 2.1, 2.2, 2.4**

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';

import { formatCitation, parseCitations, NAME_MAX } from './citations';

// ---------------------------------------------------------------------------
// Property 6: Citation format and parse round trip
// ---------------------------------------------------------------------------

// One hundred hours in seconds / milliseconds. The grammar accepts an unpadded
// `h` of one or two digits, so a whole-second range strictly below 100 h round
// trips through the parser; a three-digit `h` would not.
const HUNDRED_HOURS_S = 100 * 3600;
const HUNDRED_HOURS_MS = HUNDRED_HOURS_S * 1000;

// The explicit trim set shared with the implementation (and the Python side):
// U+0020, U+0009, U+00A0, U+1680, U+2000-U+200A, U+202F, U+205F, U+3000, U+FEFF.
const TRIM_CHARS =
  '\u0020\u0009\u00a0\u1680' +
  '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a' +
  '\u202f\u205f\u3000\ufeff';
const TRIM_SET = new Set(TRIM_CHARS);

/** Strip the explicit TRIM_CHARS set from both ends (reference impl). */
function stripTrim(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && TRIM_SET.has(value[start])) {
    start += 1;
  }
  while (end > start && TRIM_SET.has(value[end - 1])) {
    end -= 1;
  }
  return value.slice(start, end);
}

function pad2(value: number): string {
  return value.toString().padStart(2, '0');
}

/** Independent reference formatter for the Req 2.1 timecode shape. */
function refTimecode(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const remainder = totalSeconds % 3600;
  const minutes = Math.floor(remainder / 60);
  const seconds = remainder % 60;
  if (totalSeconds < 3600) {
    return `${pad2(minutes)}:${pad2(seconds)}`;
  }
  return `${hours}:${pad2(minutes)}:${pad2(seconds)}`;
}

// --- Generators -------------------------------------------------------------

// A single code point as a string, excluding surrogates. `extra` lists further
// characters to forbid.
function codePointChar(extra: string): fc.Arbitrary<string> {
  return fc
    .integer({ min: 1, max: 0x10ffff })
    .filter((cp) => cp < 0xd800 || cp > 0xdfff)
    .map((cp) => String.fromCodePoint(cp))
    .filter((ch) => !extra.includes(ch));
}

// Display_Name: no leading/trailing TRIM_CHARS, none of `[` `]` `@` `#` CR LF,
// and 1..NAME_MAX UTF-16 code units after trimming. Capped at 127 code points so
// that even all-astral names stay within NAME_MAX (255 code units) once trimmed.
const displayNames: fc.Arbitrary<string> = fc
  .array(codePointChar('[]@#\r\n'), { minLength: 1, maxLength: 127 })
  .map((chars) => stripTrim(chars.join('')))
  .filter((name) => name.length >= 1 && name.length <= NAME_MAX);

// prefix / suffix carry no `[` or `]` so they introduce no second bracket; the
// suffix additionally must not start with `(` (the grammar rejects a `]`
// immediately followed by `(` — the markdown-link guard).
const prefixes: fc.Arbitrary<string> = fc
  .array(codePointChar('[]'), { maxLength: 12 })
  .map((chars) => chars.join(''));
const suffixes: fc.Arbitrary<string> = fc
  .array(codePointChar('[]'), { maxLength: 12 })
  .map((chars) => chars.join(''))
  .filter((s) => !s.startsWith('('));

// (start_ms, end_ms) on whole seconds, start <= end, strictly below 100 h.
const wholeSecondRanges: fc.Arbitrary<[number, number]> = fc
  .integer({ min: 0, max: HUNDRED_HOURS_S - 1 })
  .chain((startS) =>
    fc
      .integer({ min: startS, max: HUNDRED_HOURS_S - 1 })
      .map((endS) => [startS * 1000, endS * 1000] as [number, number]),
  );

// (start_ms, end_ms) at millisecond resolution, 0 <= start <= end <= 100 h.
const msRanges: fc.Arbitrary<[number, number]> = fc
  .integer({ min: 0, max: HUNDRED_HOURS_MS })
  .chain((startMs) =>
    fc
      .integer({ min: startMs, max: HUNDRED_HOURS_MS })
      .map((endMs) => [startMs, endMs] as [number, number]),
  );

// --- Statements -------------------------------------------------------------

describe('Property 6: Citation format and parse round trip', () => {
  // Validates: Requirements 2.1, 2.2, 2.4
  //
  // For a well-formed name and whole-second range below 100 h, the formatted
  // citation embedded between bracket-free affixes parses back to exactly one
  // Citation with that name/start/end whose `text` is the formatted string.
  it('round trips a formatted citation between bracket-free affixes', () => {
    fc.assert(
      fc.property(
        displayNames,
        wholeSecondRanges,
        prefixes,
        suffixes,
        (name, [startMs, endMs], prefix, suffix) => {
          const formatted = formatCitation(name, startMs, endMs);

          const citations = parseCitations(prefix + formatted + suffix);

          expect(citations).toHaveLength(1);
          const [citation] = citations;
          expect(citation.displayName).toBe(name);
          expect(citation.startMs).toBe(startMs);
          expect(citation.endMs).toBe(endMs);
          expect(citation.text).toBe(formatted);
        },
      ),
      { numRuns: 400 },
    );
  });

  // Validates: Requirements 2.1, 2.2, 2.4
  //
  // For any 0 <= start_ms <= end_ms, formatCitation writes floor(start_ms / 1000)
  // and ceil(end_ms / 1000) seconds in the Req 2.1 shape (one space each side of
  // `@`, en dash with no spaces, mm:ss below one hour, unpadded h:mm:ss from one
  // hour).
  it('writes floor(start) and ceil(end) seconds in the Req 2.1 shape', () => {
    fc.assert(
      fc.property(displayNames, msRanges, (name, [startMs, endMs]) => {
        const startSeconds = Math.floor(startMs / 1000);
        const endSeconds = Math.ceil(endMs / 1000);

        const expected = `[${name} @ ${refTimecode(startSeconds)}\u2013${refTimecode(endSeconds)}]`;

        expect(formatCitation(name, startMs, endMs)).toBe(expected);
      }),
      { numRuns: 400 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 7 (malformed bracketed text stays plain) is appended here by task 3.8.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Property 7: Malformed bracketed text stays plain (task 3.8), TypeScript half.
//
// The Python twin is test_malformed_bracketed_text_stays_plain in
// agent/tests/test_citations.py (task 3.7). For any text built from valid
// Citations interleaved with bracketed candidates that each break exactly one
// grammar rule (a minute or second field of 60 or more, a one-digit ss, a
// three-digit h, a missing field, start later than end, a name empty after
// trimming, a name longer than NAME_MAX code units after trimming, or a closing
// `]` followed by `(`), parseCitations returns no Citation for any malformed
// candidate, and the Citations it returns are exactly those returned for the
// same text with the malformed candidates removed (compared by text,
// displayName, startMs and endMs).
//
// This block reuses the Property 6 helpers above (pad2, refTimecode,
// codePointChar, displayNames, wholeSecondRanges, TRIM_CHARS, HUNDRED_HOURS_S)
// but is otherwise self-contained.
//
// **Validates: Requirements 2.3**
// ---------------------------------------------------------------------------

// Separators placed around every segment carry no bracket, so they introduce no
// new citation boundary, and no `(` so they can never suppress a following valid
// citation's `]` via the markdown-link guard — making them inert, as on the
// Python side.
const separators: fc.Arbitrary<string> = fc
  .array(codePointChar('[]()'), { maxLength: 8 })
  .map((chars) => chars.join(''));

// Valid minute/second field (00-59) shared by the broken timecodes and their
// valid companion. A constant valid companion keeps each candidate breaking
// exactly one rule.
const FIELD = fc.integer({ min: 0, max: 59 });
const GOOD_TC = '00:30';

// One bracketed candidate that breaks exactly one Req 2.3 rule and therefore
// yields no Citation from parseCitations.
const badCandidate: fc.Arbitrary<string> = fc
  .integer({ min: 0, max: 7 })
  .chain((rule) => {
    switch (rule) {
      case 0: // a minute or second field of 60 or more
        return fc
          .tuple(displayNames, fc.boolean(), fc.integer({ min: 60, max: 99 }), FIELD)
          .map(([name, minuteBroken, big, ok]) => {
            const badTc = minuteBroken ? `${big}:${pad2(ok)}` : `${pad2(ok)}:${big}`;
            return `[${name} @ ${badTc}\u2013${GOOD_TC}]`;
          });
      case 1: // a one-digit ss (a one-digit mm, `m:ss`, is VALID since 2026-10-10 — Req 2.2 amended)
        return fc
          .tuple(displayNames, FIELD, fc.integer({ min: 0, max: 9 }))
          .map(([name, mm, s]) => `[${name} @ ${pad2(mm)}:${s}\u2013${GOOD_TC}]`);
      case 2: // a three-digit h
        return fc
          .tuple(displayNames, fc.integer({ min: 100, max: 999 }), FIELD, FIELD)
          .map(
            ([name, hours, mm, ss]) =>
              `[${name} @ ${hours}:${pad2(mm)}:${pad2(ss)}\u2013${GOOD_TC}]`,
          );
      case 3: // a missing field (a bare number, no colon)
        return fc
          .tuple(displayNames, FIELD)
          .map(([name, f]) => `[${name} @ ${pad2(f)}\u2013${GOOD_TC}]`);
      case 4: // start later than end (both valid whole-second timecodes)
        return fc
          .tuple(displayNames, fc.integer({ min: 1, max: HUNDRED_HOURS_S - 1 }))
          .chain(([name, startS]) =>
            fc
              .integer({ min: 0, max: startS - 1 })
              .map(
                (endS) =>
                  `[${name} @ ${refTimecode(startS)}\u2013${refTimecode(endS)}]`,
              ),
          );
      case 5: // a name empty after trimming
        return fc
          .array(fc.constantFrom(...TRIM_CHARS.split('')), { minLength: 1, maxLength: 4 })
          .map((chars) => formatCitation(chars.join(''), 0, 30_000));
      case 6: // a name longer than NAME_MAX code units after trimming
        return fc
          .integer({ min: NAME_MAX + 1, max: NAME_MAX + 45 })
          .map((len) => formatCitation('a'.repeat(len), 0, 30_000));
      default: // rule 7: a well-formed citation whose closing `]` is followed by `(`
        return fc
          .tuple(displayNames, wholeSecondRanges)
          .map(([name, [startMs, endMs]]) => formatCitation(name, startMs, endMs) + '(');
    }
  });

// A valid-citation segment, carrying the reduced tuple it must parse back to.
interface GoodSegment {
  kind: 'good';
  text: string;
  name: string;
  startMs: number;
  endMs: number;
}
interface BadSegment {
  kind: 'bad';
  text: string;
}
type Segment = GoodSegment | BadSegment;
type ReducedCitation = [string, string, number, number];

const goodSegment: fc.Arbitrary<GoodSegment> = fc
  .tuple(displayNames, wholeSecondRanges)
  .map(([name, [startMs, endMs]]) => ({
    kind: 'good' as const,
    text: formatCitation(name, startMs, endMs),
    name,
    startMs,
    endMs,
  }));
const badSegment: fc.Arbitrary<BadSegment> = badCandidate.map((text) => ({
  kind: 'bad' as const,
  text,
}));
const segment: fc.Arbitrary<Segment> = fc.oneof(goodSegment, badSegment);

interface Interleaving {
  full: string;
  cleaned: string;
  expected: ReducedCitation[];
  badTexts: string[];
}

// Interleave valid citations and malformed candidates with bracket-free
// separators. `full` is the whole text; `cleaned` is the same text with every
// malformed candidate removed; `expected` is the reduced tuple of the valid
// citations in order; `badTexts` are the removed candidate strings.
const interleaved: fc.Arbitrary<Interleaving> = fc
  .tuple(separators, fc.array(fc.tuple(segment, separators), { maxLength: 6 }))
  .map(([firstSep, pairs]) => {
    const full: string[] = [firstSep];
    const cleaned: string[] = [firstSep];
    const expected: ReducedCitation[] = [];
    const badTexts: string[] = [];

    for (const [seg, sep] of pairs) {
      if (seg.kind === 'good') {
        full.push(seg.text);
        cleaned.push(seg.text);
        expected.push([seg.text, seg.name, seg.startMs, seg.endMs]);
      } else {
        full.push(seg.text);
        badTexts.push(seg.text);
      }
      full.push(sep);
      cleaned.push(sep);
    }

    return {
      full: full.join(''),
      cleaned: cleaned.join(''),
      expected,
      badTexts,
    };
  });

/** Each Citation reduced to the tuple Property 7 compares on. */
function reduced(citations: ReturnType<typeof parseCitations>): ReducedCitation[] {
  return citations.map((c) => [c.text, c.displayName, c.startMs, c.endMs]);
}

describe('Property 7: Malformed bracketed text stays plain', () => {
  // Validates: Requirements 2.3
  //
  // Malformed bracketed candidates produce no Citation, and the Citations parsed
  // from the full text are exactly those parsed from the same text with every
  // malformed candidate removed.
  it('leaves malformed bracketed candidates plain while valid citations still parse', () => {
    fc.assert(
      fc.property(interleaved, ({ full, cleaned, expected, badTexts }) => {
        const cleanedCitations = reduced(parseCitations(cleaned));
        const fullCitations = reduced(parseCitations(full));

        // The valid citations survive on their own, in order.
        expect(cleanedCitations).toEqual(expected);
        // The malformed candidates add nothing: full parses to exactly the cleaned set.
        expect(fullCitations).toEqual(cleanedCitations);

        // No malformed candidate was returned as a Citation.
        const returnedTexts = new Set(fullCitations.map(([text]) => text));
        for (const candidate of badTexts) {
          expect(returnedTexts.has(candidate)).toBe(false);
        }
      }),
      { numRuns: 400 },
    );
  });
});
