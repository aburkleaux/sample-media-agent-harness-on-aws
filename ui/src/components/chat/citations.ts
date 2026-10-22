// Citation_Formatter and Citation_Parser (Component 3, media-harness-search-qa-rendering).
//
// Pure, dependency-free module. The TypeScript twin of
// agent/src/project_search/citations.py. Both implementations share one grammar and one
// test fixture (infra/lib/projects/fixtures/citation-cases.json) so they cannot drift: the
// fixture test asserts CITATION_PATTERN is byte-identical in both files, and every
// format/parse/keys case matches the Python results.
//
// Implements Requirements 2.1-2.4.

// The citation grammar, byte-identical to the `pattern` field of
// infra/lib/projects/fixtures/citation-cases.json (and to the Python CITATION_PATTERN).
// The fixture test asserts this equality. The literal `\u2013` escape (en-dash) is kept as
// source text — the RegExp engine interprets it when compiling, yielding a character class
// of the en-dash (U+2013) and the hyphen-minus (U+002D).
export const CITATION_PATTERN: string =
  '\\[([^\\[\\]\\r\\n]+?) *@ *((?:\\d{1,2}:[0-5]\\d|[0-5]?\\d):[0-5]\\d) *[\\u2013-] *' +
  '((?:\\d{1,2}:[0-5]\\d|[0-5]?\\d):[0-5]\\d) *\\](?!\\()';

// The PAGE citation grammar for a document (PDF) hit: `[report.pdf p.3]` / `[report.pdf @ p.3]`, or a RANGE
// `[report.pdf p.1–2]` / `[report.pdf pp. 1-2]` (session 993b85b5: ranges were left as plain text).
// Byte-identical to the fixture's `pagePattern` (and the Python PAGE_CITATION_PATTERN). Whitespace or `@`
// must precede `p.`, so `[app.3]` is not "ap" page 3. A document hit's span encodes its page, not a time.
export const PAGE_CITATION_PATTERN: string =
  '\\[([^\\[\\]\\r\\n]+?)(?: *@ *| +)pp?\\. ?([1-9]\\d{0,3})(?: *[\\u2013-] *([1-9]\\d{0,3}))? *\\](?!\\()';

/** Must equal `shared/pageLocator.ts` PAGE_STRIDE_MS and the agent's (fixtures/search-kinds.json). */
const PAGE_STRIDE_MS = 10_000;

// Maximum Display_Name length, in characters, after trimming (Req 2.3).
export const NAME_MAX = 255;

// The explicit trim set shared with the Python side. Python str.strip() (no args) and JS
// String.prototype.trim() disagree on some characters, so neither default is used: both
// languages trim exactly this set.
// U+0020, U+0009, U+00A0, U+1680, U+2000-U+200A, U+202F, U+205F, U+3000, U+FEFF.
const TRIM_CHARS =
  '\u0020\u0009\u00a0\u1680' +
  '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a' +
  '\u202f\u205f\u3000\ufeff';

// The separator the formatter writes between timecodes: en-dash (U+2013).
const FORMAT_DASH = '\u2013';

const TRIM_SET = new Set(TRIM_CHARS);

/** Strip the explicit TRIM_CHARS set from both ends (not String.prototype.trim()). */
function trim(name: string): string {
  let start = 0;
  let end = name.length;
  while (start < end && TRIM_SET.has(name[start])) {
    start += 1;
  }
  while (end > start && TRIM_SET.has(name[end - 1])) {
    end -= 1;
  }
  return name.slice(start, end);
}

/** 'mm:ss' or 'h:mm:ss' -> whole-second milliseconds. */
function timecodeToMs(timecode: string): number {
  const parts = timecode.split(':');
  let hours: number;
  let minutes: number;
  let seconds: number;
  if (parts.length === 2) {
    hours = 0;
    minutes = parseInt(parts[0], 10);
    seconds = parseInt(parts[1], 10);
  } else {
    hours = parseInt(parts[0], 10);
    minutes = parseInt(parts[1], 10);
    seconds = parseInt(parts[2], 10);
  }
  return (hours * 3600 + minutes * 60 + seconds) * 1000;
}

/**
 * 'mm:ss' when totalSeconds < 3600, else 'h:mm:ss' with h unpadded. Throws when negative.
 */
