/**
 * View-time re-signing of artifact references inside an HTML artifact.
 *
 * Regression for session b42731df: the agent wrote `slowdown_summit_storyboard.html` whose 8
 * `<img src>` values were presigned S3 URLs (`X-Amz-Expires=21600` + a runtime session token),
 * baked in by the code_interpreter input presign. Hours later every panel was broken, and
 * nothing in the SPA could repair it — the browser fetches those nested URLs itself.
 */
import { describe, it, expect } from "vitest";
import {
  artifactKeyFromUrl,
  collectArtifactKeys,
  isRewritableHtml,
  resignHtmlArtifact,
  rewriteArtifactRefsInHtml,
  HTML_REWRITE_MAX_BYTES,
} from "./htmlArtifactRewrite";

const BUCKET = "mediaworks-harness-skills-store-100000000002-us-west-2";
const KEY = "artifacts/external/3b1bfb92bd0f6e9934229bf0.jpg";
// Shortened but structurally identical to what the storyboard actually carried (the credential
// values are placeholders — only the SHAPE matters here).
const EXPIRED = `https://${BUCKET}.s3.amazonaws.com/${KEY}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=EXAMPLEKEYID%2F20261006%2Fus-west-2%2Fs3%2Faws4_request&X-Amz-Date=20261006T135734Z&X-Amz-Expires=21600&X-Amz-SignedHeaders=host&X-Amz-Security-Token=EXAMPLETOKEN&X-Amz-Signature=0000dead`;
const FRESH = `https://${BUCKET}.s3.us-west-2.amazonaws.com/${KEY}?X-Amz-Signature=NEW`;

describe("artifactKeyFromUrl", () => {
  it("extracts the key from an expired presigned URL on our own bucket", () => {
    expect(artifactKeyFromUrl(EXPIRED, BUCKET)).toBe(KEY);
  });

  it("extracts the key from a regional, path-style, and CDN form", () => {
    expect(artifactKeyFromUrl(`https://${BUCKET}.s3.us-west-2.amazonaws.com/${KEY}`, BUCKET)).toBe(KEY);
    expect(artifactKeyFromUrl(`https://s3.us-west-2.amazonaws.com/${BUCKET}/${KEY}`, BUCKET)).toBe(KEY);
    expect(artifactKeyFromUrl(`https://dexamplecdn002.cloudfront.net/${KEY}`, BUCKET)).toBe(KEY);
    expect(artifactKeyFromUrl(`s3://${BUCKET}/${KEY}`, BUCKET)).toBe(KEY);
  });

  it("extracts the key from the proxy read-through form, and de-doubles the prefix", () => {
    expect(artifactKeyFromUrl(`/api/artifact?key=${encodeURIComponent(KEY)}`, BUCKET)).toBe(KEY);
    expect(artifactKeyFromUrl("/api/artifact?key=artifacts/artifacts/a/b.png", BUCKET)).toBe("artifacts/a/b.png");
  });

  it("decodes a percent-encoded key path", () => {
    expect(artifactKeyFromUrl(`https://${BUCKET}.s3.amazonaws.com/artifacts/s1/out/my%20file.png`, BUCKET))
      .toBe("artifacts/s1/out/my file.png");
  });

  it("normalizes &amp; separators from generated markup", () => {
    const entity = `https://${BUCKET}.s3.amazonaws.com/${KEY}?X-Amz-Expires=1&amp;X-Amz-Signature=x`;
    expect(artifactKeyFromUrl(entity, BUCKET)).toBe(KEY);
  });

  it("refuses anything that is not ours to re-sign", () => {
    // another bucket's presigned URL
    expect(artifactKeyFromUrl(`https://other-bucket.s3.amazonaws.com/${KEY}`, BUCKET)).toBeNull();
    expect(artifactKeyFromUrl(`https://s3.us-west-2.amazonaws.com/other-bucket/${KEY}`, BUCKET)).toBeNull();
    expect(artifactKeyFromUrl(`s3://other-bucket/${KEY}`, BUCKET)).toBeNull();
    // third-party hosts
    expect(artifactKeyFromUrl("https://v3.fal.media/files/penguin/abc.png", BUCKET)).toBeNull();
    expect(artifactKeyFromUrl("https://picsum.photos/400", BUCKET)).toBeNull();
    // non-artifact keys, traversal, and non-URLs
    expect(artifactKeyFromUrl(`https://${BUCKET}.s3.amazonaws.com/users/a/secret.txt`, BUCKET)).toBeNull();
    expect(artifactKeyFromUrl(`https://${BUCKET}.s3.amazonaws.com/artifacts/../users/a`, BUCKET)).toBeNull();
    expect(artifactKeyFromUrl("data:image/png;base64,AAAA", BUCKET)).toBeNull();
    expect(artifactKeyFromUrl("./panel1.jpg", BUCKET)).toBeNull();
    expect(artifactKeyFromUrl("", BUCKET)).toBeNull();
  });
});

