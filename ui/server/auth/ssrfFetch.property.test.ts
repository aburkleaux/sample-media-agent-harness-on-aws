// Feature: custom-agents, Property 14: SSRF address/scheme classification
//
// Property 14 (design.md): For any candidate issuer/JWKS URL and its resolved
// address, the fetch is accepted IF AND ONLY IF the scheme is HTTPS AND the
// resolved address is not loopback / link-local / cloud-metadata
// (169.254.169.254) / RFC1918-private / CGNAT / IPv6-ULA / unspecified; and the
// same classifier is applied to every redirect-hop target.
//
// This exercises the pure classifier exports `classifyAddress` and
// `classifyDestination` from `./ssrfFetch` (task 11.1) directly across
// generated IPv4, IPv6, and IPv4-mapped-IPv6 addresses in every bucket, crossed
// with HTTPS/HTTP/FTP schemes, and over sequences of per-hop redirect targets.
// Acceptance is checked against an INDEPENDENT oracle (`oracleBlocked`) that is
// re-derived in this test from the RFC definitions rather than from the
// implementation under test.
//
// Validates: Requirements 12.1, 12.2, 12.3

import { describe, it, expect } from "vitest";
import fc from "fast-check";

import { classifyAddress, classifyDestination } from "./ssrfFetch";

const NUM_RUNS = 200; // Property 14 requires a minimum of 100 iterations.

// ---------------------------------------------------------------------------
// Independent oracle — a from-scratch reimplementation of "is this resolved
// address a NON-routable (blocked) destination?", derived directly from the RFC
// range definitions. It shares no code with the module under test.
// ---------------------------------------------------------------------------

function oracleBlockedIpv4(octets: number[]): boolean {
  const [a, b, c, d] = octets;
  if (a === 0) return true; // 0.0.0.0/8 unspecified / "this host"
  if (a === 127) return true; // 127.0.0.0/8 loopback
  if (a === 169 && b === 254) return true; // 169.254.0.0/16 link-local (+ metadata)
  if (a === 10) return true; // 10.0.0.0/8 RFC1918
  if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12 RFC1918
  if (a === 192 && b === 168) return true; // 192.168.0.0/16 RFC1918
  if (a === 100 && b >= 64 && b <= 127) return true; // 100.64.0.0/10 CGNAT
  if (a === 255 && b === 255 && c === 255 && d === 255) return true; // broadcast
  return false;
}

