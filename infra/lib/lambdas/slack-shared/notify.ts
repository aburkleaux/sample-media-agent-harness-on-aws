// Pure decision helpers for async push (Feature 1) + native upload (Feature 3).
// No I/O.

export function isSlackSession(sessionId: string | undefined): boolean {
  return typeof sessionId === "string" && sessionId.startsWith("slack-");
}

const TERMINAL = new Set([
  "SUCCEEDED",
  "COMPLETED",
  "FAILED",
  "ERROR",
  "TIMED_OUT",
  "ABORTED",
  "CANCELLED",
  "CANCELED",
]);

export function isTerminalStatus(status: string | undefined): boolean {
  return TERMINAL.has((status ?? "").toUpperCase());
}

export function isSuccessStatus(status: string | undefined): boolean {
  const s = (status ?? "").toUpperCase();
  return s === "SUCCEEDED" || s === "COMPLETED";
}

export function shouldUploadNatively(sizeBytes: number, capBytes: number): boolean {
  return (
    Number.isFinite(sizeBytes) &&
    sizeBytes > 0 &&
    Number.isFinite(capBytes) &&
    capBytes > 0 &&
    sizeBytes <= capBytes
  );
}

export function extractArtifactUrls(text: string, artifactsDomain: string): string[] {
  if (!text || !artifactsDomain) return [];
  const escaped = artifactsDomain.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`https://${escaped}/[^\\s)\\]<>"']+`, "g");
  const seen = new Set<string>();
  const out: string[] = [];
  for (const m of text.matchAll(re)) {
    const url = m[0].replace(/[.,);\]]+$/, "");
    if (!seen.has(url)) {
      seen.add(url);
      out.push(url);
    }
  }
  return out;
}

/**
 * `projects/<pid>/sessions/<sid>/output/<file>` — the session output layout (asset-lineage Phase 3 (a)).
 * A copy of ui/src/shared/sessionLayout `isProjectSessionOutputKey` (this Lambda bundle cannot import it),
 * pinned to it by artifactS3.test.ts over agent/tests/fixtures/session_layout_cases.json.
 */
const PROJECT_SESSION_OUTPUT_RE =
  /^projects\/p[0-9a-f]{32}\/sessions\/(?!(?:external|durable|imports|uploads|m2c)\/)[A-Za-z0-9][A-Za-z0-9._-]{7,127}\/output\/[^/\\](?:[^\\]*[^/\\])?$/;

/**
 * The S3 key an artifacts-CDN URL (`https://<artifactsDomain>/artifacts/<key>`) identifies, or null
 * for any other URL. The CDN URL is a stable IDENTIFIER: when the CDN is private it is not fetchable,
 * so the Lambdas read the object by key from the bucket instead. Rejects traversal/backslashes and
 * collapses a doubled `artifacts/artifacts/` prefix (the agent sometimes emits it).
 */
export function artifactKeyFromUrl(url: string, artifactsDomain: string): string | null {
  if (!url || !artifactsDomain) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.host.toLowerCase() !== artifactsDomain.toLowerCase()) return null;
  let key: string;
  try {
    key = decodeURIComponent(parsed.pathname.slice(1));
  } catch {
    return null;
  }
  if (key.includes("..") || key.includes("\\")) return null;
  // A projects-session OUTPUT (asset-lineage Phase 3 (a)) — and only that sub-tree of `projects/`.
  if (PROJECT_SESSION_OUTPUT_RE.test(key)) return key;
  if (!key.startsWith("artifacts/") || key.length <= "artifacts/".length) return null;
  return key.startsWith("artifacts/artifacts/") ? key.replace("artifacts/artifacts/", "artifacts/") : key;
}

/** True when `ARTIFACTS_CDN_PRIVATE` is on (`1`/`true`/`yes`, any case), like the agent's `cdn_private()`. */
export function cdnPrivateFromEnv(value: string | undefined): boolean {
  return ["1", "true", "yes"].includes((value ?? "").trim().toLowerCase());
}

/**
 * Reply text with every one of OUR artifacts-CDN URLs replaced by the UI's authenticated opener,
 * `https://<uiDomain>/open?key=<urlencoded key>`. Used only when the artifacts CDN is private
 * (`artifactsCdnPrivate`): a CDN link would 403 for a Slack reader, while the UI route resolves the key
 * through the signed-in user's session. Any other URL (incl. another host's) is left untouched, as is
 * text when `uiDomain` or `artifactsDomain` is empty. `|` ends a URL so a Slack `<url|label>` link keeps
 * its label; trailing sentence punctuation is not part of the URL (same rule as `extractArtifactUrls`).
 */
export function rewriteArtifactUrlsForPrivateCdn(text: string, artifactsDomain: string, uiDomain: string): string {
  if (!text || !artifactsDomain || !uiDomain) return text;
  const escaped = artifactsDomain.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`https://${escaped}/[^\\s)\\]<>"'|]+`, "gi");
  const host = uiDomain.replace(/^https?:\/\//i, "").replace(/\/+$/, "");
  return text.replace(re, (match) => {
    const trail = /[.,);\]]+$/.exec(match)?.[0] ?? "";
    const url = trail ? match.slice(0, -trail.length) : match;
    const key = artifactKeyFromUrl(url, artifactsDomain);
    if (!key) return match;
    return `https://${host}/open?key=${encodeURIComponent(key)}${trail}`;
  });
}
