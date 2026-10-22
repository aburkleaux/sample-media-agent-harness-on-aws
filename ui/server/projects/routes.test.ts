// Route tests for `/projects` (media-harness-projects-access task 6.9). The handler runs on a real
// Express app against the real `ProjectRegistry` over `InMemoryTransactStore` (DynamoDB transaction and
// condition semantics), so these exercise the registry, not a fake of it. Identity comes from a stand-in
// for `authMiddleware` that sets `req.actorId` from a test header; the last block mounts through
// `buildApp()` to cover the real `authPaths` wiring.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import express from "express";
import http from "http";
import { resetProjectsStore, setProjectsStore } from "./access";
import { InMemoryTransactStore } from "./inMemoryStore";
import { NO_SINGLE_USER, createProjectsHandler } from "./routes";
import { closeServers, listenLoopback } from "../testing/loopback";
import type { ResolveInvitee } from "./routes";

const OWNER = "sub-owner";
const MEMBER = "sub-member";
const STRANGER = "sub-stranger";

const servers: http.Server[] = [];
let store: InMemoryTransactStore;

function listen(app: express.Express): Promise<string> {
  return listenLoopback(app, servers);
}

/** An app with the projects handler behind a stand-in auth middleware (`x-test-actor` → `req.actorId`). */
function testApp(resolveInvitee?: ResolveInvitee): Promise<string> {
  const app = express();
  app.use("/projects", (req: any, _res, next) => {
    const a = req.headers["x-test-actor"];
    if (typeof a === "string") req.actorId = a;
    next();
  });
  app.use("/projects", createProjectsHandler(resolveInvitee ? { resolveInvitee } : {}));
  return listen(app);
}

/** Resolves `<name>@example.com` to `sub-<name>`; anything else matches no one. */
const fakeDirectory: ResolveInvitee = async (value) => {
  const m = /^([a-z]+)@example\.com$/i.exec(value);
  return m
    ? { ok: true, invitee: { sub: `sub-${m[1].toLowerCase()}`, display: value.toLowerCase() } }
    : { ok: false, error: NO_SINGLE_USER };
};

