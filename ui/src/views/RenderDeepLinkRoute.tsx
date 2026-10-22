import { useEffect, useState } from "react";
import AuthGate from "../auth/AuthGate";
import RenderDeepLink from "./RenderDeepLink";
import { loadRuntimeConfig } from "../config/runtimeConfig";

/**
 * `/render-view` entry point. Decides, per environment, whether the deep link needs sign-in.
 *
 * Where the artifacts CDN is still public (`/config` → `artifactsRequireAuth: false`), the page
 * stays login-free exactly as before. Where it is private (`artifactsCdnPrivate`, staging first),
 * the manifest and its media are only readable by an authenticated user, so the page renders under
 * `AuthGate`; sign-in returns to this same URL (see `signinRedirect(returnTo)` / `safeReturnTo`).
 *
 * A `/config` failure falls back to the public page: that is the old behavior, and an env that is
 * private will then show "Could not display this render" rather than loop.
 */
export default function RenderDeepLinkRoute() {
  const [requireAuth, setRequireAuth] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadRuntimeConfig()
      .then((cfg: { artifactsRequireAuth?: unknown }) => {
        if (!cancelled) setRequireAuth(cfg?.artifactsRequireAuth === true);
      })
      .catch(() => {
        if (!cancelled) setRequireAuth(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (requireAuth === null) return null;
  if (!requireAuth) return <RenderDeepLink />;
  return <AuthGate>{() => <RenderDeepLink authenticated />}</AuthGate>;
}
