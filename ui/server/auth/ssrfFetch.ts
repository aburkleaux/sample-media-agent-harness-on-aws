/**
 * SSRF-hardened fetch wrapper for issuer discovery / JWKS fetching (Req 12).
 *
 * Runs in Node (the Express proxy / Vite dev proxy), NOT in the browser — it
 * depends on the Node built-in `https`, `dns`, and `net` modules. This module
 * is imported by `ui/server/auth/agentOidc.ts` (Component 4) so that EVERY
 * OIDC discovery and JWKS fetch goes through a single hardened path.
 *
 * Threat model: the issuer URL is Owner-supplied. Without hardening, an Owner
 * could point the issuer at `http://169.254.169.254/…` (cloud metadata),
 * `https://127.0.0.1/…` (loopback), or an RFC1918 host, and coerce the proxy
 * into server-side request forgery. DNS rebinding and redirect-to-internal are
 * additional vectors, so the classifier is re-applied to every redirect hop and
 * connections are pinned to the validated resolved address.
 *
 * Design (mirrors design.md §Req 12 and Property 14):
 *   - HTTPS only. Non-HTTPS schemes are rejected before any socket is opened.
 *   - DNS resolve + classify. Every resolved address is classified; if ANY is
 *     loopback / unspecified / link-local / cloud-metadata / RFC1918-private /
 *     CGNAT-shared / IPv6-ULA, the request is rejected.
 *   - Pin to the validated address. The connection is pinned (via a custom
 *     `lookup`) to a classified-accepted address so a late-binding re-resolve
 *     cannot rebind to an internal address (TOCTOU defense).
 *   - Capped, re-validated redirects. Redirects are followed only up to a small
 *     cap, and each hop's target is re-parsed, re-resolved, and re-classified
 *     before the next connection proceeds.
 *
 * The pure classifier (`classifyAddress` / `classifyDestination`) is exported
 * separately so Property 14 can call it directly with generated addresses, and
 * the fetch orchestration accepts injected DNS/transport behavior so the
 * redirect-cap and egress-allowlist example tests can drive it deterministically
 * without real network I/O.
 */

import net from "net";
import https from "https";
import { lookup as dnsLookup } from "dns/promises";
import { URL } from "url";

/** Default maximum number of redirects followed before rejecting. */
export const DEFAULT_MAX_REDIRECTS = 3;

/**
 * Reason an address/scheme was rejected. The categories map onto the OIDC
 * verifier's failure taxonomy (issuer/`jwks_unavailable`) in `agentOidc.ts`.
 */
export type SsrfRejectionReason =
  | "not_https"
  | "invalid_url"
  | "invalid_address"
  | "dns_failure"
  | "loopback"
  | "unspecified"
  | "link_local"
  | "cloud_metadata"
  | "private"
  | "unique_local"
  | "too_many_redirects"
  | "egress_not_allowed";

/** Result of the pure address/scheme classifier. */
export type ClassificationResult =
  | { accepted: true }
  | { accepted: false; reason: SsrfRejectionReason };

const ACCEPTED: ClassificationResult = { accepted: true };

function rejected(reason: SsrfRejectionReason): ClassificationResult {
  return { accepted: false, reason };
}

// ---------------------------------------------------------------------------
// Pure classifier (exported for Property 14)
// ---------------------------------------------------------------------------

/**
 * Classify a single literal IPv4 address. Returns a rejection reason for any
 * loopback / unspecified / link-local / cloud-metadata / RFC1918-private /
 * CGNAT-shared / broadcast address, or `null` when the address is a routable
 * public destination.
 */
function classifyIpv4(address: string): SsrfRejectionReason | null {
  const parts = address.split(".");
  if (parts.length !== 4) return "invalid_address";
  const octets = parts.map((p) => Number(p));
  if (octets.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) {
    return "invalid_address";
  }
  const [a, b] = octets;

  // Cloud metadata endpoint (subset of link-local, flagged distinctly).
  if (address === "169.254.169.254") return "cloud_metadata";
  // 0.0.0.0/8 — "this host" / unspecified.
  if (a === 0) return "unspecified";
  // 127.0.0.0/8 — loopback.
  if (a === 127) return "loopback";
  // 169.254.0.0/16 — link-local.
  if (a === 169 && b === 254) return "link_local";
  // RFC1918 private ranges.
  if (a === 10) return "private";
  if (a === 172 && b >= 16 && b <= 31) return "private";
  if (a === 192 && b === 168) return "private";
  // 100.64.0.0/10 — RFC6598 shared/CGNAT address space.
  if (a === 100 && b >= 64 && b <= 127) return "private";
  // 255.255.255.255 — limited broadcast.
  if (a === 255 && b === 255 && octets[2] === 255 && octets[3] === 255) {
    return "private";
  }

  return null;
}

