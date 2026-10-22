// Property 2: Name validation (media-harness-projects-access design, "Correctness properties").
// **Validates: Requirements 1.2**
//
// For any submitted string and any set of names the same Owner already holds, validation accepts
// exactly when the trimmed string is non-empty, at most 64 code points, contains no \p{Cc}, does not
// fold to `personal`, and does not fold to an existing name of that Owner; a rejection names the
// first violated rule. The oracle below restates each rule independently of names.ts (code-point
// ranges for Cc, Array.from for length) so the property is not the implementation read back.

import { describe, it, expect } from "vitest";
import fc from "fast-check";

import { NAME_RULES, foldName, validateProjectName } from "./names";

function isCc(cp: number): boolean {
  return cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f);
}

function expectedRule(raw: string, folds: ReadonlySet<string>): string | null {
  const t = raw.trim();
  const cps = Array.from(t, (c) => c.codePointAt(0)!);
  if (cps.length === 0) return NAME_RULES.empty;
  if (cps.length > 64) return NAME_RULES.tooLong;
  if (cps.some(isCc)) return NAME_RULES.controlCharacter;
  const f = t.normalize("NFC").toLowerCase();
  if (f === "personal") return NAME_RULES.reserved;
  if (folds.has(f)) return NAME_RULES.duplicate;
  return null;
}

// Pieces that steer generated names into every rule and across its boundary.
const piece = fc.oneof(
  fc.string({ unit: "binary", maxLength: 6 }),
  fc.constantFrom(" ", "\t", "\n", "\u00a0", "\u0000", "\u0007", "\u007f", "\u0085", "\u009f"),
  fc.constantFrom("😀", "👍🏽", "e\u0301", "\u00e9", "É", "ß", "İ"),
  fc.constantFrom("Personal", "PERSONAL", "personal", "PeRsOnAl"),
  fc.string({ unit: "grapheme-ascii", minLength: 1, maxLength: 70 }),
  fc.integer({ min: 60, max: 68 }).map((n) => "a".repeat(n)),
  fc.integer({ min: 60, max: 68 }).map((n) => "😀".repeat(n)),
);
const nameArb = fc.array(piece, { maxLength: 4 }).map((ps) => ps.join(""));

// Owner names: arbitrary, plus case/normalization variants of the candidate so duplicates occur.
const variantArb = (raw: string) =>
  fc.constantFrom(
    raw,
    raw.trim().toUpperCase(),
    raw.trim().toLowerCase(),
    raw.trim().normalize("NFD"),
    ` ${raw.trim()} `,
  );

const inputArb = fc
  .tuple(nameArb, fc.array(nameArb, { maxLength: 4 }), fc.boolean())
  .chain(([raw, others, includeVariant]) =>
    (includeVariant ? variantArb(raw).map((v) => [...others, v]) : fc.constant(others)).map(
      (owned) => ({ raw, folds: new Set(owned.map((n) => foldName(n.trim()))) }),
    ),
  );

describe("Property 2: name validation", () => {
  it("accepts exactly when every rule holds, else reports the first violated rule", () => {
    fc.assert(
      fc.property(inputArb, ({ raw, folds }) => {
        const want = expectedRule(raw, folds);
        const got = validateProjectName(raw, folds);
        if (want === null) {
          expect(got).toBe(raw.trim());
        } else {
          expect(got).toEqual({ code: "invalid_name", rule: want });
        }
      }),
      { numRuns: 1000 },
    );
  });

  it("covers each rule and its boundary (table)", () => {
    const none = new Set<string>();
    const cases: Array<[string, ReadonlySet<string>, string | null]> = [
      ["", none, NAME_RULES.empty],
      [" \t\n ", none, NAME_RULES.empty],
      ["a".repeat(64), none, null],
      ["a".repeat(65), none, NAME_RULES.tooLong],
      ["😀".repeat(64), none, null], // 64 code points, 128 UTF-16 units (D9)
      ["😀".repeat(65), none, NAME_RULES.tooLong],
      [`  ${"a".repeat(64)}  `, none, null],
      ["a\u0000b", none, NAME_RULES.controlCharacter],
      ["a\u0085b", none, NAME_RULES.controlCharacter],
      ["\u0085", none, NAME_RULES.controlCharacter], // NEL is not trimmed
      [` ${"a".repeat(65)}\u0000`, none, NAME_RULES.tooLong], // first rule only
      [" PERSONAL ", none, NAME_RULES.reserved],
      ["Personal\u0007", none, NAME_RULES.controlCharacter],
      ["Café", new Set([foldName("cafe\u0301")]), NAME_RULES.duplicate], // NFC fold
      ["Café", new Set(["cafe"]), null],
      ["Personal", new Set(["personal"]), NAME_RULES.reserved], // reserved before duplicate
      ["  Reels  ", none, null],
    ];
    for (const [raw, folds, want] of cases) {
      const got = validateProjectName(raw, folds);
      if (want === null) expect(got).toBe(raw.trim());
      else expect(got).toEqual({ code: "invalid_name", rule: want });
    }
  });
});
