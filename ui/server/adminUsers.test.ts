// Admin (gateway-admins) surface: /admin/users directory (D3) + grant/revoke admin (D1),
// gated by requireAdmin (D2). Reuses the existing gateway-admins group + actorGroups — no new
// claim. Cognito + validateJwt are mocked so this is deterministic and offline.
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import http from "http";

const h = vi.hoisted(() => ({
  sent: [] as any[],
  listUsersResult: { Users: [] as any[] },
  adminMembers: { Users: [] as any[] },
  jwtGroups: [] as string[],
}));

vi.mock("@aws-sdk/client-cognito-identity-provider", () => {
  class CognitoIdentityProviderClient {
    async send(cmd: any) {
      h.sent.push(cmd);
      if (cmd.__kind === "ListUsers") return h.listUsersResult;
      if (cmd.__kind === "ListUsersInGroup") return h.adminMembers;
      return {};
    }
  }
  class ListUsersCommand { __kind = "ListUsers"; constructor(public input: any) {} }
  class ListUsersInGroupCommand { __kind = "ListUsersInGroup"; constructor(public input: any) {} }
  class AdminAddUserToGroupCommand { __kind = "AddToGroup"; constructor(public input: any) {} }
  class AdminRemoveUserFromGroupCommand { __kind = "RemoveFromGroup"; constructor(public input: any) {} }
  class AdminResetUserPasswordCommand { __kind = "ResetPassword"; constructor(public input: any) {} }
  class AdminDeleteUserCommand { __kind = "DeleteUser"; constructor(public input: any) {} }
  class AdminCreateUserCommand { __kind = "CreateUser"; constructor(public input: any) {} }
  class AdminDisableUserCommand { __kind = "DisableUser"; constructor(public input: any) {} }
  class AdminEnableUserCommand { __kind = "EnableUser"; constructor(public input: any) {} }
  return { CognitoIdentityProviderClient, ListUsersCommand, ListUsersInGroupCommand, AdminAddUserToGroupCommand, AdminRemoveUserFromGroupCommand, AdminResetUserPasswordCommand, AdminDeleteUserCommand, AdminCreateUserCommand, AdminDisableUserCommand, AdminEnableUserCommand };
});

// Mock JWT validation so real-auth mode yields a chosen group set without a real token/JWKS.
vi.mock("../auth/validateJwt", () => ({
  validateJwt: async (_token: string) => ({ sub: "test-user", groups: h.jwtGroups }),
}));

import { buildApp, hasAdminGroup, ADMIN_GROUP } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const VARS = [
  "AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH",
  "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID", "USER_POOL_ID", "AWS_REGION",
];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.USER_POOL_ID = "us-west-2_testpool";
  process.env.AWS_REGION = "us-west-2";
  h.sent.length = 0;
  h.listUsersResult = { Users: [] };
  h.adminMembers = { Users: [] };
  h.jwtGroups = [];
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

describe("admin gate — hasAdminGroup (unit)", () => {
  it("is true only when the gateway-admins group is present", () => {
    expect(ADMIN_GROUP).toBe("gateway-admins");
    expect(hasAdminGroup(["gateway-admins"])).toBe(true);
    expect(hasAdminGroup(["x", "gateway-admins", "y"])).toBe(true);
    expect(hasAdminGroup([])).toBe(false);
    expect(hasAdminGroup(["editor"])).toBe(false);
    expect(hasAdminGroup(undefined)).toBe(false);
    expect(hasAdminGroup(null)).toBe(false);
  });
});

describe("/admin — requireAdmin gate (D2)", () => {
  it("403s an authenticated NON-admin user (real auth, no gateway-admins group)", async () => {
    process.env.DEV_BYPASS_AUTH = "false";
    process.env.USER_POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test";
    process.env.USER_POOL_CLIENT_ID = "testclient";
    h.jwtGroups = []; // authenticated, but not an admin
    const base = await listen();
    const r = await fetch(`${base}/admin/users`, { headers: { Authorization: "Bearer faketoken" } });
    expect(r.status).toBe(403);
    const body = await r.json() as any;
    expect(body?.error?.code).toBe("forbidden");
    // The gate ran BEFORE any Cognito call.
    expect(h.sent).toHaveLength(0);
  });

  it("401s (not 403) when unauthenticated — auth middleware short-circuits first", async () => {
    process.env.DEV_BYPASS_AUTH = "false";
    process.env.USER_POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test";
    process.env.USER_POOL_CLIENT_ID = "testclient";
    const base = await listen();
    const r = await fetch(`${base}/admin/users`); // no token
    expect(r.status).toBe(401);
  });
});

