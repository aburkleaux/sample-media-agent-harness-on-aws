// useCurrentUser.ts — React hook that surfaces the signed-in user's identity
// for the UI shell.
//
// Returns a stable view that's safe to consume from `SessionSidebar` and
// other layout chrome:
//
//   - `displayName` — best-effort human-friendly label. Prefers `name`, then
//     `given_name + family_name`, then the local-part of `email`, then `sub`,
//     then "Anonymous" as a final fallback.
//   - `email` — the ID-token email claim (or undefined).
//   - `initials` — two-letter avatar fallback derived from the same source
//     as displayName.
//   - `mode` — `"cognito" | "dev" | "anonymous"` so the UI can disambiguate
//     real vs dev sessions.
//   - `signOut()` — Cognito hosted-UI logout for real sessions; clears the
//     dev token and reloads for dev sessions.
//
// The hook subscribes to the `UserManager` events so the UI re-renders when
// silent renew swaps the User object. In dev-bypass mode it polls the dev
// token cache once (it doesn't change without an explicit click) and reads
// from `getDevAccessToken()`.

import { useEffect, useMemo, useState } from "react";
import type { User } from "oidc-client-ts";
import {
  getUser,
  getUserManager,
  signoutRedirect,
} from "./authManager";
import {
  getDevAccessToken,
  isDevBypassEnabled,
  signOutDev,
} from "./devAuth";

export type CurrentUserMode = "cognito" | "dev" | "anonymous";

export interface CurrentUser {
  mode: CurrentUserMode;
  displayName: string;
  email?: string;
  initials: string;
  /** The Cognito `sub` claim — useful for diagnostics. */
  sub?: string;
  /** Sign out and tear down the in-memory session. */
  signOut: () => Promise<void>;
}

interface UserClaims {
  name?: string;
  email?: string;
  given_name?: string;
  family_name?: string;
  preferred_username?: string;
  sub?: string;
}

const ANONYMOUS: CurrentUser = {
  mode: "anonymous",
  displayName: "Not signed in",
  initials: "?",
  signOut: async () => {
    /* no-op when no session is loaded */
  },
};

function computeInitials(label: string): string {
  const parts = label.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function pickDisplayName(claims: UserClaims): {
  displayName: string;
  initials: string;
} {
  if (claims.name && claims.name.trim()) {
    return {
      displayName: claims.name.trim(),
      initials: computeInitials(claims.name),
    };
  }
  if (claims.given_name || claims.family_name) {
    const composed = [claims.given_name, claims.family_name]
      .filter((s): s is string => Boolean(s && s.trim()))
      .join(" ");
    return { displayName: composed, initials: computeInitials(composed) };
  }
  if (claims.preferred_username && claims.preferred_username.trim()) {
    const u = claims.preferred_username.trim();
    return { displayName: u, initials: computeInitials(u) };
  }
  if (claims.email && claims.email.trim()) {
    const local = claims.email.split("@")[0];
    return { displayName: claims.email, initials: computeInitials(local) };
  }
  if (claims.sub && claims.sub.trim()) {
    const s = claims.sub.trim();
    // sub is a UUID — initials would be noise; show first 6 chars label.
    return {
      displayName: s.length > 8 ? `${s.slice(0, 8)}…` : s,
      initials: s.slice(0, 2).toUpperCase(),
    };
  }
  return { displayName: "Anonymous", initials: "?" };
}

function fromCognitoUser(user: User): CurrentUser {
  const claims = (user.profile ?? {}) as UserClaims;
  const { displayName, initials } = pickDisplayName(claims);
  return {
    mode: "cognito",
    displayName,
    email: claims.email,
    initials,
    sub: claims.sub,
    signOut: async () => {
      try {
        await signoutRedirect();
      } catch {
        // If hosted-UI logout fails (e.g. logout URL not registered), at
        // least clear the in-memory user so the next render bounces to
        // sign-in via AuthGate.
        await (await getUserManager()).removeUser();
        window.location.replace("/");
      }
    },
  };
}

function devUserView(): CurrentUser {
  return {
    mode: "dev",
    displayName: "Local Dev",
    email: undefined,
    initials: "LD",
    sub: "local-dev-user",
    signOut: async () => {
      signOutDev();
      window.location.replace("/");
    },
  };
}

export function useCurrentUser(): CurrentUser {
  const devBypass = useMemo(() => isDevBypassEnabled(), []);
  const [user, setUser] = useState<User | null>(null);
  const [devTokenPresent, setDevTokenPresent] = useState<boolean>(
    () => devBypass && getDevAccessToken() !== null,
  );

  useEffect(() => {
    if (devBypass) {
      // Dev token only changes via Dev Login click or signOutDev; cheap to
      // recheck on focus rather than wiring an event emitter.
      const handle = () => setDevTokenPresent(getDevAccessToken() !== null);
      window.addEventListener("focus", handle);
      return () => window.removeEventListener("focus", handle);
    }

    let cancelled = false;
    const handleLoaded = (u: User) => setUser(u);
    const handleUnloaded = () => setUser(null);

    void getUserManager().then(async (manager) => {
      if (cancelled) return;
      manager.events.addUserLoaded(handleLoaded);
      manager.events.addUserUnloaded(handleUnloaded);
      const u = await manager.getUser();
      if (!cancelled) setUser(u);
    }).catch(() => {
      // Auth config unavailable (e.g. `GET /config` unreachable in a test/harness,
      // or a transient backend hiccup) — degrade to the anonymous view rather than
      // leaking an unhandled promise rejection (which fails the vitest run even when
      // every test passes). `user` stays null, so the memo below returns ANONYMOUS.
      if (!cancelled) setUser(null);
    });

    return () => {
      cancelled = true;
      void getUserManager().then((manager) => {
        manager.events.removeUserLoaded(handleLoaded);
        manager.events.removeUserUnloaded(handleUnloaded);
      }).catch(() => {
        // Manager was never built (config unavailable) — nothing to detach.
      });
    };
  }, [devBypass]);

  return useMemo<CurrentUser>(() => {
    if (devBypass) {
      return devTokenPresent ? devUserView() : ANONYMOUS;
    }
    if (user === null || user.expired) {
      return ANONYMOUS;
    }
    return fromCognitoUser(user);
  }, [devBypass, devTokenPresent, user]);
}
