/**
 * Resolve an artifact reference into a src an `<iframe>`/`<embed>` can safely load.
 *
 * FAILS CLOSED. Anything this cannot positively vouch for returns `null`, and the caller must
 * show an error instead of pointing an element at it. The predecessor (`toProxy` in
 * DocumentViewer) ended with a bare `return url`, so an unrecognised reference was handed
 * straight to `<iframe src>` — where a relative or same-origin path resolves against the SPA's
 * own origin, CloudFront answers the unmatched path with `index.html`, and the harness renders
 * itself inside its own document viewer. That is the "document renders the harness UI" report:
 * not a broken document, a silent passthrough.
 *
 * The origin is a parameter rather than read from `location` so the rule is testable and so a
 * caller can't accidentally compare against the wrong origin.
 */

import { isProjectSessionOutputKey } from "../../../shared/sessionLayout";

/** Content served from our own `artifacts/` tree goes through the proxy, for content-type + CORS. */
function proxied(key: string): string {
  return `/api/artifact?key=${encodeURIComponent(key)}`;
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * True when a reference cannot name ONE object: it contains a backslash or a CR/LF (raw or
 * percent-encoded), or a second `http(s)://` after its start. An agent that writes two links into one
 * field produces exactly this (session 865950fd: `previewUrl` was
 * `.../compliance_report.json\nhttps://.../compliance_report.pdf`, with a literal `\n`). Proxied, that
 * key fails the server's artifact-key policy and the iframe shows its raw JSON 400 body; passed
 * through, `new URL` silently strips the newline and loads a path nobody wrote.
 */
export function isMalformedReference(raw: string): boolean {
  if (/[\\\r\n]/.test(raw) || /[\\\r\n]/.test(safeDecode(raw))) return true;
  return /https?:\/\//i.test(raw.slice(1));
}

export function toProxySrc(url: string, spaOrigin: string): string | null {
  if (!url || typeof url !== "string") return null;
  const raw = url.trim();
  if (!raw) return null;
  if (isMalformedReference(raw)) return null;

  // Already the proxy. Must be a PATH, not `https://elsewhere/api/artifact...`.
  if (raw.startsWith("/api/artifact")) return raw;

  // A CloudFront artifact url -> proxy by key.
  const cf = raw.match(/^https?:\/\/[^/]*cloudfront\.net\/(.+)$/i);
  if (cf) return proxied(cf[1]);

  // A bare artifacts/ key, or a projects-session OUTPUT key (asset-lineage Phase 3 (a)).
  if (raw.startsWith("artifacts/") || isProjectSessionOutputKey(raw)) return proxied(raw);

  // Everything else must be an absolute http(s) url on a DIFFERENT origin. `new URL` rejects
  // relative refs (no base given), which is what closes the hole: `report.pdf`,
  // `/output/doc.pdf` and `//evil.example/x.pdf` all throw or fail the checks below rather
  // than being passed through.
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;

  // Our own origin is exactly the hazard: it is what resolves to the SPA fallback. Anything
  // legitimately ours was already matched above as a proxy path or an artifacts key.
  try {
    if (parsed.origin === new URL(spaOrigin).origin) return null;
  } catch {
    return null; // an unparseable origin means we cannot rule the hazard out
  }

  return raw;
}

/**
 * The `sandbox` flags for a frame showing an artifact document: scripts, forms, popups, modals and
 * downloads stay on (interactive agent pages keep working), and `allow-same-origin` is NEVER given, so
 * the framed document has an opaque origin and cannot read this app's storage or call its APIs. The
 * same flags the proxy sends as `Content-Security-Policy: sandbox` (ui/server/artifacts/viewTicket.ts);
 * the attribute is defence in depth for a frame whose bytes come from somewhere else.
 */
export const DOCUMENT_FRAME_SANDBOX =
  "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";

/** True when a frame src names a PDF: the `key` of an `/api/artifact` URL, else the URL's path. */
export function isPdfReference(src: string): boolean {
  if (!src) return false;
  try {
    const u = new URL(src, "https://spa.invalid");
    const key = u.pathname === "/api/artifact" ? u.searchParams.get("key") ?? "" : u.pathname;
    return /\.pdf$/i.test(safeDecode(key));
  } catch {
    return false;
  }
}

/**
 * `sandbox` for a document frame, or `undefined` for a PDF. Chromium refuses to render a PDF in a
 * sandboxed frame ("This page has been blocked by Chrome"), and the PDF viewer runs the document's
 * scripts in its own isolated context — never with the embedding origin's DOM or storage — so a PDF is
 * the one document framed without it. Everything else (HTML, SVG, XML, text, …) is sandboxed. The
 * proxy's headers still isolate a ".pdf" whose stored type is really HTML (servePolicy.ts).
 * Callers must set the result when the frame is CREATED (key the element on it): adding `sandbox` to a
 * live frame applies only to its next navigation.
 */
export function frameSandboxFor(src: string | undefined | null): string | undefined {
  return src && isPdfReference(src) ? undefined : DOCUMENT_FRAME_SANDBOX;
}
