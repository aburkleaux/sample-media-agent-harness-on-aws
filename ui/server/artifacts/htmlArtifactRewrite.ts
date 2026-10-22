/**
 * Re-sign artifact references inside an HTML artifact AT VIEW TIME.
 *
 * ── Why this exists ─────────────────────────────────────────────────────────
 * The agent builds deliverables like a comic storyboard as a standalone `.html` file
 * written to `artifacts/<session>/output/*.html`. The `<img src>` values it embeds are
 * whatever the generating code held — and because the artifacts CDN is private, the
 * code_interpreter input presign (`agent/src/tool_input_normalize.py`) has already
 * swapped every CDN artifact URL in that code for a **presigned S3 GET**. So the URLs
 * get BAKED INTO THE SAVED FILE, carrying `X-Amz-Expires=21600` and the runtime's
 * temporary session token.
 *
 * Six hours later (sooner if the runtime's credentials rolled) every image in the saved
 * document 403s. Session b42731df: a storyboard with 8 panels, all 8 broken, nothing in
 * the viewer able to fix it — the file is static and the browser fetches those URLs
 * directly, so the SPA's own re-sign-on-error path never sees them.
 *
 * A presigned URL is a CREDENTIAL, which is the wrong thing to persist in a document.
 * The durable form is an IDENTIFIER (the `artifacts/...` S3 key) resolved to a
 * credential at use time — the same Option B rule the rest of the app follows. This
 * module applies that rule to HTML we SERVE, so it fixes documents already written as
 * well as any future one, whatever got baked in.
 *
 * Rewritten (each → a fresh presigned GET for the same key):
 *   * an EXPIRED-or-not presigned S3 URL on our own artifacts bucket
 *     (`https://<bucket>.s3[.<region>].amazonaws.com/<key>?X-Amz-...`)
 *   * a CloudFront artifacts-CDN URL (`https://<anything>.cloudfront.net/artifacts/...`),
 *     which 403s wholesale once the CDN is private
 *   * a same-origin `/api/artifact?key=artifacts/...` URL — it works for the top-level
 *     document (which carries its own view ticket) but a nested `<img>` inherits none,
 *     so in gated mode it would 401
 *   * a bare `s3://<our-bucket>/<key>` (appears in hand-written HTML)
 *
 * Left alone: third-party hosts (fal, picsum), `data:`/`blob:` URIs, relative paths, and
 * any key outside `artifacts/` (except a projects-session OUTPUT key). Unparseable input is returned verbatim — a rewrite must
 * never be able to corrupt a document.
 */

import { isProjectSessionOutputKey } from "../../src/shared/sessionLayout";

/** Longest HTML we will buffer and rewrite; beyond this the object streams untouched. */
export const HTML_REWRITE_MAX_BYTES = 4 * 1024 * 1024;

/**
 * An `artifacts/`-scoped S3 key, or a projects-session OUTPUT key (asset-lineage Phase 3 (a)). Spaces
 * are legal in a key (they arrive percent-encoded).
 */
function isArtifactKey(key: string): boolean {
  if (/[\r\n\\]/.test(key)) return false;
  if (isProjectSessionOutputKey(key)) return true;
  return (
    key.startsWith("artifacts/") &&
    key.length > "artifacts/".length &&
    !key.includes("..")
  );
}

/**
 * Candidate URL-ish runs inside HTML. Deliberately coarse: any `http(s)`/`s3`/`/api/artifact`
 * run up to the first character that cannot appear in an attribute value. Quotes, whitespace,
 * `<`, `>`, `\` and backticks terminate it; `&` and `?` do not, so a whole presigned query
 * string is captured as one token.
 */
