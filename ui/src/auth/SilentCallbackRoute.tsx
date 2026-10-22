// SilentCallbackRoute.tsx — Handles `/auth/silent-callback`.
//
// `oidc-client-ts` performs a silent token renewal by loading
// `silent_redirect_uri` inside a hidden iframe. Cognito redirects that
// iframe back to this route with `?code=…&state=…`. We call
// `signinSilentCallback()` which posts the token result back to the parent
// window's UserManager and the iframe is then discarded by oidc-client-ts.
//
// Renders nothing visible — this component only ever runs inside the
// invisible renewal iframe, so any UI here would be invisible anyway.
//
// Requirement: R2 — UI handles the OAuth2 authorization code flow.

import { useEffect } from "react";
import { signinSilentCallback } from "./authManager";

export default function SilentCallbackRoute() {
  useEffect(() => {
    // Errors here are intentionally swallowed: the iframe is hidden, and the
    // parent window's `signinSilent()` promise will reject with the same
    // error. Surfacing it from inside the iframe would just produce a
    // duplicate, unactionable error.
    void signinSilentCallback().catch(() => {
      /* parent window handles renewal failure */
    });
  }, []);

  return null;
}