describe("/admin/users — directory (D3) + grant/revoke (D1), admin allowed", () => {
  it("GET /admin/users returns the mapped Cognito directory with admin flags", async () => {
    process.env.DEV_BYPASS_AUTH = "true"; // dev bypass grants gateway-admins
    h.listUsersResult = {
      Users: [
        {
          Username: "joe",
          UserStatus: "CONFIRMED",
          Enabled: true,
          UserCreateDate: new Date("2024-01-01T00:00:00Z"),
          Attributes: [{ Name: "sub", Value: "s-joe" }, { Name: "email", Value: "joe@example.com" }],
        },
        {
          Username: "ann",
          UserStatus: "CONFIRMED",
          Enabled: true,
          Attributes: [{ Name: "sub", Value: "s-ann" }, { Name: "email", Value: "ann@example.com" }],
        },
      ],
    };
    h.adminMembers = { Users: [{ Username: "joe" }] }; // joe is a gateway-admin, ann is not
    const base = await listen();
    const r = await fetch(`${base}/admin/users?limit=25`);
    expect(r.status).toBe(200);
    const body = await r.json() as any;
    expect(body.users).toHaveLength(2);
    const joe = body.users.find((u: any) => u.username === "joe");
    const ann = body.users.find((u: any) => u.username === "ann");
    expect(joe).toMatchObject({ sub: "s-joe", email: "joe@example.com", status: "CONFIRMED", enabled: true, admin: true });
    expect(ann).toMatchObject({ sub: "s-ann", email: "ann@example.com", admin: false });
    const lu = h.sent.find((c) => c.__kind === "ListUsers");
    expect(lu.input.UserPoolId).toBe("us-west-2_testpool");
    expect(lu.input.Limit).toBe(25);
    const grp = h.sent.find((c) => c.__kind === "ListUsersInGroup");
    expect(grp.input).toMatchObject({ UserPoolId: "us-west-2_testpool", GroupName: "gateway-admins" });
  });

  it("POST /admin/users/:username/admin grants gateway-admins", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users/joe/admin`, { method: "POST" });
    expect(r.status).toBe(200);
    const body = await r.json() as any;
    expect(body).toMatchObject({ ok: true, username: "joe", admin: true });
    const add = h.sent.find((c) => c.__kind === "AddToGroup");
    expect(add.input).toMatchObject({ UserPoolId: "us-west-2_testpool", Username: "joe", GroupName: "gateway-admins" });
  });

  it("DELETE /admin/users/:username/admin revokes gateway-admins", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users/joe/admin`, { method: "DELETE" });
    expect(r.status).toBe(200);
    const body = await r.json() as any;
    expect(body).toMatchObject({ ok: true, username: "joe", admin: false });
    const rem = h.sent.find((c) => c.__kind === "RemoveFromGroup");
    expect(rem.input).toMatchObject({ Username: "joe", GroupName: "gateway-admins" });
  });

  it("URL-decodes the username (emails as Cognito usernames)", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users/${encodeURIComponent("a@b.com")}/admin`, { method: "POST" });
    expect(r.status).toBe(200);
    const add = h.sent.find((c) => c.__kind === "AddToGroup");
    expect(add.input.Username).toBe("a@b.com");
  });

  it("404s an unknown admin subroute (still behind the admin gate)", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/bogus`);
    expect(r.status).toBe(404);
  });

  it("POST /admin/users/:username/reset-password triggers a Cognito reset", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users/joe/reset-password`, { method: "POST" });
    expect(r.status).toBe(200);
    expect(await r.json()).toMatchObject({ ok: true, username: "joe", reset: true });
    const cmd = h.sent.find((c) => c.__kind === "ResetPassword");
    expect(cmd.input).toMatchObject({ UserPoolId: "us-west-2_testpool", Username: "joe" });
  });

  it("DELETE /admin/users/:username deletes the user (not shadowed by revoke)", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users/joe`, { method: "DELETE" });
    expect(r.status).toBe(200);
    expect(await r.json()).toMatchObject({ ok: true, username: "joe", deleted: true });
    const del = h.sent.find((c) => c.__kind === "DeleteUser");
    expect(del.input).toMatchObject({ UserPoolId: "us-west-2_testpool", Username: "joe" });
    // The revoke route (/users/:u/admin DELETE) must NOT have fired for a bare-user DELETE.
    expect(h.sent.find((c) => c.__kind === "RemoveFromGroup")).toBeUndefined();
  });

  it("DELETE /admin/users/:username/admin still revokes (bare-delete regex doesn't hijack it)", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users/joe/admin`, { method: "DELETE" });
    expect(r.status).toBe(200);
    expect(await r.json()).toMatchObject({ ok: true, admin: false });
    expect(h.sent.find((c) => c.__kind === "RemoveFromGroup")).toBeTruthy();
    expect(h.sent.find((c) => c.__kind === "DeleteUser")).toBeUndefined();
  });

  it("POST /admin/users creates a user with email + name attributes", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "new@example.com", firstName: "New", lastName: "User" }),
    });
    expect(r.status).toBe(200);
    expect(await r.json()).toMatchObject({ ok: true, username: "new@example.com", created: true });
    const cmd = h.sent.find((c) => c.__kind === "CreateUser");
    expect(cmd.input.Username).toBe("new@example.com");
    const attrs = Object.fromEntries((cmd.input.UserAttributes ?? []).map((a: any) => [a.Name, a.Value]));
    expect(attrs).toMatchObject({ email: "new@example.com", given_name: "New", family_name: "User" });
  });

  it("POST /admin/users rejects a missing/invalid email with 400 (no Cognito call)", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const r = await fetch(`${base}/admin/users`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: "nope" }),
    });
    expect(r.status).toBe(400);
    expect(h.sent.find((c) => c.__kind === "CreateUser")).toBeUndefined();
  });

  it("POST /admin/users/:u/disable then /enable — soft delete + undo", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const base = await listen();
    const d = await fetch(`${base}/admin/users/joe/disable`, { method: "POST" });
    expect(d.status).toBe(200);
    expect(await d.json()).toMatchObject({ ok: true, username: "joe", enabled: false });
    expect(h.sent.find((c) => c.__kind === "DisableUser")?.input).toMatchObject({ Username: "joe" });
    const e = await fetch(`${base}/admin/users/joe/enable`, { method: "POST" });
    expect(e.status).toBe(200);
    expect(await e.json()).toMatchObject({ ok: true, username: "joe", enabled: true });
    expect(h.sent.find((c) => c.__kind === "EnableUser")?.input).toMatchObject({ Username: "joe" });
  });
});
