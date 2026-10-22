/**
 * Slack HTTP API request authorizer (AuthorizationType: CUSTOM).
 *
 * Why this exists
 * ---------------
 * `POST /slack/events` was `AuthorizationType: NONE` — flagged by AppSec/Palisade
 * (Apigatewayv2 route/not_authorized). The endpoint IS authenticated (Slack HMAC
 * over `v0:{timestamp}:{rawBody}`, verified in the ingress Lambda), but there was
 * no authorizer at the gateway. An HTTP API v2 Lambda authorizer does NOT receive
 * the request body, so it cannot recompute the full HMAC — that verification must
 * stay in the ingress handler. What the authorizer CAN do at the edge, it does:
 *
 *   1. Require the Slack signature + timestamp headers (enforced by the route's
 *      `identitySource` — a request missing either is 401'd by API Gateway before
 *      this Lambda even runs).
 *   2. Reject a stale/replayed request: the `X-Slack-Request-Timestamp` must be
 *      within ±5 minutes of now (Slack's own replay window).
 *   3. Reject a malformed signature: `X-Slack-Signature` must be `v0=<64 hex>`.
 *
 * Result: the route is `CUSTOM`-authorized (defense in depth — unsigned, stale,
 * or malformed requests are dropped at the edge), while the cryptographic HMAC
 * over the body remains the ingress handler's job. SIMPLE response format.
 */

const REPLAY_WINDOW_SECONDS = 300; // Slack's recommended max request age.
const SIGNATURE_RE = /^v0=[0-9a-f]{64}$/;

interface SimpleAuthorizerEvent {
  headers?: Record<string, string | undefined>;
}

function headerValue(headers: Record<string, string | undefined> | undefined, name: string): string {
  if (!headers) return "";
  const lower = name.toLowerCase();
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === lower && typeof v === "string") return v;
  }
  return "";
}

export function isAuthorized(headers: Record<string, string | undefined> | undefined, nowSeconds: number): boolean {
  const signature = headerValue(headers, "X-Slack-Signature");
  const timestamp = headerValue(headers, "X-Slack-Request-Timestamp");
  if (!SIGNATURE_RE.test(signature)) return false;
  const ts = Number(timestamp);
  if (!Number.isFinite(ts) || ts <= 0) return false;
  if (Math.abs(nowSeconds - ts) > REPLAY_WINDOW_SECONDS) return false; // stale / future-dated => replay
  return true;
}

export const handler = async (event: SimpleAuthorizerEvent): Promise<{ isAuthorized: boolean }> => {
  const nowSeconds = Math.floor(Date.now() / 1000);
  return { isAuthorized: isAuthorized(event.headers, nowSeconds) };
};
