/**
 * `toProxySrc` must FAIL CLOSED.
 *
 * The reported symptom was "the document link is wrong and the render ends up rendering the
 * harness UI". `toProxy` in DocumentViewer tried three rewrites (already-proxied,
 * cloudfront.net, bare `artifacts/` key) and then, for anything else, did `return url`
 * unchanged. Hand it a relative or same-origin path and the `<iframe src>` resolves it against
 * the SPA's own origin, CloudFront serves `index.html` for the unmatched path, and the harness
 * renders itself inside its own document viewer. It looks like a broken document; it is
 * actually a silent passthrough.
 *
 * So the contract is: return a src ONLY when we can positively vouch for it. Everything else
 * returns null and the caller shows an explicit error rather than an iframe pointed at
 * ourselves. Note what "vouch for" excludes: the SPA's own origin. A genuinely external
 * absolute URL is fine — it cannot resolve to the harness.
 */
import { describe, it, expect } from 'vitest';
import { toProxySrc } from './artifactSrc';

const SPA = 'https://harness.example.net';

describe('toProxySrc — positive rewrites', () => {
  it('passes through an already-proxied path', () => {
    expect(toProxySrc('/api/artifact?key=artifacts/a/b.pdf', SPA))
      .toBe('/api/artifact?key=artifacts/a/b.pdf');
  });

  it('rewrites a CloudFront artifact url to the same-origin proxy', () => {
    expect(toProxySrc('https://d123.cloudfront.net/artifacts/s1/output/doc.pdf', SPA))
      .toBe(`/api/artifact?key=${encodeURIComponent('artifacts/s1/output/doc.pdf')}`);
  });

  it('rewrites a bare artifacts/ key', () => {
    expect(toProxySrc('artifacts/s1/output/doc.pdf', SPA))
      .toBe(`/api/artifact?key=${encodeURIComponent('artifacts/s1/output/doc.pdf')}`);
  });

  it('allows a genuinely external absolute url', () => {
    // Cannot resolve to the harness, so it is not the hazard.
    expect(toProxySrc('https://cdn.vendor.example/report.pdf', SPA))
      .toBe('https://cdn.vendor.example/report.pdf');
  });
});

describe('toProxySrc — fails closed', () => {
  it('refuses a bare relative filename', () => {
    // THE BUG: this resolved to https://harness.example.net/report.pdf -> SPA fallback.
    expect(toProxySrc('report.pdf', SPA)).toBeNull();
  });

  it('refuses a root-relative path that is not the artifact proxy', () => {
    expect(toProxySrc('/output/doc.pdf', SPA)).toBeNull();
  });

  it('refuses an absolute url on the SPA origin', () => {
    expect(toProxySrc(`${SPA}/artifacts/s1/doc.pdf`, SPA)).toBeNull();
  });

  it('refuses an empty or missing url', () => {
    expect(toProxySrc('', SPA)).toBeNull();
    expect(toProxySrc(undefined as unknown as string, SPA)).toBeNull();
  });

  it('refuses a non-http scheme', () => {
    // `javascript:` in an iframe src is an XSS vector; `s3://` the browser cannot fetch.
    expect(toProxySrc('javascript:alert(1)', SPA)).toBeNull();
    expect(toProxySrc('s3://bucket/artifacts/s1/doc.pdf', SPA)).toBeNull();
  });

  it('refuses a protocol-relative url', () => {
    // `//evil.example/x.pdf` is absolute-ish but inherits our scheme and is easy to miss.
    expect(toProxySrc('//evil.example/x.pdf', SPA)).toBeNull();
  });
});

describe('toProxySrc — refuses a reference that cannot name one object', () => {
  // Session 865950fd, render/2c02a31d…json: the agent wrote two links into one previewUrl. Proxied,
  // the key failed the artifact-key policy and the viewer framed the raw 400 JSON
  // `{"error":"artifact key is not readable by this caller"}` as if it were the document.
  const CF = 'https://dexamplecdn002.cloudfront.net/artifacts/865950fd/output';

  it('refuses two URLs joined by a literal backslash-n', () => {
    expect(toProxySrc(`${CF}/compliance_report.json\\nhttps://dexamplecdn002.cloudfront.net/artifacts/865950fd/output/compliance_report.pdf`, SPA)).toBeNull();
  });

  it('refuses two URLs joined by a real newline or CRLF', () => {
    expect(toProxySrc(`${CF}/a.json\n${CF}/a.pdf`, SPA)).toBeNull();
    expect(toProxySrc(`${CF}/a.json\r\n${CF}/a.pdf`, SPA)).toBeNull();
  });

  it('refuses a percent-encoded newline or backslash in the key', () => {
    expect(toProxySrc(`${CF}/a.json%0Ab.pdf`, SPA)).toBeNull();
    expect(toProxySrc(`${CF}/a%5Cb.pdf`, SPA)).toBeNull();
  });

  it('refuses a backslash in a bare key or a proxy path', () => {
    expect(toProxySrc('artifacts/s1\\doc.pdf', SPA)).toBeNull();
    expect(toProxySrc('/api/artifact?key=artifacts/s1\\doc.pdf', SPA)).toBeNull();
  });

  it('refuses a second http(s):// anywhere after the start', () => {
    expect(toProxySrc(`${CF}/a.pdfhttps://elsewhere.example/b.pdf`, SPA)).toBeNull();
    expect(toProxySrc('artifacts/s1/a.pdf http://x.example/b.pdf', SPA)).toBeNull();
    expect(toProxySrc('/api/artifact?key=artifacts/s1/a.pdf&next=https://x.example/', SPA)).toBeNull();
  });

  it('still accepts the well-formed half on its own (surrounding whitespace is trimmed)', () => {
    expect(toProxySrc(`  ${CF}/compliance_report.pdf\n`, SPA))
      .toBe(`/api/artifact?key=${encodeURIComponent('artifacts/865950fd/output/compliance_report.pdf')}`);
  });
});

describe('toProxySrc — projects-session output keys (asset-lineage Phase 3 (a))', () => {
  const KEY = `projects/p${'8'.repeat(32)}/sessions/4f9d0d02-1111-4222-8333-444455556666/output/report.pdf`;

  it('proxies a bare session-output key; a bare project ASSET key is refused', () => {
    expect(toProxySrc(KEY, SPA)).toBe(`/api/artifact?key=${encodeURIComponent(KEY)}`);
    expect(toProxySrc(`projects/p${'8'.repeat(32)}/document:x/report.pdf`, SPA)).toBeNull();
  });
});