async function call(base: string, actor: string | null, method: string, path: string, body?: unknown) {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (actor) headers["x-test-actor"] = actor;
  const res = await fetch(`${base}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, text, json: text ? JSON.parse(text) : undefined };
}

/** Create a project as OWNER and invite MEMBER; returns its id. */
async function sharedProject(base: string, name = "Alpha"): Promise<string> {
  const created = await call(base, OWNER, "POST", "/projects", { name });
  expect(created.status).toBe(201);
  const pid = created.json.project.projectId as string;
  const added = await call(base, OWNER, "POST", `/projects/${pid}/members`, { invite: "member@example.com" });
  expect(added.status).toBe(201);
  return pid;
}

beforeEach(() => {
  store = new InMemoryTransactStore();
  setProjectsStore(store, { sleep: async () => {} });
});
afterEach(async () => {
  await closeServers(servers);
  resetProjectsStore();
  vi.restoreAllMocks();
});

describe("/projects guards", () => {
  it("401s with no actor and reads nothing from the registry", async () => {
    const base = await testApp();
    const batchGet = vi.spyOn(store, "batchGet");
    const query = vi.spyOn(store, "query");
    const r = await call(base, null, "GET", "/projects");
    expect(r.status).toBe(401);
    expect(r.json.error.code).toBe("missing_actor");
    expect(batchGet).not.toHaveBeenCalled();
    expect(query).not.toHaveBeenCalled();
  });

  it("answers every /projects* route 404 projects_not_enabled when no store is configured", async () => {
    setProjectsStore(null);
    const base = await testApp();
    for (const [method, path] of [
      ["GET", "/projects"],
      ["POST", "/projects"],
      ["GET", "/projects/p0123456789abcdef0123456789abcdef"],
      ["POST", "/projects/p0123456789abcdef0123456789abcdef/leave"],
    ]) {
      const r = await call(base, OWNER, method, path);
      expect(r.status, `${method} ${path}`).toBe(404);
      expect(r.json.error.code).toBe("projects_not_enabled");
    }
  });

  it("405s a wrong method with an Allow header, and 404s an unknown sub-path", async () => {
    const base = await testApp();
    const res = await fetch(`${base}/projects`, { method: "PUT", headers: { "x-test-actor": OWNER } });
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("GET, POST");
    const r = await call(base, OWNER, "GET", "/projects/p0123456789abcdef0123456789abcdef/nope");
    expect(r.status).toBe(404);
    expect(r.json.error.code).toBe("not_found");
  });

  it("400s invalid JSON", async () => {
    const r = await call(await testApp(), OWNER, "POST", "/projects", "{not json");
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("bad_request");
  });
});

describe("GET/POST /projects", () => {
  it("creates the Personal_Project on first list and lists it first", async () => {
    const base = await testApp();
    const first = await call(base, OWNER, "GET", "/projects");
    expect(first.status).toBe(200);
    expect(first.json.projects).toHaveLength(1);
    expect(first.json.projects[0]).toMatchObject({ name: "Personal", role: "owner", personal: true, activeAssetCount: 0 });

    expect((await call(base, OWNER, "POST", "/projects", { name: "alpha" })).status).toBe(201);
    const again = await call(base, OWNER, "GET", "/projects");
    expect(again.json.projects.map((p: any) => p.name)).toEqual(["Personal", "alpha"]);
    // ensure-Personal is idempotent: same id on the second list.
    expect(again.json.projects[0].projectId).toBe(first.json.projects[0].projectId);
  });

  it("takes the owner from req.actorId and ignores any actor id in the body", async () => {
    const base = await testApp();
    const r = await call(base, OWNER, "POST", "/projects", {
      name: "Mine",
      actorId: STRANGER,
      actor_id: STRANGER,
      ownerSub: STRANGER,
    });
    expect(r.status).toBe(201);
    expect(r.json.project.ownerSub).toBe(OWNER);
    const strangers = await call(base, STRANGER, "GET", "/projects");
    expect(strangers.json.projects.map((p: any) => p.name)).toEqual(["Personal"]);
  });

  it("refuses an invalid or duplicate name with invalid_name", async () => {
    const base = await testApp();
    expect((await call(base, OWNER, "POST", "/projects", { name: "Alpha" })).status).toBe(201);
    const dup = await call(base, OWNER, "POST", "/projects", { name: "ALPHA" });
    expect(dup.status).toBe(400);
    expect(dup.json.error.code).toBe("invalid_name");
    const missing = await call(base, OWNER, "POST", "/projects", {});
    expect(missing.status).toBe(400);
    expect(missing.json.error.code).toBe("invalid_name");
  });
});

describe("/projects/:pid", () => {
  it("returns detail to a member, and the same 404 bytes to a stranger, a malformed id, and a never-created id", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);

    const detail = await call(base, MEMBER, "GET", `/projects/${pid}`);
    expect(detail.status).toBe(200);
    expect(detail.json.role).toBe("member");
    expect(detail.json.owner.sub).toBe(OWNER);
    expect(detail.json.members.map((m: any) => m.display)).toEqual(["member@example.com"]);

    const denied = [
      await call(base, STRANGER, "GET", `/projects/${pid}`),
      await call(base, OWNER, "GET", "/projects/not-a-project-id"),
      await call(base, OWNER, "GET", "/projects/p0123456789abcdef0123456789abcdef"),
    ];
    for (const d of denied) {
      expect(d.status).toBe(404);
      expect(d.text).toBe('{"error":{"code":"project_not_found","message":"Project not found"}}');
    }
  });

  it("renames for the Owner only", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);
    const byMember = await call(base, MEMBER, "PATCH", `/projects/${pid}`, { name: "Beta" });
    expect(byMember.status).toBe(403);
    expect(byMember.json.error).toMatchObject({ code: "owner_only", action: "rename" });
    const byOwner = await call(base, OWNER, "PATCH", `/projects/${pid}`, { name: "Beta" });
    expect(byOwner.status).toBe(200);
    expect(byOwner.json.project.name).toBe("Beta");
  });

  it("POST /projects takes settings; PATCH changes description and tags (owner only) and needs a field", async () => {
    const base = await testApp(fakeDirectory);
    const created = await call(base, OWNER, "POST", "/projects", { name: "Promo", description: "Launch cuts", tags: ["q4"] });
    expect(created.status).toBe(201);
    expect(created.json.project).toMatchObject({ description: "Launch cuts", tags: ["q4"], indexing: false });
    const pid = created.json.project.projectId as string;
    expect((await call(base, OWNER, "POST", `/projects/${pid}/members`, { invite: "member@example.com" })).status).toBeLessThan(300);

    const empty = await call(base, OWNER, "PATCH", `/projects/${pid}`, {});
    expect(empty.status).toBe(400);
    const byMember = await call(base, MEMBER, "PATCH", `/projects/${pid}`, { description: "x" });
    expect(byMember.status).toBe(403);
    const both = await call(base, OWNER, "PATCH", `/projects/${pid}`, { name: "Promo 2", tags: ["a", "A", "b"] });
    expect(both.status).toBe(200);
    expect(both.json.project).toMatchObject({ name: "Promo 2", tags: ["a", "b"], description: "Launch cuts" });
    // indexing is not a PATCH field: it is turned on through POST /projects/:pid/ingestion/indexing.
    const sneaky = await call(base, OWNER, "PATCH", `/projects/${pid}`, { description: "y", indexing: true });
    expect(sneaky.json.project.indexing).toBe(false);
    const badTags = await call(base, OWNER, "PATCH", `/projects/${pid}`, { tags: "nope" });
    expect([badTags.status, badTags.json.error.code]).toEqual([400, "invalid_settings"]);
  });

  it("refuses to rename or delete the Personal_Project", async () => {
    const base = await testApp();
    const personal = (await call(base, OWNER, "GET", "/projects")).json.projects[0].projectId;
    const rename = await call(base, OWNER, "PATCH", `/projects/${personal}`, { name: "Other" });
    expect(rename.status).toBe(403);
    expect(rename.json.error.code).toBe("personal_project");
    const del = await call(base, OWNER, "DELETE", `/projects/${personal}`, { confirmName: "Personal" });
    expect(del.status).toBe(403);
    expect(del.json.error.code).toBe("personal_project");
  });

  it("deletes only when confirmName equals the name exactly", async () => {
    const base = await testApp();
    const pid = (await call(base, OWNER, "POST", "/projects", { name: "Alpha" })).json.project.projectId;
    for (const confirmName of ["alpha", "Alpha ", undefined]) {
      const r = await call(base, OWNER, "DELETE", `/projects/${pid}`, { confirmName });
      expect(r.status).toBe(400);
      expect(r.json.error.code).toBe("confirm_name_mismatch");
    }
    const ok = await call(base, OWNER, "DELETE", `/projects/${pid}`, { confirmName: "Alpha" });
    expect(ok.status).toBe(200);
    expect(ok.json.project.status).toBe("deleted");
    expect((await call(base, OWNER, "GET", `/projects/${pid}`)).status).toBe(404);
  });
});

describe("/projects/:pid/members and /leave", () => {
  it("refuses every invite with no_single_user by default (task 6.7 not wired) and writes nothing", async () => {
    const base = await testApp();
    const pid = (await call(base, OWNER, "POST", "/projects", { name: "Alpha" })).json.project.projectId;
    const before = store.committed;
    const r = await call(base, OWNER, "POST", `/projects/${pid}/members`, { invite: "member@example.com" });
    expect(r.status).toBe(400);
    expect(r.json.error).toEqual(NO_SINGLE_USER);
    expect(store.committed).toBe(before);
  });

  it("does not consult the directory for a non-owner", async () => {
    const resolver = vi.fn(fakeDirectory);
    const base = await testApp(resolver);
    const pid = await sharedProject(base);
    resolver.mockClear();
    const stranger = await call(base, STRANGER, "POST", `/projects/${pid}/members`, { invite: "x@example.com" });
    expect(stranger.status).toBe(404);
    expect(stranger.json.error.code).toBe("project_not_found");
    const member = await call(base, MEMBER, "POST", `/projects/${pid}/members`, { invite: "x@example.com" });
    expect(member.status).toBe(403);
    expect(member.json.error).toMatchObject({ code: "owner_only", action: "invite" });
    expect(resolver).not.toHaveBeenCalled();
  });

  it("adds a resolved member, refuses a duplicate and an empty value", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);
    const dup = await call(base, OWNER, "POST", `/projects/${pid}/members`, { invite: "MEMBER@example.com" });
    expect(dup.status).toBe(400);
    expect(dup.json.error.code).toBe("already_in_project");
    const empty = await call(base, OWNER, "POST", `/projects/${pid}/members`, { invite: "  " });
    expect(empty.status).toBe(400);
    expect(empty.json.error.code).toBe("invalid_invite");
    const memberList = await call(base, MEMBER, "GET", "/projects");
    expect(memberList.json.projects.map((p: any) => [p.name, p.role])).toEqual([
      ["Personal", "owner"],
      ["Alpha", "member"],
    ]);
  });

  it("removes a member (Owner only) and the Owner cannot be removed", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);
    const byMember = await call(base, MEMBER, "DELETE", `/projects/${pid}/members/${OWNER}`);
    expect(byMember.status).toBe(403);
    const owner = await call(base, OWNER, "DELETE", `/projects/${pid}/members/${OWNER}`);
    expect(owner.status).toBe(400);
    expect(owner.json.error.code).toBe("owner_cannot_leave");
    const unknown = await call(base, OWNER, "DELETE", `/projects/${pid}/members/sub-nobody`);
    expect(unknown.status).toBe(404);
    expect(unknown.json.error.code).toBe("member_not_found");
    const ok = await call(base, OWNER, "DELETE", `/projects/${pid}/members/${encodeURIComponent(MEMBER)}`);
    expect(ok.status).toBe(200);
    expect(ok.json.removed).toEqual({ sub: MEMBER });
    expect((await call(base, MEMBER, "GET", `/projects/${pid}`)).status).toBe(404);
  });

  it("lets a member leave, and refuses the Owner", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);
    const owner = await call(base, OWNER, "POST", `/projects/${pid}/leave`);
    expect(owner.status).toBe(400);
    expect(owner.json.error.code).toBe("owner_cannot_leave");
    const ok = await call(base, MEMBER, "POST", `/projects/${pid}/leave`);
    expect(ok.status).toBe(200);
    expect(ok.json.left).toEqual({ sub: MEMBER });
    expect((await call(base, MEMBER, "GET", `/projects/${pid}`)).status).toBe(404);
  });
});

describe("/projects ingestion write routes are dispatched", () => {
  it("serves capabilities to a member and the uniform 404 to a stranger", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);
    for (const actor of [OWNER, MEMBER]) {
      const r = await call(base, actor, "GET", `/projects/${pid}/ingestion/capabilities`);
      expect(r.status).toBe(200);
      expect(r.json.maxFilesPerBatch).toBe(20);
      expect(r.json.limits.document).toBe(524288000);
    }
    const denied = await call(base, STRANGER, "GET", `/projects/${pid}/ingestion/capabilities`);
    expect(denied.status).toBe(404);
    expect(denied.json.error.code).toBe("project_not_found");
    // A write route behind the same check: a stranger never reaches S3 or the Asset_Service.
    const upload = await call(base, STRANGER, "POST", `/projects/${pid}/uploads`, { files: [{ name: "a.txt", size: 1 }] });
    expect(upload.status).toBe(404);
    expect(upload.json.error.code).toBe("project_not_found");
  });

  it("still 404s an unknown ingestion subroute", async () => {
    const base = await testApp(fakeDirectory);
    const pid = await sharedProject(base);
    const r = await call(base, OWNER, "POST", `/projects/${pid}/uploads/x/parts`, {});
    expect(r.status).toBe(404);
    expect(r.json.error.code).toBe("not_found");
  });
});

describe("/projects through buildApp()", () => {
  const VARS = ["AGENT_RUNTIME_ARN", "DEV_BYPASS_AUTH", "LAB_DEFAULT_ACTOR_ID"];
  const saved: Record<string, string | undefined> = {};
  beforeEach(() => {
    for (const k of VARS) saved[k] = process.env[k];
    process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
    delete process.env.DEV_BYPASS_AUTH;
    process.env.LAB_DEFAULT_ACTOR_ID = "local-dev-user";
  });
  afterEach(() => {
    for (const k of VARS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });

  it("is behind authPaths: no token → JSON 401", async () => {
    const { buildApp } = await import("../app");
    const res = await fetch(`${await listen(buildApp())}/projects`);
    expect(res.status).toBe(401);
    expect((await res.json()).error.code).toBe("missing_token");
  });

  it("is mounted: the dev-bypass actor lists its Personal_Project", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const { buildApp } = await import("../app");
    const r = await call((await listen(buildApp())), null, "GET", "/projects");
    expect(r.status).toBe(200);
    expect(r.json.projects).toEqual([expect.objectContaining({ name: "Personal", role: "owner" })]);
  });
});
