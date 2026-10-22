// Slack v0 HMAC signature verification.
// Reqs 1.1–1.3, 9.1. rawBody MUST be the exact received bytes.

import { createHmac, timingSafeEqual } from "node:crypto";

const SCHEME = "v0";
const MAX_SKEW_SECONDS = 60 * 5;

export interface SignatureCheck {
  ok: boolean;
  reason?: "bad_timestamp" | "stale_timestamp" | "bad_signature";
}

export function verifySlackSignature(input: {
  signingSecret: string;
  timestampHeader: string | undefined;
  signatureHeader: string | undefined;
  rawBody: string;
  nowSeconds?: number;
}): SignatureCheck {
  const now = input.nowSeconds ?? Math.floor(Date.now() / 1000);
  const ts = Number(input.timestampHeader);
  if (!input.timestampHeader || !Number.isFinite(ts)) {
    return { ok: false, reason: "bad_timestamp" };
  }
  if (Math.abs(now - ts) > MAX_SKEW_SECONDS) {
    return { ok: false, reason: "stale_timestamp" };
  }
  const base = `${SCHEME}:${input.timestampHeader}:${input.rawBody}`;
  const expected =
    `${SCHEME}=` + createHmac("sha256", input.signingSecret).update(base, "utf8").digest("hex");
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(input.signatureHeader ?? "", "utf8");
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return { ok: false, reason: "bad_signature" };
  }
  return { ok: true };
}