export function formatCitationTimecode(totalSeconds: number): string {
  if (totalSeconds < 0) {
    throw new Error('totalSeconds must not be negative');
  }
  if (totalSeconds < 3600) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${pad2(minutes)}:${pad2(seconds)}`;
  }
  const hours = Math.floor(totalSeconds / 3600);
  const remainder = totalSeconds % 3600;
  const minutes = Math.floor(remainder / 60);
  const seconds = remainder % 60;
  return `${hours}:${pad2(minutes)}:${pad2(seconds)}`;
}

function pad2(value: number): string {
  return value.toString().padStart(2, '0');
}

/**
 * '[<displayName> @ <floor start>-<ceil end>]' using integer floor on the start and integer
 * ceiling on the end. Throws when startMs < 0 or endMs < startMs.
 */
export function formatCitation(displayName: string, startMs: number, endMs: number): string {
  if (startMs < 0 || endMs < startMs) {
    throw new Error('require 0 <= startMs <= endMs');
  }
  const startSeconds = Math.floor(startMs / 1000);
  const endSeconds = Math.ceil(endMs / 1000);
  return `[${displayName} @ ${formatCitationTimecode(startSeconds)}${FORMAT_DASH}${formatCitationTimecode(endSeconds)}]`;
}

/**
 * name with TRIM_CHARS stripped from both ends, then toLowerCase(). The case-insensitive
 * comparison key of Req 2.5.
 */
export function normalizeName(name: string): string {
  return trim(name).toLowerCase();
}

/** `${normalizeName(displayName)}|p${page}` — a page citation's key, never equal to a timecode key. */
export function pageCitationKey(displayName: string, page: number, pageEnd?: number): string {
  if (pageEnd === undefined || pageEnd === page) return `${normalizeName(displayName)}|p${page}`;
  return `${normalizeName(displayName)}|p${page}-${pageEnd}`;
}

/** `[<displayName> p.<page>]`, or `[<displayName> p.<page>–<pageEnd>]`. Throws unless 1 <= page <= pageEnd. */
export function formatPageCitation(displayName: string, page: number, pageEnd?: number): string {
  if (!(page >= 1) || (pageEnd !== undefined && pageEnd < page)) throw new Error('require 1 <= page <= pageEnd');
  if (pageEnd === undefined || pageEnd === page) return `[${displayName} p.${page}]`;
  return `[${displayName} p.${page}${FORMAT_DASH}${pageEnd}]`;
}

/** `${normalizeName(displayName)}|${startMs}|${endMs}`. Identical in Python. */
export function citationKey(displayName: string, startMs: number, endMs: number): string {
  return `${normalizeName(displayName)}|${startMs}|${endMs}`;
}

export interface Citation {
  displayName: string; // trimmed, original case
  startMs: number; // whole seconds x 1000
  endMs: number; // whole seconds x 1000, >= startMs
  span: [number, number]; // [start, end) UTF-16 code-unit offsets of the bracketed text
  text: string; // the bracketed text exactly as written
  /** A page citation (a document): its first page; startMs === (page - 1) * PAGE_STRIDE_MS. */
  page?: number;
  /** Its last page (=== page for one page); endMs === (pageEnd - 1) * PAGE_STRIDE_MS. */
  pageEnd?: number;
}

/** The key a Citation's record carries (`CitationRecordWire.key`): the page form for a page citation. */
export function keyOfCitation(c: Citation): string {
  return c.page !== undefined ? pageCitationKey(c.displayName, c.page, c.pageEnd) : citationKey(c.displayName, c.startMs, c.endMs);
}

/**
 * Every Citation in `text`, in order of appearance (Req 2.2, 2.3).
 *
 * A match is rejected (and its bracketed text left plain) when the name is empty after
 * trimming, longer than NAME_MAX after trimming, or its start is later than its end.
 */
export function parseCitations(text: string): Citation[] {
  const pattern = new RegExp(CITATION_PATTERN, 'g');
  const citations: Citation[] = [];
  for (const match of text.matchAll(new RegExp(PAGE_CITATION_PATTERN, 'g'))) {
    const displayName = trim(match[1]);
    if (!displayName || displayName.length > NAME_MAX) continue;
    const page = parseInt(match[2], 10);
    const pageEnd = match[3] ? parseInt(match[3], 10) : page;
    if (pageEnd < page) continue;
    const start = match.index ?? 0;
    citations.push({
      displayName, startMs: (page - 1) * PAGE_STRIDE_MS, endMs: (pageEnd - 1) * PAGE_STRIDE_MS,
      span: [start, start + match[0].length], text: match[0], page, pageEnd,
    });
  }
  for (const match of text.matchAll(pattern)) {
    const displayName = trim(match[1]);
    if (!displayName || displayName.length > NAME_MAX) {
      continue;
    }
    const startMs = timecodeToMs(match[2]);
    const endMs = timecodeToMs(match[3]);
    if (startMs > endMs) {
      continue;
    }
    const start = match.index ?? 0;
    citations.push({
      displayName,
      startMs,
      endMs,
      span: [start, start + match[0].length],
      text: match[0],
    });
  }
  citations.sort((a, b) => a.span[0] - b.span[0]);
  return citations;
}