/** Parse a dotted-quad into octets, or null if malformed. */
function parseIpv4(address: string): number[] | null {
  const parts = address.split(".");
  if (parts.length !== 4) return null;
  const octets = parts.map((p) => Number(p));
  if (octets.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return null;
  return octets;
}

/**
 * Independent oracle for any resolved address string (IPv4, IPv6, or
 * IPv4-mapped IPv6). Returns true when the address must be BLOCKED.
 */
function oracleBlocked(address: string): boolean {
  const v4 = parseIpv4(address);
  if (v4) return oracleBlockedIpv4(v4);

  // IPv6 (all generated forms here are full 8-group, "::"-compressed for the
  // loopback/unspecified constants, or IPv4-mapped ::ffff:a.b.c.d).
  const lower = address.toLowerCase();

  if (lower === "::") return true; // unspecified
  if (lower === "::1") return true; // loopback

  // IPv4-mapped ::ffff:a.b.c.d — unwrap and defer to the IPv4 oracle.
  const mapped = lower.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) {
    const inner = parseIpv4(mapped[1]);
    return inner ? oracleBlockedIpv4(inner) : true;
  }

  // Otherwise a full 8-group address; classify by its first hextet.
  const groups = lower.split(":");
  const first = parseInt(groups[0] || "0", 16);
  if ((first & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
  if ((first & 0xfe00) === 0xfc00) return true; // fc00::/7 unique-local (ULA)
  return false; // routable public IPv6
}

// ---------------------------------------------------------------------------
// Address generators — one per bucket so every category is exercised. Each
// yields an address STRING only; the blocked/public verdict is computed by the
// independent oracle above, never carried alongside the value.
// ---------------------------------------------------------------------------

const octet = fc.integer({ min: 0, max: 255 });
/** A hextet rendered without leading zeros, matching net's canonical form. */
const hextet = fc.integer({ min: 0, max: 0xffff }).map((n) => n.toString(16));

// --- IPv4 buckets ---------------------------------------------------------

const loopbackV4 = fc
  .tuple(octet, octet, octet)
  .map(([b, c, d]) => `127.${b}.${c}.${d}`);

const linkLocalV4 = fc
  .tuple(octet, octet)
  .map(([c, d]) => `169.254.${c}.${d}`);

const metadataV4 = fc.constant("169.254.169.254");

const rfc1918V4 = fc.oneof(
  fc.tuple(octet, octet, octet).map(([b, c, d]) => `10.${b}.${c}.${d}`),
  fc
    .tuple(fc.integer({ min: 16, max: 31 }), octet, octet)
    .map(([b, c, d]) => `172.${b}.${c}.${d}`),
  fc.tuple(octet, octet).map(([c, d]) => `192.168.${c}.${d}`)
);

const cgnatV4 = fc
  .tuple(fc.integer({ min: 64, max: 127 }), octet, octet)
  .map(([b, c, d]) => `100.${b}.${c}.${d}`);

// Public first octets chosen so that ANY value of the remaining octets stays
// routable (none fall into 0/10/127/169.254/172.16-31/192.168/100.64-127/255).
const PUBLIC_V4_FIRST = [1, 8, 23, 52, 93, 140, 151, 185, 203, 208, 216];
const publicV4 = fc
  .tuple(fc.constantFrom(...PUBLIC_V4_FIRST), octet, octet, octet)
  .map(([a, b, c, d]) => `${a}.${b}.${c}.${d}`);

// --- IPv6 buckets (full 8-group form, plus the compressed specials) -------

const loopbackV6 = fc.constant("::1");
const unspecifiedV6 = fc.constant("::");

// fe80::/10 — first hextet in 0xfe80..0xfebf, remaining 7 arbitrary.
const linkLocalV6 = fc
  .tuple(fc.integer({ min: 0xfe80, max: 0xfebf }), fc.array(hextet, {
    minLength: 7,
    maxLength: 7,
  }))
  .map(([first, rest]) => [first.toString(16), ...rest].join(":"));

// fc00::/7 — first hextet in 0xfc00..0xfdff, remaining 7 arbitrary.
const ulaV6 = fc
  .tuple(fc.integer({ min: 0xfc00, max: 0xfdff }), fc.array(hextet, {
    minLength: 7,
    maxLength: 7,
  }))
  .map(([first, rest]) => [first.toString(16), ...rest].join(":"));

// Public IPv6 — first hextet a global-unicast value (2xxx/3xxx region), so the
// remaining hextets cannot flip it into a blocked range.
const publicV6 = fc
  .tuple(fc.constantFrom(0x2001, 0x2606, 0x2a00, 0x2400, 0x3ffe), fc.array(
    hextet,
    { minLength: 7, maxLength: 7 }
  ))
  .map(([first, rest]) => [first.toString(16), ...rest].join(":"));

// IPv4-mapped IPv6 (::ffff:a.b.c.d) built from any of the IPv4 buckets, so the
// mapped forms of loopback/metadata/private/public are all exercised.
const anyV4 = fc.oneof(
  loopbackV4,
  linkLocalV4,
  metadataV4,
  rfc1918V4,
  cgnatV4,
  publicV4
);
const ipv4MappedV6 = anyV4.map((v4) => `::ffff:${v4}`);

/** Every address bucket, IPv4 + IPv6 + IPv4-mapped. */
const anyAddress = fc.oneof(
  loopbackV4,
  linkLocalV4,
  metadataV4,
  rfc1918V4,
  cgnatV4,
  publicV4,
  loopbackV6,
  unspecifiedV6,
  linkLocalV6,
  ulaV6,
  publicV6,
  ipv4MappedV6
);

const scheme = fc.constantFrom("https", "http", "ftp");

/** Build a candidate URL for a scheme (host is irrelevant to the classifier). */
function urlFor(s: string): string {
  return `${s}://issuer.example.com/.well-known/openid-configuration`;
}

// ---------------------------------------------------------------------------
// Property 14
// ---------------------------------------------------------------------------

describe("Property 14: SSRF address/scheme classification", () => {
  it("classifyDestination accepts iff HTTPS AND the address is routable-public", () => {
    fc.assert(
      fc.property(scheme, anyAddress, (s, address) => {
        const expectedAccept = s === "https" && !oracleBlocked(address);
        const verdict = classifyDestination(urlFor(s), address);
        expect(verdict.accepted).toBe(expectedAccept);
      }),
      { numRuns: NUM_RUNS }
    );
  });

  it("HTTPS never rescues a blocked address and a public address never rescues a non-HTTPS scheme", () => {
    // Cross-check the two conjuncts independently to guard against a classifier
    // that accidentally ORs the conditions instead of ANDing them.
    fc.assert(
      fc.property(scheme, anyAddress, (s, address) => {
        const blocked = oracleBlocked(address);
        const verdict = classifyDestination(urlFor(s), address);
        if (blocked) {
          // A blocked address must be rejected under EVERY scheme.
          expect(verdict.accepted).toBe(false);
        }
        if (s !== "https") {
          // A non-HTTPS scheme must be rejected for EVERY address.
          expect(verdict.accepted).toBe(false);
        }
      }),
      { numRuns: NUM_RUNS }
    );
  });

  it("classifyAddress agrees with the independent oracle across all buckets", () => {
    // The address half of the predicate on its own (scheme held aside).
    fc.assert(
      fc.property(anyAddress, (address) => {
        expect(classifyAddress(address).accepted).toBe(!oracleBlocked(address));
      }),
      { numRuns: NUM_RUNS }
    );
  });

  it("applies the same classifier to every redirect hop (per-hop accept iff HTTPS + public)", () => {
    // A redirect chain is modeled as a sequence of (scheme, address) hops. The
    // fetch may proceed only while EVERY hop so far is accepted, so the chain is
    // accepted iff every individual hop is accepted under the same predicate.
    const hop = fc.record({ scheme, address: anyAddress });
    fc.assert(
      fc.property(fc.array(hop, { minLength: 1, maxLength: 6 }), (hops) => {
        const perHop = hops.map(
          (h) => classifyDestination(urlFor(h.scheme), h.address).accepted
        );
        const expectedPerHop = hops.map(
          (h) => h.scheme === "https" && !oracleBlocked(h.address)
        );
        // Each hop is classified independently and identically...
        expect(perHop).toEqual(expectedPerHop);
        // ...and the whole chain is accepted iff no hop is blocked.
        const chainAccepted = perHop.every(Boolean);
        expect(chainAccepted).toBe(expectedPerHop.every(Boolean));
      }),
      { numRuns: NUM_RUNS }
    );
  });
});
