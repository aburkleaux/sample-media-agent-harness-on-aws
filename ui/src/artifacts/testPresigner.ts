/**
 * Test helper: a fake `authFetch` that answers `POST /api/artifact-urls` the way the proxy route
 * does (contract: `{urls: {key: {url, expiresAt}}, errors: {key: {code}}}`), and records each call.
 * Not imported by production code.
 */

export interface PresignCall {
  keys: string[];
  download: boolean;
}

export interface FakePresigner {
  /** Pass as the `authFetch` mock implementation. */
  handle: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  calls: PresignCall[];
  /** Keys answered with `errors[key] = {code: 'not_found'}`. */
  notFound: Set<string>;
  /** Bump to change every URL minted from now on (simulates a fresh signature). */
  generation: number;
  /** When set, every request answers this HTTP status with no body. */
  failStatus: number | null;
  /** When true, every request rejects (network error). */
  reject: boolean;
  ttlSeconds: number;
  urlFor: (key: string, download?: boolean, generation?: number) => string;
}

export function makeFakePresigner(): FakePresigner {
  const p: FakePresigner = {
    calls: [],
    notFound: new Set(),
    generation: 1,
    failStatus: null,
    reject: false,
    ttlSeconds: 3600,
    urlFor: (key, download = false, generation = p.generation) =>
      `https://skills-bucket.s3.us-west-2.amazonaws.com/${key}?X-Amz-Signature=sig${generation}${download ? '&response-content-disposition=attachment' : ''}&X-Amz-Credential=c`,
    handle: async (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      if (url !== '/api/artifact-urls') {
        return new Response('unexpected', { status: 599 });
      }
      const body = JSON.parse(String(init?.body ?? '{}')) as { keys: string[]; download?: boolean };
      p.calls.push({ keys: body.keys, download: body.download === true });
      if (p.reject) throw new TypeError('Failed to fetch');
      if (p.failStatus !== null) return new Response('', { status: p.failStatus });
      const urls: Record<string, { url: string; expiresAt: number }> = {};
      const errors: Record<string, { code: string }> = {};
      const expiresAt = Math.floor(Date.now() / 1000) + p.ttlSeconds;
      for (const k of body.keys) {
        if (p.notFound.has(k)) errors[k] = { code: 'not_found' };
        else urls[k] = { url: p.urlFor(k, body.download === true), expiresAt };
      }
      return new Response(JSON.stringify({ urls, errors }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    },
  };
  return p;
}
