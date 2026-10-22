// CallbackRoute.tsx — Handles `/auth/callback` after the Cognito hosted UI
// redirects the browser back with an authorization code.
//
// Cognito's auth-code grant lands on `redirect_uri` with `?code=…&state=…`
// in the URL. `oidc-client-ts`'s `signinCallback()` exchanges the code for
// tokens, stores the resulting User in the manager's user store, and
// resolves once the in-memory token state is ready.
//
// On success we navigate to `/` and replace the history entry so the
// `?code=…` URL doesn't stay in the browser back stack. On failure we still
// land on `/` — `AuthGate` will then re-trigger `signinRedirect()` because
// no user is loaded.
//
// Requirement: R2 — UI handles the OAuth2 authorization code flow.

import { useEffect, useState } from "react";
import { safeReturnTo, signinCallback } from "./authManager";
import { theme } from '../styles/tokens';

type CallbackState = "processing" | "error";

export default function CallbackRoute() {
  const [state, setState] = useState<CallbackState>("processing");

  useEffect(() => {
    let cancelled = false;

    async function handle() {
      try {
        const user = await signinCallback();
        if (cancelled) return;
        // Return to the page the user started on (carried in the OIDC state by signinRedirect),
        // e.g. a render deep link; `safeReturnTo` only accepts same-origin relative paths.
        const target = safeReturnTo(user?.state);
        // Replace so the `?code=…` URL is not retained in browser history.
        window.history.replaceState({}, "", target);
        // Hard reload so the rest of the app re-mounts under AuthGate with
        // the freshly-signed-in user state. Avoids needing a router or a
        // shared auth subscription just for this one transition.
        window.location.replace(target);
      } catch {
        if (cancelled) return;
        setState("error");
      }
    }

    void handle();

    return () => {
      cancelled = true;
    };
  }, []);

  return <CallbackStatus state={state} />;
}

function CallbackStatus({ state }: { state: CallbackState }) {
  const message =
    state === "error"
      ? "Sign-in failed. Returning to the app…"
      : "Completing sign-in…";

  // If we hit the error branch, give the user a moment to read the message
  // before sending them back to `/` where AuthGate will retry the redirect.
  useEffect(() => {
    if (state !== "error") return;
    const timer = window.setTimeout(() => {
      window.location.replace("/");
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [state]);

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        fontFamily: 'var(--font-body)',
        color: theme.colors.textSecondary,
        background: theme.colors.background,
      }}
    >
      <div style={{ fontSize: 'var(--fs-md)' }}>{message}</div>
    </div>
  );
}