describe("rewriteArtifactRefsInHtml", () => {
  it("(b42731df) replaces every baked-in presigned panel URL with a fresh one", () => {
    const keys = [
      "artifacts/external/3b1bfb92bd0f6e9934229bf0.jpg",
      "artifacts/external/67f5eb2a9f186cd090d34139.jpg",
      "artifacts/external/65c10aab38968ad82a81499b.jpg",
    ];
    const html = `<!DOCTYPE html><html><body>${keys
      .map((k) => `<img src="https://${BUCKET}.s3.amazonaws.com/${k}?X-Amz-Expires=21600&X-Amz-Signature=old">`)
      .join("")}</body></html>`;

    expect(collectArtifactKeys(html, BUCKET)).toEqual(keys);
    const signed = new Map(keys.map((k) => [k, `https://signed.example/${k}?sig=new`]));
    const out = rewriteArtifactRefsInHtml(html, BUCKET, signed);

    for (const k of keys) expect(out).toContain(`https://signed.example/${k}?sig=new`);
    expect(out).not.toContain("X-Amz-Signature=old");
    expect(out.match(/<img /g)).toHaveLength(3);
  });

  it("re-signs a private-CDN URL, which 403s for every viewer", () => {
    const html = `<img src="https://dexamplecdn002.cloudfront.net/${KEY}">`;
    const out = rewriteArtifactRefsInHtml(html, BUCKET, new Map([[KEY, FRESH]]));
    expect(out).toBe(`<img src="${FRESH}">`);
  });

  it("re-signs /api/artifact refs, which a nested <img> cannot authenticate", () => {
    // The iframe document carries its own view ticket; an <img> inside it inherits nothing, so in
    // gated mode the nested read would 401.
    const html = `<img src="/api/artifact?key=${KEY}">`;
    const out = rewriteArtifactRefsInHtml(html, BUCKET, new Map([[KEY, FRESH]]));
    expect(out).toBe(`<img src="${FRESH}">`);
  });

  it("leaves third-party and inline sources untouched", () => {
    const html = `<img src="https://v3.fal.media/files/x.png"><img src="data:image/png;base64,AA">`
      + `<a href="https://example.com/doc">doc</a>`;
    expect(rewriteArtifactRefsInHtml(html, BUCKET, new Map([[KEY, FRESH]]))).toBe(html);
  });

  it("keeps the original URL for a key whose presign failed", () => {
    const html = `<img src="${EXPIRED}">`;
    expect(rewriteArtifactRefsInHtml(html, BUCKET, new Map([["artifacts/other.jpg", FRESH]]))).toBe(html);
  });

  it("preserves &amp; escaping so the attribute stays valid HTML", () => {
    const html = `<img src="https://${BUCKET}.s3.amazonaws.com/${KEY}?X-Amz-Expires=1&amp;X-Amz-Signature=old">`;
    const out = rewriteArtifactRefsInHtml(html, BUCKET, new Map([[KEY, "https://s/x?a=1&b=2"]]));
    expect(out).toBe(`<img src="https://s/x?a=1&amp;b=2">`);
  });

  it("is total: empty, non-string, and no-signatures inputs pass through", () => {
    expect(rewriteArtifactRefsInHtml("", BUCKET, new Map([[KEY, FRESH]]))).toBe("");
    expect(rewriteArtifactRefsInHtml(`<img src="${EXPIRED}">`, BUCKET, new Map())).toBe(`<img src="${EXPIRED}">`);
    expect(collectArtifactKeys(undefined as unknown as string, BUCKET)).toEqual([]);
  });

  it("rewrites css url(), srcset and poster attributes too (same URL grammar)", () => {
    const html = `<div style="background:url(${EXPIRED})"></div>`
      + `<video poster="https://d1.cloudfront.net/${KEY}"></video>`;
    const out = rewriteArtifactRefsInHtml(html, BUCKET, new Map([[KEY, FRESH]]));
    expect(out).toContain(`url(${FRESH})`);
    expect(out).toContain(`poster="${FRESH}"`);
    expect(out).not.toContain("X-Amz-Signature=0000dead");
  });
});

