import { useEffect, useMemo, useState } from "react";
import ManifestRouter from "../components/renderer/templates/ManifestRouter";
import { parseManifest, type RenderManifest } from "../components/nle/manifestParser";
import { theme } from "../styles/tokens";
import { authFetch } from "../auth/authFetch";

/**
 * RenderDeepLink — a standalone, read-only launch surface for a single render
 * manifest, reachable by URL (e.g. from a Slack "Open interactive renderer"
 * link). It is mounted OUTSIDE `AuthGate` in `main.tsx`.
 *
 * Why no auth:
 *   - Render artifacts (manifest JSON + the media it references) are already
 *     PUBLIC via CloudFront. The `/api/artifact` proxy is explicitly in the
 *     proxy's PUBLIC_PATHS (see ui/server/app.ts + artifactPublic.test.ts): it
 *     is key-validated (only `artifacts/*`) and never 401s for a valid key.
 *   - So this page needs no Cognito login and no presigned URL — presigning
 *     CloudFront-public content is strictly worse (expiry + bypasses the CDN).
 *
 * This is NOT a second renderer. It fetches the manifest with a plain `fetch`
 * (public path, so no auth manager dependency), then hands the parsed manifest
 * to the EXISTING `ManifestRouter`, which routes it to the correct template.
 * There is no chat, no session state, and no mutation — read-only by design.
 */

// Confinement: only session-scoped render manifests. Mirrors the proxy's own
// `artifacts/*` key gate and the design's "validate key is session-scoped
// render JSON" rule. Character class is intentionally conservative.
const MANIFEST_KEY_RE =
  /^artifacts\/[A-Za-z0-9._-]+\/render\/[A-Za-z0-9._-]+\.json$/;

export interface DeepLinkParams {
  sessionId: string;
  manifestKey: string;
}

/** Read `sessionId` + `manifestKey` from the current URL. Exported for tests. */
export function readDeepLinkParams(search: string = window.location.search): DeepLinkParams {
  const q = new URLSearchParams(search);
  return {
    sessionId: q.get("sessionId") ?? "",
    manifestKey: q.get("manifestKey") ?? "",
  };
}

/**
 * Validate a deep-link. Returns an error message string, or `null` when valid.
 * Pure and exported so the security-relevant confinement is unit-testable.
 */
export function validateDeepLink(sessionId: string, manifestKey: string): string | null {
  if (!manifestKey) return "Missing manifestKey";
  if (!MANIFEST_KEY_RE.test(manifestKey)) {
    return "Unsupported or unauthorized manifest key";
  }
  if (sessionId) {
    // key shape is artifacts/<session>/render/<file>.json — segment [1] is the session.
    const segment = manifestKey.split("/")[1];
    if (segment !== sessionId) {
      return "Manifest key does not belong to this session";
    }
  }
  return null;
}

/** Public read-through URL for a manifest key. Exported for tests. */
export function manifestFetchUrl(manifestKey: string): string {
  return `/api/artifact?key=${encodeURIComponent(manifestKey)}`;
}

// A just-produced manifest can be transiently missing (the proxy returns 410 for an
// absent object, and the render key reaches this link around the time the object
// lands), so retry transient failures with backoff before showing the error —
// mirrors ManifestRouter's loader. A genuinely-missing key (404) is NOT transient
// and fails fast.
const MANIFEST_LOAD_TIMEOUT_MS = 10_000;
const MANIFEST_MAX_ATTEMPTS = 4;
const MANIFEST_RETRY_BASE_MS = 700;

function isTransientManifestStatus(status: number): boolean {
  return status === 408 || status === 410 || status === 425 || status === 429 || status >= 500;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

type LoadState =
  | { status: "invalid"; message: string }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; manifest: RenderManifest };

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        height: "100vh",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        color: theme.colors.textSecondary,
        fontSize: theme.typography.sizes.body,
        textAlign: "center",
      }}
    >
      {children}
    </div>
  );
}

