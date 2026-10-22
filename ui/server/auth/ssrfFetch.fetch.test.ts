// Feature: custom-agents, Task 11.3 — example/integration tests for the
// SSRF-hardened fetch orchestration (Req 12.3 redirect cap, Req 12.4 egress
// allowlist). These drive `hardenedFetch` with injected `deps` seams so no
// real network I/O occurs; `resolveHostname` and `performRequest` are stubbed.
//
// Validates: Requirements 12.3, 12.4

import { describe, it, expect } from "vitest";

import {
  hardenedFetch,
  makePinnedLookup,
  SsrfError,
  DEFAULT_MAX_REDIRECTS,
  type PinnedRequestArgs,
  type RawResponse,
} from "./ssrfFetch";

// A routable, public IPv4 the classifier accepts.
const PUBLIC_IP = "93.184.216.34";
// A private RFC1918 address the classifier must reject on any hop.
const PRIVATE_IP = "10.0.0.5";

/** Resolve every hostname to a single public IP (no real DNS). */
const resolvePublic = async (_hostname: string): Promise<string[]> => [
  PUBLIC_IP,
];

/** Build a `performRequest` stub that always emits a redirect to `location`. */
function alwaysRedirect(location: string) {
  const calls: PinnedRequestArgs[] = [];
  const performRequest = async (
    args: PinnedRequestArgs
  ): Promise<RawResponse> => {
    calls.push(args);
    return {
      status: 302,
      headers: { location },
      body: Buffer.alloc(0),
    };
  };
  return { performRequest, calls };
}

/** Build a `performRequest` stub that returns a terminal 200 with a body. */
function ok(body = "ok") {
  const calls: PinnedRequestArgs[] = [];
  const performRequest = async (
    args: PinnedRequestArgs
  ): Promise<RawResponse> => {
    calls.push(args);
    return {
      status: 200,
      headers: { "content-type": "text/plain" },
      body: Buffer.from(body),
    };
  };
  return { performRequest, calls };
}

// ---------------------------------------------------------------------------
// Req 12.3 — redirect-count cap
// ---------------------------------------------------------------------------

