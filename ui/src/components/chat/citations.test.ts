// Property 8: Python and TypeScript parsers agree (media-harness-search-qa-rendering
// task 3.9), TypeScript half. The Python half is agent/tests/test_citations.py
// (test_corpus_in_sync and friends).
//
// Cross-running Python from vitest is impractical, so agreement is proved through
// committed artifacts both languages read:
//
//   * infra/lib/projects/fixtures/citation-cases.json          -- `parse`/`keys` cases
//   * infra/lib/projects/fixtures/citation-corpus.json          -- 2,000 input strings
//   * infra/lib/projects/fixtures/citation-parse-expected.json  -- the committed Python
//     parse results for the fixture `parse` inputs AND the corpus inputs
//
// This suite parses the same inputs with parseCitations, asserts the TS
// CITATION_PATTERN is byte-identical to the fixture `pattern`, and compares every
// result against the Python-produced artifact. Both parsers matching one artifact
// proves they agree. A disagreement names the offending input rather than hiding it.
//
// Pure node — no DOM — so no jsdom pragma. The fixtures are read with readFileSync
// (mirrors ui/server/projects/ids.test.ts); importing JSON keeps vitest from needing
// resolveJsonModule and keeps the file outside the tsconfig `src` build.
//
// Validates: Requirements 2.2, 2.3, 2.5

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { CITATION_PATTERN, PAGE_CITATION_PATTERN, citationKey, keyOfCitation, normalizeName, pageCitationKey, parseCitations } from './citations';

const FIXTURES_DIR = join(__dirname, '../../../../infra/lib/projects/fixtures');

function loadJson(name: string): any {
  return JSON.parse(readFileSync(join(FIXTURES_DIR, name), 'utf8'));
}

const cases = loadJson('citation-cases.json');
const corpus = loadJson('citation-corpus.json');
const expected = loadJson('citation-parse-expected.json');

/**
 * parseCitations(text) reduced to the Property 8 cross-language tuple
 * [displayName, startMs, endMs, text]. Span offsets are omitted: Python counts
 * code points and JS counts UTF-16 code units, which differ for astral characters.
 */
function reduce(text: string): Array<[string, number, number, string]> {
  return parseCitations(text).map((c) => [c.displayName, c.startMs, c.endMs, c.text]);
}

describe('Property 8: Python and TypeScript citation parsers agree', () => {
  it('CITATION_PATTERN is byte-identical to the fixture pattern', () => {
    expect(CITATION_PATTERN).toBe(cases.pattern);
    // The artifact pins the same grammar the Python parser used.
    expect(CITATION_PATTERN).toBe(expected.pattern);
    expect(PAGE_CITATION_PATTERN).toBe(cases.pagePattern);
    expect(PAGE_CITATION_PATTERN).toBe(expected.pagePattern);
  });

  it('parses every fixture `parse` case to its recorded citations', () => {
    cases.parse.forEach((c: any) => {
      const want = c.citations.map((x: any) => [x.display_name, x.start_ms, x.end_ms, x.text]);
      expect(reduce(c.input), `fixture parse case: ${JSON.stringify(c.input)}`).toEqual(want);
    });
  });

  it('matches the committed Python results for the fixture `parse` inputs', () => {
    cases.parse.forEach((c: any, i: number) => {
      expect(reduce(c.input), `fixture parse case #${i}: ${JSON.stringify(c.input)}`).toEqual(
        expected.fixtureParse[i],
      );
    });
  });

  it('matches the committed Python results for all 2,000 corpus inputs', () => {
    expect(corpus.inputs.length).toBe(expected.corpus.length);
    corpus.inputs.forEach((input: string, i: number) => {
      expect(reduce(input), `corpus input #${i}: ${JSON.stringify(input)}`).toEqual(
        expected.corpus[i],
      );
    });
  });

  it('agrees on normalizeName and citationKey for every `keys` case', () => {
    cases.keys.forEach((c: any) => {
      const key = c.page !== undefined ? pageCitationKey(c.name, c.page, c.page_end) : citationKey(c.name, c.start_ms, c.end_ms);
      expect(key, `keys case: ${JSON.stringify(c.name)}`).toBe(c.key);
      // normalizeName is the key prefix before the first '|'.
      expect(normalizeName(c.name), `keys case: ${JSON.stringify(c.name)}`).toBe(
        c.key.slice(0, c.key.indexOf('|')),
      );
    });
  });
});

describe('page citations (documents)', () => {
  it('key a page citation by page, never like the same span as a timecode', () => {
    const [page] = parseCitations('see [report.pdf p.3]');
    const [time] = parseCitations('see [report.pdf @ 00:20–00:20]');
    expect(page).toMatchObject({ displayName: 'report.pdf', page: 3, startMs: 20_000, endMs: 20_000 });
    expect(keyOfCitation(page)).toBe('report.pdf|p3');
    expect(keyOfCitation(time)).not.toBe(keyOfCitation(page));
  });
});
