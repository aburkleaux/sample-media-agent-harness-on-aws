/**
 * Invite resolution against Cognito (media-harness-projects-access task 6.7; design Component 1
 * "Invite lookup", Req 1.9–1.11).
 *
 * `POST /projects/:pid/members {invite}` carries an email or a username. This module turns that value
 * into exactly one enabled Harness user, or the uniform "no single Harness user matches" refusal:
 *
 *   * A value containing `@` is an email. One `ListUsers` with filter `email = "<value>"` finds it;
 *     that filter is case-insensitive on the pool, so one query covers every case variant. `ListUsers`
 *     is eventually consistent, so the single candidate is then confirmed with `AdminGetUser`, which
 *     also yields the authoritative `Enabled` flag. More or fewer than one candidate is a refusal.
 *   * Any other value is a username, resolved by `AdminGetUser` on the exact string. A
 *     `UserNotFoundException` (or any error) is a refusal.
 *
 * The invite proceeds only when exactly one ENABLED user matches. The MEMBER item stores `display`
 * (the email when matched by email, else the username). Membership refusals ("already in the
 * Project", Owner-only, not-found) are enforced by the route's `checkAccess` pre-check and by
 * `ProjectRegistry.invite`'s own transaction (`attribute_not_exists` on the MEMBER item), so this
 * resolver is purely the directory lookup.
 *
 * The Cognito client is injected (`InviteResolverDeps.client`) so tests drive a fake directory with no
 * network. {@link defaultInviteResolver} builds the real client lazily from the environment and is the
 * resolver `routes.ts` installs by default; when `USER_POOL_ID` is unset (local dev, or an environment
 * without Projects) it refuses every value, exactly as the pre-6.7 stub did.
 */
import {
  AdminGetUserCommand,
  CognitoIdentityProviderClient,
  ListUsersCommand,
} from "@aws-sdk/client-cognito-identity-provider";

import { NO_SINGLE_USER } from "./routes";
import type { InviteResolution, ResolveInvitee } from "./routes";

/** The slice of the Cognito client this module uses. A test passes a fake with the same `send`. */
export interface CognitoClientLike {
  send(command: any): Promise<any>;
}

export interface InviteResolverDeps {
  client: CognitoClientLike;
  userPoolId: string;
}

/** Either Cognito shape of a user's attribute list: `Attributes` (ListUsers) or `UserAttributes` (AdminGetUser). */
interface CognitoUserShape {
  Username?: string;
  Enabled?: boolean;
  Attributes?: Array<{ Name?: string; Value?: string }>;
  UserAttributes?: Array<{ Name?: string; Value?: string }>;
}

const refusal = (): InviteResolution => ({ ok: false, error: NO_SINGLE_USER });

function attribute(user: CognitoUserShape, name: string): string | undefined {
  const list = user.Attributes ?? user.UserAttributes ?? [];
  for (const a of list) if (a?.Name === name) return a?.Value;
  return undefined;
}

/** Build the resolution from a confirmed user: one enabled user with a `sub` passes, anything else refuses. */
function resolutionFor(user: CognitoUserShape, display: string): InviteResolution {
  const sub = attribute(user, "sub");
  if (!sub || user.Enabled !== true) return refusal();
  return { ok: true, invitee: { sub, display } };
}

/** Cognito's `ListUsers` filter needs the value quoted; escape any embedded quote. */
function emailFilter(email: string): string {
  return `email = "${email.replace(/"/g, '\\"')}"`;
}

export function createInviteResolver(deps: InviteResolverDeps): ResolveInvitee {
  const { client, userPoolId } = deps;

  async function byEmail(email: string): Promise<InviteResolution> {
    // Limit 2 is enough to tell "exactly one" from "more than one"; both of the latter refuse.
    const listed = await client.send(
      new ListUsersCommand({ UserPoolId: userPoolId, Filter: emailFilter(email), Limit: 2 }),
    );
    const users: CognitoUserShape[] = Array.isArray(listed?.Users) ? listed.Users : [];
    if (users.length !== 1) return refusal();
    const username = users[0]?.Username;
    if (!username) return refusal();
    // Confirm the eventually-consistent list hit, and read the authoritative Enabled flag.
    const got = await client.send(new AdminGetUserCommand({ UserPoolId: userPoolId, Username: username }));
    return resolutionFor(got as CognitoUserShape, email);
  }

  async function byUsername(username: string): Promise<InviteResolution> {
    const got = await client.send(new AdminGetUserCommand({ UserPoolId: userPoolId, Username: username }));
    return resolutionFor(got as CognitoUserShape, username);
  }

  return async function resolveInvitee(value: string): Promise<InviteResolution> {
    const trimmed = value.trim();
    if (!trimmed) return refusal();
    try {
      return trimmed.includes("@") ? await byEmail(trimmed) : await byUsername(trimmed);
    } catch {
      // UserNotFoundException, a throttle, or any transport failure: fail closed to the uniform refusal.
      return refusal();
    }
  };
}

// ------------------------------------------------------------------------------------------ default (env)

let cachedResolver: ResolveInvitee | null = null;

/**
 * The resolver `routes.ts` installs when the caller passes none. Lazily reads `USER_POOL_ID` and builds
 * one Cognito client per process; with no pool configured it refuses every value, matching the behaviour
 * before task 6.7 so tests and Projects-disabled environments are unaffected.
 */
export const defaultInviteResolver: ResolveInvitee = async (value) => {
  if (!cachedResolver) {
    const userPoolId = process.env.USER_POOL_ID || "";
    if (!userPoolId) {
      cachedResolver = async () => refusal();
    } else {
      const client = new CognitoIdentityProviderClient({ region: process.env.AWS_REGION || "us-west-2" });
      cachedResolver = createInviteResolver({ client, userPoolId });
    }
  }
  return cachedResolver(value);
};

/** Test-only reset of the memoized default resolver. */
export function __resetDefaultInviteResolverForTests(): void {
  cachedResolver = null;
}