const URL_RUN = /(?:https?:\/\/|s3:\/\/)[^\s"'<>\\`)]+|\/api\/artifact\?[^\s"'<>\\`)]+/g;

const decode = (s: string): string | null => {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
};

/**
 * The `artifacts/...` key a single URL names, or null when it is not one of our artifact
 * references. `bucket` is this environment's artifacts bucket; a presigned URL for any OTHER
 * bucket is left alone (we cannot vouch for it and must not re-sign into our own).
 */
export function artifactKeyFromUrl(raw: string, bucket: string): string | null {
  if (typeof raw !== "string" || !raw) return null;
  // HTML entities are common in generated markup (`&amp;` between query params). Normalize
  // before parsing so a presigned URL written as `?a=1&amp;b=2` still resolves.
  const candidate = raw.replace(/&amp;/gi, "&");
  // Traversal can never be legitimate and must not reach a presigner.
  if (candidate.includes("..")) return null;

  if (candidate.startsWith("s3://")) {
    const rest = candidate.slice("s3://".length);
    const slash = rest.indexOf("/");
    if (slash <= 0) return null;
    if (rest.slice(0, slash) !== bucket) return null;
    const key = rest.slice(slash + 1).split("?")[0];
    return isArtifactKey(key) ? key : null;
  }

  let u: URL;
  try {
    // A root-relative `/api/artifact?...` needs a base; the host is irrelevant below.
    u = new URL(candidate, "http://proxy.invalid");
  } catch {
    return null;
  }

  // (a) the proxy's own read-through form
  if (u.pathname === "/api/artifact") {
    const key = u.searchParams.get("key");
    if (!key) return null;
    const normalized = key.startsWith("artifacts/artifacts/")
      ? key.replace("artifacts/artifacts/", "artifacts/")
      : key;
    return isArtifactKey(normalized) ? normalized : null;
  }

  // Determine the host style FIRST, because only that says which part of the path is the
  // key: virtual-hosted puts the bucket in the host, path style puts it in the path.
  const host = u.hostname.toLowerCase();
  const rawPath = u.pathname.replace(/^\//, "");
  const b = bucket.toLowerCase();
  let keyPath: string | null = null;

  if (b && (host === `${b}.s3.amazonaws.com` || host.startsWith(`${b}.s3.`) || host.startsWith(`${b}.s3-`))) {
    // (b) presigned (or plain) S3 URL, virtual-hosted on OUR artifacts bucket
    keyPath = rawPath;
  } else if (/^s3[.-][a-z0-9-]*\.?amazonaws\.com$/.test(host)) {
    // (b') path style: /<bucket>/<key> — only ours
    const slash = rawPath.indexOf("/");
    if (slash > 0 && rawPath.slice(0, slash).toLowerCase() === b) keyPath = rawPath.slice(slash + 1);
  } else if (host.endsWith(".cloudfront.net")) {
    // (c) the artifacts CDN — private, so these 403 for every viewer
    keyPath = rawPath;
  }
  // Anything else (another bucket's presigned URL, fal, picsum) is not ours to re-sign.
  if (!keyPath) return null;

  const key = decode(keyPath);
  return key && isArtifactKey(key) ? key : null;
}

/** Every distinct artifact key referenced by this HTML, in first-appearance order. */
export function collectArtifactKeys(html: string, bucket: string): string[] {
  if (typeof html !== "string" || !html) return [];
  const seen = new Set<string>();
  for (const run of html.match(URL_RUN) ?? []) {
    const key = artifactKeyFromUrl(run, bucket);
    if (key) seen.add(key);
  }
  return [...seen];
}

/**
 * Replace every artifact reference with `signed.get(key)`.
 *
 * A key absent from `signed` (presign failed) keeps its ORIGINAL URL: a stale link that may
 * still work beats a broken document, and it keeps this function total.
 */
export function rewriteArtifactRefsInHtml(
  html: string,
  bucket: string,
  signed: Map<string, string>,
): string {
  if (typeof html !== "string" || !html || signed.size === 0) return html;
  return html.replace(URL_RUN, (run) => {
    const key = artifactKeyFromUrl(run, bucket);
    if (!key) return run;
    const fresh = signed.get(key);
    if (!fresh) return run;
    // The source may have been written with `&amp;` separators inside an attribute. The
    // replacement is a fresh URL, so emit it entity-escaped the same way to keep the
    // attribute value valid HTML.
    return run.includes("&amp;") ? fresh.replace(/&/g, "&amp;") : fresh;
  });
}

/** True when this object should be buffered and rewritten rather than streamed. */
export function isRewritableHtml(key: string, contentType: string | undefined, size: number | undefined): boolean {
  if (size != null && size > HTML_REWRITE_MAX_BYTES) return false;
  const type = (contentType || "").toLowerCase();
  if (type.includes("text/html")) return true;
  return !type && /\.html?$/i.test(key || "");
}

/**
 * Re-sign every artifact reference in `html` that the CALLER may read, and return the rewritten
 * document.
 *
 * `authorize` is the access check, and it is not optional: a reference inside a document is a
 * request to read THAT key, made by whoever opens the document. Without it, any user could have
 * the agent write an HTML file naming another session's `artifacts/<their-session>/…` key, open
 * it, and be handed a working presigned URL for an object they cannot read directly — the
 * document's own authorization would launder access to everything it mentions. A refused key is
 * left exactly as authored (it was not ours to grant), so the outcome is the same as if this
 * rewrite did not exist.
 *
 * Total: a presign or authorization failure for one key leaves that reference as authored and
 * never fails the document.
 */
export async function resignHtmlArtifact(
  html: string,
  bucket: string,
  deps: {
    presign: (key: string) => Promise<string>;
    authorize: (key: string) => Promise<boolean>;
    warn?: (message: string) => void;
  },
): Promise<string> {
  const keys = collectArtifactKeys(html, bucket);
  if (keys.length === 0) return html;
  const signed = new Map<string, string>();
  await Promise.all(
    keys.map(async (key) => {
      try {
        if (!(await deps.authorize(key))) return;
        signed.set(key, await deps.presign(key));
      } catch (e: any) {
        deps.warn?.(`artifact html re-sign skipped for ${key}: ${e?.message ?? e}`);
      }
    }),
  );
  return rewriteArtifactRefsInHtml(html, bucket, signed);
}
