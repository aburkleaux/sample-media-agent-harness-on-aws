/**
 * View tickets: a credential for reading ONE artifact through `/api/artifact`, minted for a
 * signed-in viewer and carried in the URL instead of their login token.
 *
 * ── Why this exists ─────────────────────────────────────────────────────────
 * An HTML artifact has to be loaded through `/api/artifact` (which re-signs the links inside it
 * per view — see htmlArtifactRewrite.ts), and an `<iframe>` or a new tab cannot send an
 * Authorization header. The document viewer therefore put the user's Cognito access token in
 * the URL as `?access_token=`. But the page is AGENT-AUTHORED, and any script in it can read
 * its own `location.href` — sandboxed or not — so the token reached code the user never wrote.
 * A leaked access token is the user's whole identity for up to an hour, on every route.
 *
 * A ticket is the opposite: valid for exactly one key, read-only, for one hour, and useless
 * anywhere else. If the page reads it from its own URL, it learns the right to read itself.
 *
 * Format: `v1.<expEpochSeconds>.<base64url(actorId)>.<base64url(HMAC-SHA256)>`, MAC over
 * `v1|<key>|<exp>|<actorId>`. The actor travels in the ticket because the read still has to be
 * authorized AS that viewer: the links inside the document are re-signed only for keys the
 * viewer may read (htmlArtifactRewrite.resignHtmlArtifact).
 */
import crypto from "crypto";

const VERSION = "v1";

/** Same lifetime as a presigned artifact URL, so the SPA's re-resolve cadence fits both. */
export const VIEW_TICKET_TTL_S = 3600;

const b64url = (b: Buffer): string => b.toString("base64url");

function mac(secret: string, key: string, exp: number, actorId: string): Buffer {
  return crypto
    .createHmac("sha256", secret)
    .update(`${VERSION}|${key}|${exp}|${actorId}`, "utf8")
    .digest();
}

/** Mint a ticket for `actorId` to read `key` until now + `ttlS`. */
export function mintViewTicket(
  key: string,
  actorId: string,
  secret: string,
  nowMs: number = Date.now(),
  ttlS: number = VIEW_TICKET_TTL_S,
): { ticket: string; expiresAt: number } {
  if (!secret || !key || !actorId) throw new Error("view ticket needs a secret, a key and an actor");
  const exp = Math.floor(nowMs / 1000) + ttlS;
  const sig = mac(secret, key, exp, actorId);
  return { ticket: `${VERSION}.${exp}.${b64url(Buffer.from(actorId, "utf8"))}.${b64url(sig)}`, expiresAt: exp };
}

/**
 * The actor a ticket was minted for, when it is valid for THIS key right now; otherwise null.
 * Constant-time comparison; never throws.
 */
export function verifyViewTicket(
  ticket: unknown,
  key: string,
  secret: string,
  nowMs: number = Date.now(),
): string | null {
  try {
    if (typeof ticket !== "string" || !secret || !key) return null;
    const parts = ticket.split(".");
    if (parts.length !== 4 || parts[0] !== VERSION) return null;
    const exp = Number(parts[1]);
    if (!Number.isInteger(exp) || exp < Math.floor(nowMs / 1000)) return null;
    const actorId = Buffer.from(parts[2], "base64url").toString("utf8");
    if (!actorId) return null;
    const given = Buffer.from(parts[3], "base64url");
    const expected = mac(secret, key, exp, actorId);
    if (given.length !== expected.length || !crypto.timingSafeEqual(given, expected)) return null;
    return actorId;
  } catch {
    return null;
  }
}

/** Artifacts that are documents a browser renders, and so must be viewed through the proxy. */
export function isViewedDocumentKey(key: string): boolean {
  return /\.(html?|svg)$/i.test(key || "");
}

/** `/api/artifact?key=…[&view=…]` — the same-origin URL a document should be loaded from. */
export function documentViewUrl(key: string, ticket?: string): string {
  const base = `/api/artifact?key=${encodeURIComponent(key)}`;
  return ticket ? `${base}&view=${encodeURIComponent(ticket)}` : base;
}

/**
 * Isolates an artifact document from the app that serves it. Agent-written HTML is served from
 * the app's own origin; without this its scripts run AS the app (they can read the signed-in
 * user's tokens from storage and call every API). `sandbox` without `allow-same-origin` gives the
 * document an opaque origin even when opened in its own tab. Scripts, forms, popups and
 * downloads stay on, so interactive pages (games, slideshows, charts) keep working; what stops
 * working is same-origin storage (localStorage, cookies) inside the page.
 */
export const DOCUMENT_SANDBOX_CSP =
  "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";

/**
 * True for a response type a browser would render as an active (script-capable) document: HTML,
 * and every XML type — `text/xml`/`application/xml` render XHTML-namespaced markup and run its
 * scripts just like `+xml` types (SVG, XHTML). `/api/artifact` no longer gates isolation on this —
 * it sandboxes every response (servePolicy.ts) — but it names the set for callers that must know.
 */
export function isActiveDocumentType(contentType: string | undefined): boolean {
  const t = (contentType || "").split(";")[0].trim().toLowerCase();
  return t === "text/html" || t === "text/xml" || t === "application/xml" || t.endsWith("+xml");
}
