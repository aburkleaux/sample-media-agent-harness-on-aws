// originVerify.ts — the proxy half of the ALB origin lock (security finding #2, interim).
//
// The UiProxy ALB is internet-facing and its security group admits CloudFront's whole origin-facing
// prefix list — i.e. EVERY CloudFront distribution, anyone's. What makes a request ours is the
// `X-Origin-Verify` header our distribution adds (its value is a Secrets Manager secret injected here
// as ORIGIN_VERIFY_SECRET). CloudFront overwrites a viewer-sent header of that name, so a client
// cannot forge it through our distribution, and without it a request from anywhere else is refused.
//
// Exempt: `/health` only — the ALB's own target health check, which carries no header and serves
// no data. Unset secret (local dev, unit tests) → no enforcement, so `npm run dev` is unchanged.
import crypto from "crypto";

export const ORIGIN_VERIFY_HEADER = "x-origin-verify";

const EXEMPT_PATHS = new Set(["/health"]);

function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  // Hash both so the comparison is constant-time regardless of the presented length.
  const ha = crypto.createHash("sha256").update(ab).digest();
  const hb = crypto.createHash("sha256").update(bb).digest();
  return crypto.timingSafeEqual(ha, hb) && ab.length === bb.length;
}

/** Express middleware; a no-op when `secret` is empty. */
export function originVerifyMiddleware(secret: string | undefined) {
  const expected = (secret ?? "").trim();
  return (req: any, res: any, next: any) => {
    if (!expected) return next();
    const path = String(req.originalUrl || req.url || "").split("?")[0];
    if (EXEMPT_PATHS.has(path)) return next();
    const got = req.headers?.[ORIGIN_VERIFY_HEADER];
    if (typeof got === "string" && safeEqual(got, expected)) return next();
    res.writeHead(403, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { code: "origin_not_allowed", message: "Use the CloudFront endpoint" } }));
  };
}