/**
 * Expand an IPv6 address string (including `::` compression and embedded
 * IPv4 forms such as `::ffff:1.2.3.4`) into its eight 16-bit groups. Returns
 * `null` if the string is not a well-formed IPv6 address.
 */
function ipv6ToGroups(address: string): number[] | null {
  let s = address.trim().toLowerCase();

  // Strip an RFC 4007 zone id (e.g. `fe80::1%eth0`).
  const zoneIdx = s.indexOf("%");
  if (zoneIdx !== -1) s = s.slice(0, zoneIdx);

  // Fold a trailing embedded IPv4 (e.g. `::ffff:1.2.3.4`) into two hextets.
  const lastColon = s.lastIndexOf(":");
  if (lastColon !== -1 && s.slice(lastColon + 1).includes(".")) {
    const dotted = s.slice(lastColon + 1);
    const oct = dotted.split(".");
    if (oct.length !== 4) return null;
    const nums = oct.map((o) => Number(o));
    if (nums.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return null;
    const g1 = ((nums[0] << 8) | nums[1]).toString(16);
    const g2 = ((nums[2] << 8) | nums[3]).toString(16);
    s = s.slice(0, lastColon + 1) + g1 + ":" + g2;
  }

  const parseGroups = (str: string): number[] | null => {
    if (str === "") return [];
    const out: number[] = [];
    for (const g of str.split(":")) {
      if (g === "" || g.length > 4 || !/^[0-9a-f]+$/.test(g)) return null;
      out.push(parseInt(g, 16));
    }
    return out;
  };

  const halves = s.split("::");
  if (halves.length > 2) return null;

  if (halves.length === 2) {
    const head = parseGroups(halves[0]);
    const tail = parseGroups(halves[1]);
    if (head === null || tail === null) return null;
    const missing = 8 - head.length - tail.length;
    if (missing < 1) return null; // `::` must stand in for at least one group
    return [...head, ...new Array<number>(missing).fill(0), ...tail];
  }

  const groups = parseGroups(s);
  if (groups === null || groups.length !== 8) return null;
  return groups;
}

/**
 * Classify eight 16-bit IPv6 groups. IPv4-mapped addresses
 * (`::ffff:a.b.c.d`) are unwrapped and re-classified as IPv4 so that, e.g.,
 * `::ffff:169.254.169.254` is still caught as cloud-metadata.
 */
function classifyIpv6Groups(groups: number[]): SsrfRejectionReason | null {
  // IPv4-mapped ::ffff:0:0/96 → unwrap embedded IPv4 and re-classify.
  if (
    groups[0] === 0 &&
    groups[1] === 0 &&
    groups[2] === 0 &&
    groups[3] === 0 &&
    groups[4] === 0 &&
    groups[5] === 0xffff
  ) {
    const v4 = `${groups[6] >> 8}.${groups[6] & 0xff}.${groups[7] >> 8}.${
      groups[7] & 0xff
    }`;
    return classifyIpv4(v4);
  }

  const allZero = groups.every((g) => g === 0);
  if (allZero) return "unspecified"; // ::
  // ::1 — loopback.
  if (
    groups.slice(0, 7).every((g) => g === 0) &&
    groups[7] === 1
  ) {
    return "loopback";
  }
  // fe80::/10 — link-local.
  if ((groups[0] & 0xffc0) === 0xfe80) return "link_local";
  // fc00::/7 — unique local address (ULA / private).
  if ((groups[0] & 0xfe00) === 0xfc00) return "unique_local";

  return null;
}

/**
 * Pure classifier for a resolved IP address (IPv4 or IPv6, including
 * IPv4-mapped IPv6). Exported so Property 14 can exercise it directly with
 * generated addresses. Accepts iff the address is a routable public
 * destination.
 */
export function classifyAddress(address: string): ClassificationResult {
  const kind = net.isIP(address);
  if (kind === 4) {
    const reason = classifyIpv4(address);
    return reason ? rejected(reason) : ACCEPTED;
  }
  if (kind === 6) {
    const groups = ipv6ToGroups(address);
    if (groups === null) return rejected("invalid_address");
    const reason = classifyIpv6Groups(groups);
    return reason ? rejected(reason) : ACCEPTED;
  }
  return rejected("invalid_address");
}

/**
 * Pure classifier over a (URL, resolved address) pair — the exact predicate in
 * Property 14. Accepts iff the URL scheme is HTTPS AND the resolved address is
 * a routable public destination (not loopback / link-local / cloud-metadata /
 * RFC1918-private / ULA). Exported separately for direct property testing.
 */
export function classifyDestination(
  url: string | URL,
  resolvedAddress: string
): ClassificationResult {
  let parsed: URL;
  try {
    parsed = typeof url === "string" ? new URL(url) : url;
  } catch {
    return rejected("invalid_url");
  }
  if (parsed.protocol !== "https:") return rejected("not_https");
  return classifyAddress(resolvedAddress);
}

// ---------------------------------------------------------------------------
// Hardened fetch orchestration (injectable DNS / transport for tests)
// ---------------------------------------------------------------------------

/** Error thrown when a fetch is rejected by the SSRF guard. */
export class SsrfError extends Error {
  readonly reason: SsrfRejectionReason;
  readonly url: string;

  constructor(reason: SsrfRejectionReason, url: string, message?: string) {
    super(message ?? `SSRF guard rejected fetch (${reason}): ${url}`);
    this.name = "SsrfError";
    this.reason = reason;
    this.url = url;
  }
}

/** Minimal response shape returned by a single (non-redirect) request. */
export interface RawResponse {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: Buffer;
}

/** Final response returned by {@link hardenedFetch}. */
export interface HardenedResponse extends RawResponse {
  /** The final URL after any redirects. */
  url: string;
}

/** Injectable seams so the orchestration is testable without network I/O. */
export interface HardenedFetchDeps {
  /**
   * Resolve a hostname to a list of IP address strings. Defaults to
   * `dns.lookup(hostname, { all: true })`. IP literals resolve to themselves.
   */
  resolveHostname?: (hostname: string) => Promise<string[]>;
  /**
   * Perform a single request pinned to the already-validated `address`,
   * WITHOUT following redirects. Defaults to a pinned `https.request`.
   */
  performRequest?: (args: PinnedRequestArgs) => Promise<RawResponse>;
}

export interface PinnedRequestArgs {
  url: URL;
  address: string;
  family: number;
  method: string;
  headers: Record<string, string>;
}

export interface HardenedFetchOptions {
  method?: string;
  headers?: Record<string, string>;
  /** Maximum redirects to follow (default {@link DEFAULT_MAX_REDIRECTS}). */
  maxRedirects?: number;
  /**
   * Optional egress allowlist of hostnames. When provided, every hop's host
   * (including redirect targets) must be a member or the fetch is rejected.
   * This is the in-code half of "restrict egress to validated destinations
   * only"; classification + address pinning is the other half.
   */
  allowedHosts?: readonly string[];
  /** Injected DNS/transport behavior (tests). */
  deps?: HardenedFetchDeps;
}

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

async function defaultResolveHostname(hostname: string): Promise<string[]> {
  if (net.isIP(hostname)) return [hostname];
  const results = await dnsLookup(hostname, { all: true, verbatim: true });
  return results.map((r) => r.address);
}

/**
 * Build the custom `lookup` that pins a socket to an already-classified
 * address (TOCTOU/DNS-rebinding defense), returning it in whichever callback
 * shape Node asked for.
 *
 * Node's HTTPS agent invokes `lookup` with `{ all: true }`, which requires the
 * ARRAY callback form `cb(null, [{ address, family }])`. The 3-arg scalar form
 * `cb(null, address, family)` makes Node read the address as `undefined` and
 * throw "Invalid IP address: undefined" before the socket ever connects — so
 * the shim MUST honor `options.all`. Exported so the regression test can drive
 * the real callback contract directly (the network I/O around it is otherwise
 * only reachable via a live TLS socket).
 */
export function makePinnedLookup(
  address: string,
  family: number,
): https.RequestOptions["lookup"] {
  const fam = family || (net.isIP(address) as number);
  return ((_hostname: string, options: unknown, cb: (
    err: NodeJS.ErrnoException | null,
    addr: string | { address: string; family: number }[],
    fam?: number,
  ) => void) => {
    if (options && (options as { all?: boolean }).all) {
      cb(null, [{ address, family: fam }]);
    } else {
      cb(null, address, fam);
    }
  }) as unknown as https.RequestOptions["lookup"];
}

function defaultPerformRequest(args: PinnedRequestArgs): Promise<RawResponse> {
  const { url, address, family, method, headers } = args;
  return new Promise<RawResponse>((resolve, reject) => {
    const req = https.request(
      {
        protocol: "https:",
        hostname: url.hostname,
        port: url.port ? Number(url.port) : 443,
        path: `${url.pathname}${url.search}`,
        method,
        headers: { host: url.host, ...headers },
        // Preserve SNI / certificate validation against the real hostname...
        servername: url.hostname,
        // ...while pinning the socket to the address we already classified,
        // so no re-resolution can rebind to an internal address (TOCTOU).
        lookup: makePinnedLookup(address, family),
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () => {
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            body: Buffer.concat(chunks),
          });
        });
      }
    );
    req.on("error", reject);
    req.end();
  });
}