export default function RenderDeepLink({ authenticated = false }: { authenticated?: boolean } = {}) {
  const { sessionId, manifestKey } = useMemo(() => readDeepLinkParams(), []);
  const invalidReason = useMemo(
    () => validateDeepLink(sessionId, manifestKey),
    [sessionId, manifestKey],
  );

  const [state, setState] = useState<LoadState>(() =>
    invalidReason ? { status: "invalid", message: invalidReason } : { status: "loading" },
  );

  useEffect(() => {
    if (invalidReason) return;
    let cancelled = false;
    (async () => {
      setState({ status: "loading" });
      for (let attempt = 1; attempt <= MANIFEST_MAX_ATTEMPTS; attempt++) {
        if (cancelled) return;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), MANIFEST_LOAD_TIMEOUT_MS);
        try {
          // Signed-in mode (an environment whose artifacts CDN is private): `/api/artifact` is
          // authenticated, so the manifest goes through authFetch. Public mode keeps plain fetch.
          const doFetch = authenticated ? authFetch : fetch;
          const res = await doFetch(manifestFetchUrl(manifestKey), { signal: controller.signal });
          clearTimeout(timeout);
          if (cancelled) return;
          if (!res.ok) {
            // 410 (not-yet-readable) and other transient statuses retry; a genuine
            // miss (404) or refusal fails fast.
            if (isTransientManifestStatus(res.status) && attempt < MANIFEST_MAX_ATTEMPTS) {
              await sleep(MANIFEST_RETRY_BASE_MS * attempt);
              continue;
            }
            setState({ status: "error", message: `Failed to load manifest (${res.status})` });
            return;
          }
          const parsed = parseManifest(await res.text());
          if (cancelled) return;
          if (!parsed.ok) {
            setState({
              status: "error",
              message: parsed.errors.map((e) => e.message).join("; ") || "Invalid manifest",
            });
            return;
          }
          setState({ status: "ready", manifest: parsed.manifest });
          return;
        } catch (err) {
          clearTimeout(timeout);
          if (cancelled) return;
          // Timeouts and network errors are transient too — retry, then report.
          if (attempt < MANIFEST_MAX_ATTEMPTS) {
            await sleep(MANIFEST_RETRY_BASE_MS * attempt);
            continue;
          }
          const isAbort = err instanceof Error && err.name === "AbortError";
          setState({
            status: "error",
            message: isAbort
              ? "Manifest loading timed out (10s)"
              : err instanceof Error ? err.message : String(err),
          });
          return;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [invalidReason, manifestKey, authenticated]);

  if (state.status === "invalid") {
    return <Centered>{state.message}</Centered>;
  }
  if (state.status === "loading") {
    return <Centered>Loading render…</Centered>;
  }
  if (state.status === "error") {
    return (
      <Centered>
        <div>
          <p style={{ color: theme.colors.textPrimary, marginBottom: 8 }}>
            Could not display this render
          </p>
          <p style={{ fontSize: theme.typography.sizes.meta }}>{state.message}</p>
        </div>
      </Centered>
    );
  }

  // Hand the parsed manifest to the existing router. Passing `data.manifest`
  // (not `manifestUrl`) means ManifestRouter renders immediately and never runs
  // its own manifest fetch. Public mode: `resolveArtifacts={false}` keeps the public CDN
  // URLs (presigning needs the AUTHENTICATED `/api/artifact-urls`, and a 401 would bounce
  // this page to sign-in). Signed-in mode (`RenderDeepLinkRoute` wraps this page in AuthGate
  // when the env's artifacts CDN is private): resolve media to presigned URLs like the app.
  return (
    <div style={{ height: "100vh", overflow: "auto" }}>
      <ManifestRouter data={{ manifest: state.manifest }} title="Rendered result" theme={theme} resolveArtifacts={authenticated} />
    </div>
  );
}
