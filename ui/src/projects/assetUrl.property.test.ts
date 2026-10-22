// Feature: media-harness-search-qa-rendering, Property 20: Asset URL refresh state machine
//
// For any sequence of UrlEvents with non-decreasing `now`, arbitrary `obtainedAt`
// values, and interleaved load_error / load_ok / minted / mint_failed, driving `step`
// from `{ phase: 'minting' }`:
//
//   1. within one load (from a `load_start` to the next `load_start`), `step` returns
//      `mint: true` for a refresh at most once (the SAFETY invariant);
//   2. every `load_start` that leaves the state `ready` without a mint has
//      `now - minted.obtainedAt <= URL_MAX_AGE_MS`;
//   3. `failed` is absorbing and never returns `mint: true`;
//   4. the `resume` carried into `minting` equals the triggering event's `resume`, and
//      it is kept until the next `load_ok` (the one processed from `ready`; a `load_ok`
//      while a mint is in flight is a no-op that keeps the pending resume, per the
//      module contract).
//
// Note on the task 8.2 deviation: the implementation keeps `refreshUsed` through
// `load_ok` (a documented departure from the design table) precisely to uphold
// invariant 1 — a later error in the same load cannot mint again. This test encodes
// that SAFETY invariant directly, so it is agnostic to how the field is threaded.
//
// **Validates: Requirements 2.12, 3.27**

import { describe, it, expect } from "vitest";
import fc from "fast-check";

import {
  URL_MAX_AGE_MS,
  step,
  type UrlState,
  type UrlEvent,
  type MintedUrl,
  type Resume,
  type FailReason,
} from "./assetUrl";

// A fresh Resume object per draw, so reference equality (`toBe`) is meaningful for
// invariant 4 — the exact triggering object must be the one carried into `minting`.
const resumeArb: fc.Arbitrary<Resume> = fc.record({
  time: fc.integer({ min: 0, max: 100_000 }),
  playing: fc.boolean(),
});

// `obtainedAt` is arbitrary and spans well before and well after the `now` scale, so
// both stale and fresh `load_start`s are exercised (URL_MAX_AGE_MS = 3,000,000 ms).
const mintedArb: fc.Arbitrary<MintedUrl> = fc.record({
  url: fc.string({ maxLength: 12 }),
  obtainedAt: fc.integer({ min: -2 * URL_MAX_AGE_MS, max: 10_000_000 }),
});

const failReasonArb: fc.Arbitrary<FailReason> = fc.constantFrom(
  "not_found",
  "removed",
  "load_failed",
);

// Event descriptors: `load_start` carries a non-negative delta so that threading
// produces a non-decreasing `now`.
type EventSpec =
  | { kind: "minted"; minted: MintedUrl }
  | { kind: "mint_failed"; reason: FailReason }
  | { kind: "load_start"; nowDelta: number; resume: Resume }
  | { kind: "load_ok" }
  | { kind: "load_error"; resume: Resume };

const eventSpecArb: fc.Arbitrary<EventSpec> = fc.oneof(
  fc.record({ kind: fc.constant("minted" as const), minted: mintedArb }),
  fc.record({ kind: fc.constant("mint_failed" as const), reason: failReasonArb }),
  fc.record({
    kind: fc.constant("load_start" as const),
    nowDelta: fc.integer({ min: 0, max: 2_000_000 }),
    resume: resumeArb,
  }),
  fc.record({ kind: fc.constant("load_ok" as const) }),
  fc.record({ kind: fc.constant("load_error" as const), resume: resumeArb }),
);

// Fold descriptors into UrlEvents, threading a monotonically non-decreasing `now`
// that advances only at `load_start` (the only event carrying `now`). The `resume`
// reference is passed through unchanged so invariant 4 can be checked by identity.
function toEvents(specs: EventSpec[]): UrlEvent[] {
  let now = 0;
  const out: UrlEvent[] = [];
  for (const s of specs) {
    switch (s.kind) {
      case "minted":
        out.push({ type: "minted", minted: s.minted });
        break;
      case "mint_failed":
        out.push({ type: "mint_failed", reason: s.reason });
        break;
      case "load_start":
        now += s.nowDelta;
        out.push({ type: "load_start", now, resume: s.resume });
        break;
      case "load_ok":
        out.push({ type: "load_ok" });
        break;
      case "load_error":
        out.push({ type: "load_error", resume: s.resume });
        break;
    }
  }
  return out;
}

describe("Property 20: asset URL refresh state machine", () => {
  it("upholds the refresh, freshness, absorbing-failed, and resume-carry invariants", () => {
    fc.assert(
      fc.property(fc.array(eventSpecArb, { maxLength: 40 }), (specs) => {
        const events = toEvents(specs);

        let state: UrlState = { phase: "minting", resume: null };

        // Invariant 1 bookkeeping: mints seen in the current load window. A window
        // opens at each `load_start`; events before the first `load_start` form the
        // initial mount window.
        let mintsInWindow = 0;

        // Invariant 4 bookkeeping: the resume reference carried since the most recent
        // entry into `minting`, or null when not currently carrying.
        let carried: Resume | null = null;

        for (const e of events) {
          const before = state;

          // A new load window opens on `load_start` (reset before processing it, so
          // a stale `load_start`'s own refresh counts toward the window it opens).
          if (e.type === "load_start") {
            mintsInWindow = 0;
          }

          const { state: after, mint } = step(before, e);
          state = after;

          // Invariant 3: `failed` is absorbing — nothing leaves it, nothing mints.
          if (before.phase === "failed") {
            expect(mint).toBe(false);
            expect(after.phase).toBe("failed");
            if (after.phase === "failed") {
              expect(after.reason).toBe(before.reason);
            }
          }

          // Invariant 1: at most one refresh per load window.
          if (mint) {
            mintsInWindow += 1;
            expect(mintsInWindow).toBeLessThanOrEqual(1);
            // A mint is only ever requested from `ready` and lands in `minting`.
            expect(before.phase).toBe("ready");
            expect(after.phase).toBe("minting");
          }

          // Invariant 2: a `load_start` that stays `ready` without a mint had a URL
          // obtained no more than URL_MAX_AGE_MS ago.
          if (e.type === "load_start" && !mint && after.phase === "ready") {
            expect(e.now - after.minted.obtainedAt).toBeLessThanOrEqual(URL_MAX_AGE_MS);
          }

          // Invariant 4: resume carried into `minting` equals the triggering event's
          // resume and is kept until the clearing `load_ok` (processed from `ready`).
          if (mint) {
            // Mint-triggering events (stale load_start, first load_error) carry a resume.
            const trigger = e as Extract<UrlEvent, { resume: Resume }>;
            expect(after.phase).toBe("minting");
            if (after.phase === "minting") {
              expect(after.resume).toBe(trigger.resume);
            }
            carried = trigger.resume;
          } else if (carried !== null) {
            if (after.phase === "failed") {
              // Load gave up; the carried resume is dropped with the state.
              carried = null;
            } else if (before.phase === "ready" && e.type === "load_ok") {
              // The reload landed: the carried resume is cleared here, and only here.
              expect(after.resume).toBeNull();
              carried = null;
            } else {
              // Still carrying: the exact same resume object is preserved.
              expect(after.resume).toBe(carried);
            }
          }
        }
      }),
    );
  });
});