describe("isRewritableHtml", () => {
  it("accepts html by content-type, or by extension when the type is absent", () => {
    expect(isRewritableHtml("artifacts/s/out/a.html", "text/html; charset=utf-8", 1000)).toBe(true);
    expect(isRewritableHtml("artifacts/s/out/a.html", undefined, 1000)).toBe(true);
    expect(isRewritableHtml("artifacts/s/out/a.htm", "", 1000)).toBe(true);
  });

  it("rejects non-html and anything too large to buffer", () => {
    expect(isRewritableHtml("artifacts/s/out/a.mp4", "video/mp4", 1000)).toBe(false);
    expect(isRewritableHtml("artifacts/s/r/a.json", "application/json", 10)).toBe(false);
    expect(isRewritableHtml("artifacts/s/out/a.html", "text/html", HTML_REWRITE_MAX_BYTES + 1)).toBe(false);
  });
});

describe("resignHtmlArtifact — authorization", () => {
  const MINE = "artifacts/my-session/output/panel.jpg";
  const THEIRS = "artifacts/victim-session/output/private.png";
  const html = `<img src="https://${BUCKET}.s3.amazonaws.com/${MINE}?X-Amz-Expires=1">`
    + `<img src="https://d1.cloudfront.net/${THEIRS}">`;

  it("never mints a URL for a key the viewer may not read (no laundering through a document)", async () => {
    // A user can have the agent write ANY key into an HTML file they own. If the document's own
    // authorization were enough, opening it would hand them a working URL for another session's
    // object. Each embedded key is authorized for the viewer, exactly like a direct read.
    const presigned: string[] = [];
    const out = await resignHtmlArtifact(html, BUCKET, {
      presign: async (k) => { presigned.push(k); return `https://signed/${k}`; },
      authorize: async (k) => k === MINE,
    });
    expect(presigned).toEqual([MINE]);
    expect(out).toContain(`https://signed/${MINE}`);
    expect(out).not.toContain(`https://signed/${THEIRS}`);
    expect(out).toContain(`https://d1.cloudfront.net/${THEIRS}`); // left exactly as authored
  });

  it("is total: an authorize or presign failure leaves that one reference as authored", async () => {
    const warns: string[] = [];
    const out = await resignHtmlArtifact(html, BUCKET, {
      presign: async (k) => { if (k === MINE) throw new Error("throttled"); return `https://signed/${k}`; },
      authorize: async (k) => { if (k === THEIRS) throw new Error("ddb down"); return true; },
      warn: (m) => warns.push(m),
    });
    expect(out).toBe(html);
    expect(warns).toHaveLength(2);
  });

  it("does no I/O for a document with no artifact references", async () => {
    let calls = 0;
    const plain = "<h1>hello</h1><img src=\"https://v3.fal.media/x.png\">";
    const out = await resignHtmlArtifact(plain, BUCKET, {
      presign: async () => { calls++; return ""; },
      authorize: async () => { calls++; return true; },
    });
    expect(out).toBe(plain);
    expect(calls).toBe(0);
  });
});

describe("projects-session output keys (asset-lineage Phase 3 (a))", () => {
  const PID = "p" + "4".repeat(32);
  const SID = "4f9d0d02-1111-4222-8333-444455556666";
  const OUT = `projects/${PID}/sessions/${SID}/output/panel.jpg`;

  it("recognises every link form of a session output, never a project ASSET key", () => {
    expect(artifactKeyFromUrl(`https://${BUCKET}.s3.amazonaws.com/${OUT}?X-Amz-Expires=1`, BUCKET)).toBe(OUT);
    expect(artifactKeyFromUrl(`https://d1.cloudfront.net/${OUT}`, BUCKET)).toBe(OUT);
    expect(artifactKeyFromUrl(`/api/artifact?key=${encodeURIComponent(OUT)}`, BUCKET)).toBe(OUT);
    expect(artifactKeyFromUrl(`s3://${BUCKET}/${OUT}`, BUCKET)).toBe(OUT);
    expect(artifactKeyFromUrl(`https://d1.cloudfront.net/projects/${PID}/video:x/a.jpg`, BUCKET)).toBeNull();
  });

  it("re-signs an authorized session-output link inside a document", async () => {
    const out = await resignHtmlArtifact(`<img src="https://d1.cloudfront.net/${OUT}">`, BUCKET, {
      presign: async (k) => `https://signed/${k}`,
      authorize: async (k) => k === OUT,
    });
    expect(out).toContain(`https://signed/${OUT}`);
  });
});
