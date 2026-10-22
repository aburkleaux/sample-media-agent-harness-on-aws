/**
 * Cognito invite resolution (media-harness-projects-access task 6.7; Req 1.9–1.11).
 *
 * A fake Cognito directory (no network): `ListUsers` with an `email = "..."` filter, then
 * `AdminGetUser` to confirm the single candidate and read `Enabled`; a non-email value goes straight to
 * `AdminGetUser` on the username. The invite proceeds only when exactly one ENABLED user matches, and the
 * MEMBER `display` is the email when matched by email, else the username.
 */
import { describe, it, expect, vi } from "vitest";
import {
  AdminGetUserCommand,
  ListUsersCommand,
} from "@aws-sdk/client-cognito-identity-provider";
import { createInviteResolver, type CognitoClientLike } from "./invite";

class UserNotFound extends Error {
  readonly name = "UserNotFoundException";
}

interface FakeUser {
  username: string;
  email?: string;
  sub: string;
  enabled: boolean;
}

/**
 * A fake pool. `ListUsers` filters by exact email, case-insensitively (as the real pool does, per the
 * design). `AdminGetUser` looks up by exact username and throws `UserNotFoundException` when absent.
 */
function fakeClient(users: FakeUser[]): { client: CognitoClientLike; sent: any[] } {
  const sent: any[] = [];
  const toListShape = (u: FakeUser) => ({
    Username: u.username,
    Enabled: u.enabled,
    Attributes: [
      { Name: "sub", Value: u.sub },
      ...(u.email ? [{ Name: "email", Value: u.email }] : []),
    ],
  });
  const toGetShape = (u: FakeUser) => ({
    Username: u.username,
    Enabled: u.enabled,
    UserAttributes: [
      { Name: "sub", Value: u.sub },
      ...(u.email ? [{ Name: "email", Value: u.email }] : []),
    ],
  });
  const client: CognitoClientLike = {
    async send(command: any) {
      sent.push(command);
      if (command instanceof ListUsersCommand) {
        const m = /^email = "(.*)"$/.exec(String(command.input.Filter ?? ""));
        const email = (m?.[1] ?? "").toLowerCase();
        const matches = users.filter((u) => (u.email ?? "").toLowerCase() === email);
        return { Users: matches.map(toListShape) };
      }
      if (command instanceof AdminGetUserCommand) {
        const u = users.find((x) => x.username === command.input.Username);
        if (!u) throw new UserNotFound("user not found");
        return toGetShape(u);
      }
      throw new Error(`unexpected command ${command?.constructor?.name}`);
    },
  };
  return { client, sent };
}

const USER_POOL_ID = "us-west-2_pool";

describe("createInviteResolver — email path", () => {
  it("resolves a single enabled user and stores the email as display", async () => {
    const { client, sent } = fakeClient([
      { username: "alice", email: "alice@example.com", sub: "sub-alice", enabled: true },
    ]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });

    const r = await resolve("alice@example.com");
    expect(r).toEqual({ ok: true, invitee: { sub: "sub-alice", display: "alice@example.com" } });
    // One ListUsers (email filter) then one AdminGetUser (confirm).
    expect(sent[0]).toBeInstanceOf(ListUsersCommand);
    expect(sent[0].input.Filter).toBe('email = "alice@example.com"');
    expect(sent[1]).toBeInstanceOf(AdminGetUserCommand);
    expect(sent[1].input.Username).toBe("alice");
  });

  it("matches case-insensitively (one query covers every case variant)", async () => {
    const { client } = fakeClient([
      { username: "alice", email: "alice@example.com", sub: "sub-alice", enabled: true },
    ]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("ALICE@Example.com");
    expect(r.ok).toBe(true);
  });

  it("refuses when no user matches the email", async () => {
    const { client } = fakeClient([]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("ghost@example.com");
    expect(r).toEqual({ ok: false, error: { code: "no_single_user", message: expect.any(String) } });
  });

  it("refuses when more than one user shares the email", async () => {
    const { client } = fakeClient([
      { username: "a", email: "dup@example.com", sub: "sub-a", enabled: true },
      { username: "b", email: "dup@example.com", sub: "sub-b", enabled: true },
    ]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("dup@example.com");
    expect(r.ok).toBe(false);
  });

  it("refuses when the single email match is disabled", async () => {
    const { client } = fakeClient([
      { username: "alice", email: "alice@example.com", sub: "sub-alice", enabled: false },
    ]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("alice@example.com");
    expect(r.ok).toBe(false);
  });
});

describe("createInviteResolver — username path", () => {
  it("resolves an enabled user by exact username and stores the username as display", async () => {
    const { client, sent } = fakeClient([
      { username: "bob", email: "bob@example.com", sub: "sub-bob", enabled: true },
    ]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("bob");
    expect(r).toEqual({ ok: true, invitee: { sub: "sub-bob", display: "bob" } });
    // No ListUsers for a non-email value — straight to AdminGetUser.
    expect(sent).toHaveLength(1);
    expect(sent[0]).toBeInstanceOf(AdminGetUserCommand);
  });

  it("refuses an unknown username (UserNotFoundException)", async () => {
    const { client } = fakeClient([]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("nobody");
    expect(r.ok).toBe(false);
  });

  it("refuses a disabled user matched by username", async () => {
    const { client } = fakeClient([
      { username: "bob", sub: "sub-bob", enabled: false },
    ]);
    const resolve = createInviteResolver({ client, userPoolId: USER_POOL_ID });
    const r = await resolve("bob");
    expect(r.ok).toBe(false);
  });
});

describe("createInviteResolver — guards", () => {
  it("refuses an empty value without touching the directory", async () => {
    const send = vi.fn();
    const resolve = createInviteResolver({ client: { send }, userPoolId: USER_POOL_ID });
    const r = await resolve("   ");
    expect(r.ok).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it("fails closed to no_single_user on a transport error", async () => {
    const resolve = createInviteResolver({
      client: { send: async () => { throw new Error("throttled"); } },
      userPoolId: USER_POOL_ID,
    });
    const r = await resolve("alice@example.com");
    expect(r.ok).toBe(false);
  });
});