describe("hardenedFetch — redirect cap (Req 12.3)", () => {
  it("rejects with too_many_redirects once maxRedirects is exceeded", async () => {
    // Every hop returns a 302 pointing at another public HTTPS host, so the
    // only thing that can ever stop the loop is the redirect cap.
    const { performRequest, calls } = alwaysRedirect(
      "https://next.example.com/hop"
    );

    const promise = hardenedFetch("https://start.example.com/", {
      maxRedirects: 2,
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    await expect(promise).rejects.toBeInstanceOf(SsrfError);
    await expect(promise).rejects.toMatchObject({
      reason: "too_many_redirects",
    });

    // With maxRedirects=2 the initial request plus two followed redirects run,
    // and the third redirect trips the cap: exactly 3 requests are performed.
    expect(calls.length).toBe(3);
  });

  it("uses DEFAULT_MAX_REDIRECTS when no cap is supplied", async () => {
    const { performRequest, calls } = alwaysRedirect(
      "https://next.example.com/hop"
    );

    const promise = hardenedFetch("https://start.example.com/", {
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    await expect(promise).rejects.toMatchObject({
      reason: "too_many_redirects",
    });
    // initial request + DEFAULT_MAX_REDIRECTS followed hops before the cap trips.
    expect(calls.length).toBe(DEFAULT_MAX_REDIRECTS + 1);
  });

  it("follows redirects up to the cap and returns the final response", async () => {
    // One redirect, then a terminal 200. With maxRedirects=1 this succeeds.
    let hop = 0;
    const calls: PinnedRequestArgs[] = [];
    const performRequest = async (
      args: PinnedRequestArgs
    ): Promise<RawResponse> => {
      calls.push(args);
      hop += 1;
      if (hop === 1) {
        return {
          status: 302,
          headers: { location: "https://final.example.com/done" },
          body: Buffer.alloc(0),
        };
      }
      return { status: 200, headers: {}, body: Buffer.from("done") };
    };

    const res = await hardenedFetch("https://start.example.com/", {
      maxRedirects: 1,
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    expect(res.status).toBe(200);
    expect(res.body.toString()).toBe("done");
    expect(res.url).toBe("https://final.example.com/done");
    expect(calls.length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// Req 12.3 (strengthener) — per-hop re-classification of the redirect target
// ---------------------------------------------------------------------------

describe("hardenedFetch — per-hop re-classification (Req 12.3)", () => {
  it("rejects when a redirect target resolves to a private address", async () => {
    // The start host is public; the redirect target host resolves to a private
    // RFC1918 address and must be blocked on the hop (classifier re-applied).
    const { performRequest } = alwaysRedirect("https://internal.example.com/x");

    const resolveHostname = async (hostname: string): Promise<string[]> =>
      hostname === "internal.example.com" ? [PRIVATE_IP] : [PUBLIC_IP];

    const promise = hardenedFetch("https://start.example.com/", {
      maxRedirects: 3,
      deps: { resolveHostname, performRequest },
    });

    await expect(promise).rejects.toBeInstanceOf(SsrfError);
    await expect(promise).rejects.toMatchObject({ reason: "private" });
  });
});

// ---------------------------------------------------------------------------
// Req 12.4 — egress allowlist
// ---------------------------------------------------------------------------

describe("hardenedFetch — egress allowlist (Req 12.4)", () => {
  it("blocks a host that is not on the allowlist", async () => {
    const { performRequest, calls } = ok();

    const promise = hardenedFetch("https://evil.example.com/token", {
      allowedHosts: ["idp.example.com"],
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    await expect(promise).rejects.toBeInstanceOf(SsrfError);
    await expect(promise).rejects.toMatchObject({
      reason: "egress_not_allowed",
    });
    // The egress check happens before any DNS resolve / socket, so no request
    // is ever performed.
    expect(calls.length).toBe(0);
  });

  it("allows a host that is on the allowlist (reaches performRequest)", async () => {
    const { performRequest, calls } = ok("jwks");

    const res = await hardenedFetch("https://idp.example.com/jwks", {
      allowedHosts: ["idp.example.com"],
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    expect(res.status).toBe(200);
    expect(res.body.toString()).toBe("jwks");
    expect(calls.length).toBe(1);
    // The pinned request targets the validated public address.
    expect(calls[0].address).toBe(PUBLIC_IP);
    expect(calls[0].url.hostname).toBe("idp.example.com");
  });

  it("matches allowlist entries case-insensitively", async () => {
    const { performRequest, calls } = ok();

    const res = await hardenedFetch("https://IdP.Example.com/jwks", {
      allowedHosts: ["idp.example.com"],
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    expect(res.status).toBe(200);
    expect(calls.length).toBe(1);
  });

  it("re-applies the allowlist to a redirect target off the list", async () => {
    // First hop is an allowed host that redirects to a non-allowed host; the
    // allowlist must reject the redirect target even though it is public.
    const { performRequest } = alwaysRedirect("https://elsewhere.example.com/x");

    const promise = hardenedFetch("https://idp.example.com/authorize", {
      allowedHosts: ["idp.example.com"],
      maxRedirects: 3,
      deps: { resolveHostname: resolvePublic, performRequest },
    });

    await expect(promise).rejects.toMatchObject({
      reason: "egress_not_allowed",
    });
  });
});

// ---------------------------------------------------------------------------
// Regression — the address-pinning `lookup` callback contract (BUG-1).
//
// The default (non-injected) `performRequest` pins the socket via a custom
// `lookup`. Node's HTTPS agent calls that lookup with `{ all: true }`, which
// mandates the ARRAY callback form; the earlier 3-arg scalar form made Node
// throw "Invalid IP address: undefined", breaking EVERY external OIDC JWKS
// fetch (→ 401 jwks_unavailable) regardless of issuer. The `deps.performRequest`
// mocks used by every other test bypass this shim entirely, so nothing covered
// it. This drives the real shim's callback contract directly.
// ---------------------------------------------------------------------------

describe("makePinnedLookup — address-pinning callback contract (BUG-1)", () => {
  // Invoke the (type-erased) lookup with a given options object and capture
  // exactly what it passes to Node's callback.
  function invoke(
    lookup: unknown,
    options: unknown,
  ): { err: unknown; addr: unknown; fam: unknown } {
    let captured: { err: unknown; addr: unknown; fam: unknown } | null = null;
    (lookup as (h: string, o: unknown, cb: (...a: unknown[]) => void) => void)(
      "idp.example.com",
      options,
      (err: unknown, addr: unknown, fam: unknown) => {
        captured = { err, addr, fam };
      },
    );
    if (!captured) throw new Error("lookup did not invoke its callback");
    return captured;
  }

  it("returns the ARRAY form when Node passes { all: true } (the bug fix)", () => {
    const lookup = makePinnedLookup(PUBLIC_IP, 4);
    const { err, addr } = invoke(lookup, { all: true, hints: 1024 });

    expect(err).toBeNull();
    // Must be [{ address, family }] — NOT a bare string (which becomes
    // `undefined` under `all` and throws "Invalid IP address: undefined").
    expect(Array.isArray(addr)).toBe(true);
    expect(addr).toEqual([{ address: PUBLIC_IP, family: 4 }]);
  });

  it("returns the scalar form when no { all } flag is set", () => {
    const lookup = makePinnedLookup(PUBLIC_IP, 4);
    const { err, addr, fam } = invoke(lookup, {});

    expect(err).toBeNull();
    expect(addr).toBe(PUBLIC_IP);
    expect(fam).toBe(4);
  });

  it("infers family from the address when family is 0/unknown", () => {
    const v6 = "2600:1f14:917:5700:3c35:4765:bfe1:2884";
    const { addr } = invoke(makePinnedLookup(v6, 0), { all: true });
    expect(addr).toEqual([{ address: v6, family: 6 }]);
  });
});
