// Feature: media-harness-projects-access, Property 19: Project ordering
//
// For any list of Projects with arbitrary names (case variants, accents, digits,
// duplicates across Owners) and exactly one Personal_Project at any position,
// `orderProjects` returns a permutation of the input whose first element is the
// Personal_Project and whose remaining elements are in non-decreasing
// `localeCompare(…, {sensitivity: "base"})` order of their names.
//
// **Validates: Requirements 3.2, 10.5**

import { describe, it, expect } from "vitest";
import fc from "fast-check";

import { orderProjects } from "./orderProjects";

interface Row {
  id: number;
  name: string;
  personal: boolean;
}

// Base words with case, accent, and digit variants so equal-under-base and
// near-equal names collide often; plus arbitrary strings for breadth.
const BASES = ["alpha", "Alpha", "ALPHA", "álpha", "Ålpha", "resume", "résumé", "Résumé", "zeta", "Zeta", "10", "2", "a1", "a10", "a2", "élan", "Elan", "trailer cuts"];
const nameArb = fc.oneof(
  fc.constantFrom(...BASES),
  fc.tuple(fc.constantFrom(...BASES), fc.integer({ min: 0, max: 20 })).map(([b, n]) => `${b}${n}`),
  fc.string({ minLength: 1, maxLength: 12 }).map((s) => s.trim()).filter((s) => s.length > 0),
);

const inputArb = fc
  .tuple(fc.array(nameArb, { maxLength: 30 }), fc.nat())
  .chain(([names, pos]) => {
    // Duplicate some names on purpose (duplicates across Owners are legal).
    return fc.subarray(names).map((dups) => {
      const others = [...names, ...dups];
      const at = others.length === 0 ? 0 : pos % (others.length + 1);
      const rows: Row[] = others.map((name, i) => ({ id: i + 1, name, personal: false }));
      rows.splice(at, 0, { id: 0, name: "Personal", personal: true });
      return rows;
    });
  });

describe("Property 19: Project ordering", () => {
  it("is a permutation, Personal first, the rest in non-decreasing base order", () => {
    fc.assert(
      fc.property(inputArb, (input) => {
        const snapshot = input.map((r) => ({ ...r }));
        const out = orderProjects(input);

        // Input not mutated.
        expect(input).toEqual(snapshot);

        // Permutation: same multiset of row identities.
        expect(out).toHaveLength(input.length);
        expect(out.map((r) => r.id).sort((a, b) => a - b)).toEqual(input.map((r) => r.id).sort((a, b) => a - b));

        // Personal first.
        expect(out[0].personal).toBe(true);
        expect(out.slice(1).every((r) => !r.personal)).toBe(true);

        // Rest non-decreasing under base sensitivity.
        for (let i = 2; i < out.length; i++) {
          expect(out[i - 1].name.localeCompare(out[i].name, undefined, { sensitivity: "base" })).toBeLessThanOrEqual(0);
        }
      }),
      { numRuns: 200 },
    );
  });

  it("groups case and accent variants as equal (fixed example)", () => {
    const out = orderProjects([
      { name: "zeta", personal: false },
      { name: "Résumé", personal: false },
      { name: "alpha", personal: false },
      { name: "Personal", personal: true },
      { name: "resume", personal: false },
      { name: "ALPHA", personal: false },
    ]);
    expect(out.map((p) => p.name)).toEqual(["Personal", "alpha", "ALPHA", "Résumé", "resume", "zeta"]);
  });
});
