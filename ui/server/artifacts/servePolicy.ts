/**
 * How `/api/artifact` serves an object: the content type, disposition and isolation headers.
 *
 * ── Why this exists ─────────────────────────────────────────────────────────
 * `/api/artifact` serves bytes from the APP'S OWN ORIGIN, and both the bytes and their S3
 * `ContentType` are written by the agent (or by any code it runs). So the content type is
 * attacker-influenced input, not a fact about the file. Isolation used to be applied only when that
 * type said `text/html`, `image/svg+xml` or `application/xhtml+xml`: `text/xml` (which renders
 * XHTML-namespaced markup and runs its scripts), every other `+xml`, and a type a browser sniffs
 * into HTML were served unsandboxed, with no `nosniff`, as the app itself.
 *
 * The rules are now one function, applied to EVERY response of the route:
 *   - `X-Content-Type-Options: nosniff` always, so the declared type is the only type.
 *   - The sandbox CSP always — the response is given an opaque origin, so even a document the rules
 *     below miss cannot run as the app. The single exception is `application/pdf` (see below).
 *   - A malformed type, or one naming executable code (JavaScript, WebAssembly, …), is served as
 *     `application/octet-stream` and as an attachment: nothing in the app previews those, and a
 *     same-origin script response is exactly what a sandbox CSP does not neutralise for a
 *     `<script src>` include from elsewhere.
 *   - A type nothing in the app previews inline is served as an attachment (its declared type kept,
 *     which `fetch()` callers read; a fetch ignores Content-Disposition).
 *
 * PDF and the sandbox: Chromium refuses to render a PDF in a sandboxed context — an `<iframe sandbox>`
 * or a response carrying `Content-Security-Policy: sandbox` shows "This page has been blocked" instead
 * of the document. The PDF viewer is not the page: its scripts (PDFium / pdf.js) run in the viewer's
 * own isolated context and never get the serving origin's DOM, storage or cookies. So a PDF keeps
 * inline preview without the sandbox, and `nosniff` guarantees a body claiming `application/pdf` is
 * only ever handled by the PDF viewer, never sniffed into HTML.
 */
import { DOCUMENT_SANDBOX_CSP } from "./viewTicket";

const OCTET = "application/octet-stream";

/** RFC 7231 media type: `type/subtype`, token characters only. Parameters are kept separately. */
const MEDIA_TYPE_RE = /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/;

/** Types a browser (or a plugin) would EXECUTE as code. Never served as themselves. */
const EXECUTABLE_RE =
  /(javascript|ecmascript|jscript|livescript|wasm|x-shockwave-flash|futuresplash|java-archive|java-vm|x-java|x-msdownload|x-ms-application|x-msdos-program|portable-executable|x-sh$|x-csh$|x-httpd|x-php|text\/x-component|vbscript|cache-manifest|x-silverlight)/;

/** Whole families the app previews inline (players, `<img>`, the document viewer). */
const INLINE_FAMILY_RE = /^(image|video|audio|font)\//;

/** Individual inline types: documents the viewer frames, and the text formats renderers fetch. */
const INLINE_TYPES = new Set([
  "application/pdf",
  "application/json",
  "application/ld+json",
  "application/xml",
  "application/xhtml+xml",
  "application/x-mpegurl",
  "application/vnd.apple.mpegurl",
  "application/dash+xml",
  "application/x-subrip",
  "text/html",
  "text/plain",
  "text/csv",
  "text/markdown",
  "text/xml",
  "text/vtt",
  "text/css",
  "text/tab-separated-values",
  "text/x-python",
  "text/typescript",
  "text/x-markdown",
]);

export interface ArtifactServePolicy {
  /** The `content-type` header value to send (sanitised; parameters such as charset preserved). */
  contentType: string;
  /** `inline` or `attachment` — `?download=1` always forces `attachment`. */
  disposition: "inline" | "attachment";
  /** Headers every response of this object carries (`nosniff`, and the CSP unless it is a PDF). */
  headers: Record<string, string>;
}

/** The lower-cased `type/subtype` of a header value, or null when it is not a well-formed media type. */
export function mediaTypeEssence(contentType: string | undefined | null): string | null {
  const essence = String(contentType ?? "").split(";")[0].trim().toLowerCase();
  return MEDIA_TYPE_RE.test(essence) ? essence : null;
}

/** True for a type whose response is code a browser would run. */
export function isExecutableType(essence: string): boolean {
  return EXECUTABLE_RE.test(essence);
}

/** True for a type the app previews in place (frame, player, image, or a fetch that renders it). */
export function isInlineType(essence: string): boolean {
  return INLINE_FAMILY_RE.test(essence) || INLINE_TYPES.has(essence) || essence.endsWith("+xml");
}

/** Isolation headers for a response with this (sanitised) type. Errors and JSON get them too. */
export function isolationHeaders(essence: string | null): Record<string, string> {
  return essence === "application/pdf"
    ? { "x-content-type-options": "nosniff" }
    : { "x-content-type-options": "nosniff", "content-security-policy": DOCUMENT_SANDBOX_CSP };
}

/**
 * Decide how to serve an object whose effective declared type is `contentType`.
 * `asDownload` is the caller's explicit `?download=1`.
 */
export function artifactServePolicy(contentType: string | undefined, asDownload: boolean): ArtifactServePolicy {
  const raw = String(contentType ?? "").trim();
  const essence = mediaTypeEssence(raw);
  if (!essence || isExecutableType(essence)) {
    return { contentType: OCTET, disposition: "attachment", headers: isolationHeaders(OCTET) };
  }
  // Keep well-formed parameters (charset) but nothing that could smuggle a second header.
  const params = raw.slice(raw.indexOf(";") >= 0 ? raw.indexOf(";") : raw.length);
  const safeParams = /^[\x20-\x7e]*$/.test(params) && !/[\r\n,]/.test(params) ? params : "";
  const inline = !asDownload && isInlineType(essence);
  return {
    contentType: `${essence}${safeParams}`,
    disposition: inline ? "inline" : "attachment",
    headers: isolationHeaders(essence),
  };
}

/** A `filename=` value that cannot break out of the header (quotes, backslashes, controls, non-ASCII). */
export function safeDispositionFilename(key: string): string {
  const base = String(key || "").split("/").pop() || "download";
  return base.replace(/["\\\x00-\x1f\x7f]/g, "_").replace(/[^\x20-\x7e]/g, "_");
}
