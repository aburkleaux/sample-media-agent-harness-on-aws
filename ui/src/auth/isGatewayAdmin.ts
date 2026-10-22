// isGatewayAdmin.ts — Gate the gateway-admin surface on the Cognito group.
//
// The runtime treats `gateway-admins` membership as the authoritative check
// (see `_is_admin` in `agent/src/main.py`). The UI mirrors that locally so
// non-admins don't see the menu item or hit a 403 on the admin route. The
// server-side check is still the source of truth — this helper exists purely
// to keep admin-only chrome out of non-admin DOM.
//
// `cognito:groups` lands on the ID token as a string array when the user
// belongs to one or more groups, and is omitted entirely when they belong
// to none. `oidc-client-ts` surfaces ID-token claims via `User.profile`.
//
// Fail closed: any unexpected shape (missing claim, wrong type, exception
// during user load) resolves to `false`. There is no dev override — set the
// group on the local user pool if you need to exercise admin paths in dev.
//
// Requirements: R3 (admin-only mutations), R7 (admin UI panel).

import { useEffect, useState } from "react";
import type { User } from "oidc-client-ts";
import { getUser, getUserManager } from "./authManager";
import { isDevBypassEnabled } from "./devAuth";

/** The Cognito group whose members can manage gateway registrations. */
export const GATEWAY_ADMIN_GROUP = "gateway-admins";

/**
 * Pure helper. Returns `true` when `claims["cognito:groups"]` is an array
 * containing `"gateway-admins"`. Anything else — missing claim, wrong shape,
 * `null`/`undefined` claims — resolves to `false`.
 *
 * Exported separately from the async/hook wrappers so unit tests and the
 * server-side check (in `ui/auth/validateJwt.ts`, if it grows admin-aware
 * paths) can share the same predicate.
 */
export function isGatewayAdminFromClaims(
  claims: Record<string, unknown> | null | undefined,
): boolean {
  if (claims === null || claims === undefined) {
    return false;
  }
  const groups = claims["cognito:groups"];
  if (!Array.isArray(groups)) {
    return false;
  }
  return groups.includes(GATEWAY_ADMIN_GROUP);
}

/**
 * Read the currently signed-in user and return whether they're a gateway
 * admin. Returns `false` when no user is loaded, when the user is expired,
 * or when anything in the user-load path throws — the goal is "no admin UI
 * unless we can prove admin", not "show admin UI optimistically".
 *
 * In `VITE_DEV_BYPASS_AUTH` mode the dev token has no groups claim, so this
 * returns `false` by design; the runtime would reject admin actions anyway.
 */
export async function isGatewayAdmin(): Promise<boolean> {
  if (isDevBypassEnabled()) {
    return false;
  }
  try {
    const user = await getUser();
    if (user === null || user.expired) {
      return false;
    }
    return isGatewayAdminFromClaims(
      user.profile as Record<string, unknown> | undefined,
    );
  } catch {
    return false;
  }
}

/**
 * React hook variant. Subscribes to `UserManager` events so the answer
 * stays current across silent renews and sign-outs. Suitable for gating a
 * menu item or guarding a route component's render path.
 *
 * Mirrors the structure of `useCurrentUser` so the two stay easy to reason
 * about side-by-side.
 */
export function useIsGatewayAdmin(): boolean {
  const [isAdmin, setIsAdmin] = useState<boolean>(false);

  useEffect(() => {
    if (isDevBypassEnabled()) {
      // Dev-bypass grants full admin locally, mirroring the proxy (which stamps
      // actorGroups=["gateway-admins"] in dev-bypass). This lets the admin UI be
      // exercised against a local dev server. It is NEVER active in a deployed
      // env: the CI `build` job hard-fails if DEV_BYPASS/VITE_DEV_BYPASS is set,
      // and the real Cognito path below is the only source of truth otherwise.
      setIsAdmin(true);
      return;
    }

    let cancelled = false;

    const evaluate = (user: User | null) => {
      if (cancelled) return;
      if (user === null || user.expired) {
        setIsAdmin(false);
        return;
      }
      setIsAdmin(
        isGatewayAdminFromClaims(
          user.profile as Record<string, unknown> | undefined,
        ),
      );
    };

    void getUser().then(evaluate).catch(() => {
      if (!cancelled) setIsAdmin(false);
    });

    const handleLoaded = (u: User) => evaluate(u);
    const handleUnloaded = () => evaluate(null);

    void getUserManager().then((manager) => {
      if (cancelled) return;
      manager.events.addUserLoaded(handleLoaded);
      manager.events.addUserUnloaded(handleUnloaded);
    });

    return () => {
      cancelled = true;
      void getUserManager().then((manager) => {
        manager.events.removeUserLoaded(handleLoaded);
        manager.events.removeUserUnloaded(handleUnloaded);
      });
    };
  }, []);

  return isAdmin;
}
