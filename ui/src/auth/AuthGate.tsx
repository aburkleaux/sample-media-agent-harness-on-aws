// AuthGate.tsx — Block the app shell behind a Cognito sign-in (or Dev Login
// in local dev).
//
// Wraps the root component so that:
//   1. Callback paths (`/auth/callback`, `/auth/silent-callback`) render
//      `children` immediately. Those routes need to mount BEFORE auth
//      completes so they can call `signinCallback()` / `signinSilentCallback()`
//      against the URL fragment Cognito redirected back with.
//   2. When `VITE_DEV_BYPASS_AUTH === 'true'`, we don't redirect to Cognito.
//      Instead we render a tiny "Dev Login" screen that mints a fake HS256
//      JWT (see `devAuth.ts`) and then renders `children`. If a valid dev
//      token is already cached (e.g. across a page reload) we skip the
//      button and render `children` immediately.
//   3. Otherwise, we ask the auth manager whether a user is already loaded.
//      If yes, render the app. If no, kick off `signinRedirect()` and show
//      a minimal loading state until the browser navigates away.
//
// Requirements: R2 — UI handles the OAuth2 authorization code flow.
//                R7 — dev mode still works without provisioning a real user.

import { useEffect, useState, type ReactNode } from "react";
import { getUser, signinRedirect } from "./authManager";
import { hasValidDevAuth, isDevBypassEnabled, signInDev } from "./devAuth";
import LandingPage from "../views/LandingPage";
import { theme } from '../styles/tokens';

const CALLBACK_PATHS = new Set(["/auth/callback", "/auth/silent-callback"]);

function isCallbackPath(pathname: string): boolean {
  return CALLBACK_PATHS.has(pathname);
}

type GateState =
  | "rehydrating"
  | "landing"
  | "redirecting"
  | "authenticated"
  | "dev-login-required"
  | "dev-signing-in"
  | "dev-error";

interface AuthGateControls {
  onShowLanding: () => void;
}

interface AuthGateProps {
  children: (controls: AuthGateControls) => ReactNode;
}

export default function AuthGate({ children }: AuthGateProps) {
  const [pathname] = useState(() => window.location.pathname);

  const onCallbackPath = isCallbackPath(pathname);
  const devBypass = isDevBypassEnabled();

  const initialState: GateState = onCallbackPath
    ? "authenticated"
    : devBypass
      ? hasValidDevAuth()
        ? "authenticated"
        : "landing"
      : "rehydrating";

  const [state, setState] = useState<GateState>(initialState);
  const [showLanding, setShowLanding] = useState(false);

  useEffect(() => {
    if (onCallbackPath || devBypass) {
      return;
    }

    let cancelled = false;

    async function rehydrate() {
      try {
        const user = await getUser();
        if (cancelled) return;

        if (user !== null && !user.expired) {
          setState("authenticated");
          return;
        }

        setState("landing");
      } catch {
        if (cancelled) return;
        setState("landing");
      }
    }

    void rehydrate();

    return () => {
      cancelled = true;
    };
  }, [onCallbackPath, devBypass]);

  if (onCallbackPath || state === "authenticated") {
    return (
      <>
        {showLanding ? (
          <LandingPage entryMode="return" onSignIn={() => setShowLanding(false)} />
        ) : null}
        <div
          aria-hidden={showLanding ? true : undefined}
          style={{ display: showLanding ? "none" : "contents" }}
        >
          {children({ onShowLanding: () => setShowLanding(true) })}
        </div>
      </>
    );
  }

  if (state === "landing") {
    return (
      <LandingPage
        onSignIn={async () => {
          if (devBypass) {
            setState("dev-signing-in");
            try {
              await signInDev();
              setState("authenticated");
            } catch {
              setState("dev-error");
            }
          } else {
            setState("redirecting");
            // Come back to the page the user opened (e.g. a /render-view deep link), not `/`.
            await signinRedirect(window.location.pathname + window.location.search);
          }
        }}
      />
    );
  }

  if (
    state === "dev-login-required" ||
    state === "dev-signing-in" ||
    state === "dev-error"
  ) {
    return (
      <DevLoginScreen
        state={state}
        onSignIn={async () => {
          setState("dev-signing-in");
          try {
            await signInDev();
            setState("authenticated");
          } catch (err) {
            console.error("[AuthGate] Dev sign-in failed:", err);
            setState("dev-error");
          }
        }}
      />
    );
  }

  return <AuthLoading state={state} />;
}

interface DevLoginScreenProps {
  state: "dev-login-required" | "dev-signing-in" | "dev-error";
  onSignIn: () => void;
}

function DevLoginScreen({ state, onSignIn }: DevLoginScreenProps) {
  const isBusy = state === "dev-signing-in";
  const buttonLabel = isBusy ? "Signing in…" : "Dev Login";

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 16,
        fontFamily: 'var(--font-body)',
        color: theme.colors.textPrimary,
        background: theme.colors.background,
        padding: 24,
      }}
    >
      <div style={{ fontSize: 'var(--fs-xl)', fontWeight: 600 }}>Local Dev Mode</div>
      <div style={{ fontSize: 'var(--fs-md)', color: theme.colors.textSecondary, textAlign: "center", maxWidth: 420 }}>
        VITE_DEV_BYPASS_AUTH is enabled. Click below to mint a fake JWT for
        the synthetic <code>local-dev-user</code> account and skip the
        Cognito hosted UI.
      </div>
      <button
        type="button"
        onClick={onSignIn}
        disabled={isBusy}
        style={{
          padding: "8px 20px",
          fontSize: 'var(--fs-md)',
          fontWeight: 500,
          background: isBusy ? theme.colors.textTertiary : theme.colors.primary,
          color: theme.colors.onPrimary,
          border: "none",
          borderRadius: theme.radius.button,
          cursor: isBusy ? "default" : "pointer",
        }}
      >
        {buttonLabel}
      </button>
      {state === "dev-error" ? (
        <div role="alert" style={{ fontSize: 'var(--fs-sm)', color: theme.colors.error }}>
          Dev sign-in failed. Check the console and try again.
        </div>
      ) : null}
      <div style={{ fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary }}>
        Do not enable this in production deployments.
      </div>
    </div>
  );
}

function AuthLoading({ state }: { state: Exclude<GateState, "authenticated" | "landing" | "dev-login-required" | "dev-signing-in" | "dev-error"> }) {
  const message =
    state === "redirecting" ? "Signing you in…" : "Loading…";

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
      <Spinner />
      <div style={{ fontSize: 'var(--fs-md)' }}>{message}</div>
    </div>
  );
}

function Spinner() {
  return (
    <div
      aria-hidden="true"
      style={{
        width: 28,
        height: 28,
        borderRadius: "50%",
        border: `3px solid ${theme.colors.border}`,
        borderTopColor: theme.colors.textSecondary,
        animation: "auth-gate-spin 0.8s linear infinite",
      }}
    >
      <style>{`@keyframes auth-gate-spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