function firstHeader(
  value: string | string[] | undefined
): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

/**
 * Fetch a URL with SSRF hardening: HTTPS-only, DNS-resolve + classify every
 * resolved address (rejecting if any is blocked), pin the connection to the
 * validated address, and follow at most `maxRedirects` redirects — re-running
 * the full scheme+address classifier on every hop.
 *
 * Throws {@link SsrfError} when the scheme, an address, the redirect count, or
 * the egress allowlist rejects the request.
 */
export async function hardenedFetch(
  inputUrl: string,
  options: HardenedFetchOptions = {}
): Promise<HardenedResponse> {
  const maxRedirects = options.maxRedirects ?? DEFAULT_MAX_REDIRECTS;
  const resolveHostname =
    options.deps?.resolveHostname ?? defaultResolveHostname;
  const performRequest = options.deps?.performRequest ?? defaultPerformRequest;
  const method = options.method ?? "GET";
  const allowedHosts = options.allowedHosts
    ? new Set(options.allowedHosts.map((h) => h.toLowerCase()))
    : null;

  let currentUrl: URL;
  try {
    currentUrl = new URL(inputUrl);
  } catch {
    throw new SsrfError("invalid_url", inputUrl);
  }

  let redirects = 0;

  // Loop over hops. Each iteration re-applies the full guard to the current
  // (possibly redirected) URL before any connection is opened.
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const target = currentUrl.toString();

    // 1. Scheme: HTTPS only, checked before any socket is opened.
    if (currentUrl.protocol !== "https:") {
      throw new SsrfError("not_https", target);
    }

    // 2. Egress allowlist (optional in-code destination check).
    if (allowedHosts && !allowedHosts.has(currentUrl.hostname.toLowerCase())) {
      throw new SsrfError("egress_not_allowed", target);
    }

    // 3. DNS resolve and classify EVERY resolved address; reject if any is
    //    blocked so a multi-record hostname cannot slip an internal address in.
    let addresses: string[];
    try {
      addresses = await resolveHostname(currentUrl.hostname);
    } catch {
      throw new SsrfError("dns_failure", target);
    }
    if (addresses.length === 0) {
      throw new SsrfError("dns_failure", target);
    }
    for (const addr of addresses) {
      const verdict = classifyAddress(addr);
      if (!verdict.accepted) {
        throw new SsrfError(verdict.reason, target);
      }
    }

    // 4. Pin to a validated address to defeat DNS rebinding (TOCTOU).
    const pinned = addresses[0];
    const family = net.isIP(pinned);

    const response = await performRequest({
      url: currentUrl,
      address: pinned,
      family,
      method,
      headers: options.headers ?? {},
    });

    // 5. Redirect handling: cap the count and re-validate the next hop.
    const location = firstHeader(response.headers["location"]);
    if (REDIRECT_STATUSES.has(response.status) && location) {
      if (redirects >= maxRedirects) {
        throw new SsrfError("too_many_redirects", target);
      }
      redirects += 1;
      try {
        currentUrl = new URL(location, currentUrl);
      } catch {
        throw new SsrfError("invalid_url", location);
      }
      continue; // re-enter the loop → scheme + classifier re-applied
    }

    return {
      status: response.status,
      headers: response.headers,
      body: response.body,
      url: target,
    };
  }
}

/**
 * Convenience wrapper for the OIDC verifier: fetch a URL through
 * {@link hardenedFetch} and parse the response body as JSON. Non-2xx
 * responses reject so discovery/JWKS callers can map to `jwks_unavailable`.
 */
export async function hardenedFetchJson<T = unknown>(
  inputUrl: string,
  options: HardenedFetchOptions = {}
): Promise<{ status: number; data: T; url: string }> {
  const headers = { accept: "application/json", ...(options.headers ?? {}) };
  const response = await hardenedFetch(inputUrl, { ...options, headers });
  if (response.status < 200 || response.status >= 300) {
    throw new SsrfError(
      "dns_failure",
      response.url,
      `Non-2xx response (${response.status}) fetching ${response.url}`
    );
  }
  const data = JSON.parse(response.body.toString("utf8")) as T;
  return { status: response.status, data, url: response.url };
}
