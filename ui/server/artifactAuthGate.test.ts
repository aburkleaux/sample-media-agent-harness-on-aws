// /api/artifact under ARTIFACTS_REQUIRE_AUTH (the `artifactsCdnPrivate` env flag, staging first).
//
// Gated mode: `/api/artifact` is dropped from PUBLIC_PATHS, so the auth middleware runs (Bearer
// header only; a `?access_token=` query is ignored) and the handler applies the shared artifact-key policy. Refusals are 400 — never
// 403/404, which the UI CloudFront rewrites to 200 /index.html.
// Ungated mode (env unset): the public read-through is unchanged — no token, no ownership lookup.
//
// validateJwt, DynamoDB (session-row ownership) and S3 are mocked: deterministic and offline.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

const h = vi.hoisted(() => ({
  ddbSend: vi.fn(),
  s3Send: vi.fn(),
}));

// A token's value IS its subject; "bad" fails validation.
vi.mock("../auth/validateJwt", () => ({
  validateJwt: async (token: string) => {
    if (token === "bad") throw new Error("invalid signature");
    return { sub: token, groups: [] };
  },
}));

vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    DynamoDBDocumentClient: { from: () => ({ send: h.ddbSend }) },
    GetCommand: class extends Cmd { constructor(i: any) { super(i, "Get"); } },
    DeleteCommand: class extends Cmd { constructor(i: any) { super(i, "Delete"); } },
    QueryCommand: class extends Cmd { constructor(i: any) { super(i, "Query"); } },
    PutCommand: class extends Cmd { constructor(i: any) { super(i, "Put"); } },
    UpdateCommand: class extends Cmd { constructor(i: any) { super(i, "Update"); } },
  };
});

vi.mock("@aws-sdk/client-s3", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    S3Client: class { send = h.s3Send; },
    ListObjectsV2Command: class extends Cmd { constructor(i: any) { super(i, "List"); } },
    GetObjectCommand: class extends Cmd { constructor(i: any) { super(i, "GetObj"); } },
    PutObjectCommand: class extends Cmd { constructor(i: any) { super(i, "PutObj"); } },
    DeleteObjectCommand: class extends Cmd { constructor(i: any) { super(i, "DelObj"); } },
  };
});

vi.mock("@aws-sdk/s3-request-presigner", () => ({
  getSignedUrl: async (_c: any, cmd: any) =>
    `https://s3.example/${cmd.input.Key}?X-Amz-Signature=sig` +
    (cmd.input.ResponseContentDisposition ? `&rcd=${encodeURIComponent(cmd.input.ResponseContentDisposition)}` : ""),
}));

import { buildApp } from "./app";
import { artifactOwnerTag } from "./artifactOwner";
import { closeServers, listenLoopback } from "./testing/loopback";

const OWNER = "actor-1";
const BODY = "artifact-bytes";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = [
  "AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID",
  "SESSIONS_TABLE", "ARTIFACTS_REQUIRE_AUTH",
];

/** Session rows: sess-own → OWNER, sess-other → someone else, sess-noactor → row without actorId. */
function ddbRows(cmd: any) {
  const sid = cmd.input?.Key?.sessionId;
  if (sid === "sess-own") return { Item: { sessionId: sid, actorId: OWNER } };
  if (sid === "sess-other") return { Item: { sessionId: sid, actorId: "someone-else" } };
  if (sid === "sess-noactor") return { Item: { sessionId: sid } };
  if (sid === "sess-ddb-down") throw new Error("ddb down");
  return {};
}

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "false";
  process.env.USER_POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test";
  process.env.USER_POOL_CLIENT_ID = "testclient";
  delete process.env.ARTIFACTS_REQUIRE_AUTH;
  h.ddbSend.mockReset();
  h.ddbSend.mockImplementation(async (cmd: any) => ddbRows(cmd));
  h.s3Send.mockReset();
  h.s3Send.mockImplementation(async (cmd: any) => {
    if (cmd.__type === "GetObj") {
      return {
        ContentType: "video/mp4",
        ContentLength: BODY.length,
        Body: { transformToByteArray: async () => new TextEncoder().encode(BODY) },
      };
    }
    return {};
  });
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

const art = (base: string, key: string, init?: RequestInit, extra = "") =>
  fetch(`${base}/api/artifact?key=${encodeURIComponent(key)}${extra}`, init);
const bearer = (t: string) => ({ headers: { authorization: `Bearer ${t}` } });

describe("/api/artifact — ARTIFACTS_REQUIRE_AUTH=true (gated)", () => {
  beforeEach(() => { process.env.ARTIFACTS_REQUIRE_AUTH = "true"; });

  it("401s without a token, from the auth middleware, and never reaches S3", async () => {
    const r = await art(await listen(), "artifacts/sess-own/output/a.mp4");
    expect(r.status).toBe(401);
    const body = (await r.json()) as any;
    expect(body.error.message).toBe("Authorization header is required");
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("401s an invalid token", async () => {
    const r = await art(await listen(), "artifacts/sess-own/output/a.mp4", bearer("bad"));
    expect(r.status).toBe(401);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("streams an owned artifact with a Bearer token", async () => {
    const r = await art(await listen(), "artifacts/sess-own/output/a.mp4", bearer(OWNER));
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toBe("video/mp4");
    expect(await r.text()).toBe(BODY);
  });

  it("IGNORES a ?access_token= query: a token in a URL is never a credential (view tickets replace it)", async () => {
    // A valid token, in the query only: unauthenticated, 401, S3 never read.
    const r = await art(await listen(), "artifacts/sess-own/output/a.mp4", undefined, `&access_token=${OWNER}`);
    expect(r.status).toBe(401);
    expect(((await r.json()) as any).error.code).toBe("missing_token");
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("IGNORES ?access_token= on every protected route, not only /api/artifact", async () => {
    const base = await listen();
    for (const p of ["/jobs/stream", "/sessions", "/api/artifact-urls"]) {
      const r = await fetch(`${base}${p}?access_token=${OWNER}`);
      expect(r.status, p).toBe(401);
      await r.text();
    }
  });

  it("400s (not 403/404) another owner's session artifact, without reading S3", async () => {
    const r = await art(await listen(), "artifacts/sess-other/output/a.mp4", bearer(OWNER));
    expect(r.status).toBe(400);
    expect(r.headers.get("content-type")).toContain("application/json");
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("checks the normalized key: a doubled artifacts/ prefix cannot dodge the owner check", async () => {
    const r = await art(await listen(), "artifacts/artifacts/sess-other/output/a.mp4", bearer(OWNER));
    expect(r.status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("allows the shared namespaces and the caller's OWN tagged upload", async () => {
    const base = await listen();
    for (const [k, who] of [
      ["artifacts/durable/h.mp4", "anyone"],
      ["artifacts/external/h.png", "anyone"],
      [`artifacts/uploads/${artifactOwnerTag(OWNER)}/input/1-u.mov`, OWNER],
    ]) {
      const r = await art(base, k, bearer(who));
      expect(r.status, k).toBe(200);
      await r.text();
    }
  });

  it("400s, without reading S3, what nothing attributes to the caller: missing rows, rows without actorId, flat keys, legacy uploads, another user's upload", async () => {
    // These were readable by EVERY signed-in user. Projects are off here, so no project can vouch.
    const base = await listen();
    for (const k of [
      "artifacts/no-such-session/output/a.mp4",
      "artifacts/sess-noactor/output/a.mp4",
      "artifacts/flat.mp4",
      "artifacts/uploads/input/u.mov",
      `artifacts/uploads/${artifactOwnerTag("someone-else")}/input/1-u.mov`,
    ]) {
      const r = await art(base, k, bearer(OWNER));
      expect(r.status, k).toBe(400);
      await r.text();
    }
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("fails closed (400) when the ownership lookup errors", async () => {
    const r = await art(await listen(), "artifacts/sess-ddb-down/output/a.mp4", bearer(OWNER));
    expect(r.status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("still refuses traversal and non-artifact keys with 400", async () => {
    const base = await listen();
    for (const k of ["secrets/creds.json", "artifacts/sess-own/../x"]) {
      const r = await art(base, k, bearer(OWNER));
      expect(r.status, k).toBe(400);
    }
    expect(h.s3Send).not.toHaveBeenCalled();
  });
});

describe("/api/artifact — ARTIFACTS_REQUIRE_AUTH unset (public, unchanged)", () => {
  it("streams without a token and performs no ownership lookup, even for another owner's session", async () => {
    const r = await art(await listen(), "artifacts/sess-other/output/a.mp4");
    expect(r.status).toBe(200);
    expect(await r.text()).toBe(BODY);
    expect(h.ddbSend).not.toHaveBeenCalled();
  });

  it("treats any value other than \"true\" as unset", async () => {
    process.env.ARTIFACTS_REQUIRE_AUTH = "1";
    const r = await art(await listen(), "artifacts/sess-own/output/a.mp4");
    expect(r.status).toBe(200);
    await r.text();
  });
});

describe("POST /api/artifact-urls — wired to the session-row policy", () => {
  const post = (base: string, body: unknown, token = OWNER) =>
    fetch(`${base}/api/artifact-urls`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify(body),
    });

  it("signs owned + shared keys, refuses another owner's, and honours download", async () => {
    const r = await post(await listen(), {
      keys: ["artifacts/sess-own/output/a.mp4", "artifacts/durable/d.mp4", "artifacts/sess-other/output/b.mp4", "nope/x"],
      download: true,
    });
    expect(r.status).toBe(200);
    const body = (await r.json()) as any;
    expect(Object.keys(body.urls).sort()).toEqual(["artifacts/durable/d.mp4", "artifacts/sess-own/output/a.mp4"]);
    expect(body.urls["artifacts/sess-own/output/a.mp4"].url).toContain(
      encodeURIComponent('attachment; filename="a.mp4"'),
    );
    expect(typeof body.urls["artifacts/sess-own/output/a.mp4"].expiresAt).toBe("number");
    expect(body.errors).toEqual({
      "artifacts/sess-other/output/b.mp4": { code: "not_found" },
      "nope/x": { code: "not_resolvable" },
    });
  });

  it("is authenticated in both modes", async () => {
    const r = await fetch(`${await listen()}/api/artifact-urls`, { method: "POST", body: "{\"keys\":[]}" });
    expect(r.status).toBe(401);
  });
});

describe("HTML documents — view tickets, inner-link re-signing, and the sandbox header", () => {
  // The storyboard (session b42731df): agent-written HTML whose <img> links were presigned URLs
  // baked in at generation time, expiring hours later. The page must load through /api/artifact
  // (which re-signs them), from a URL that does NOT carry the user's login token — an
  // agent-written page can read its own location.href.
  const PAGE = "artifacts/sess-own/output/story.html";
  const MINE = "artifacts/sess-own/output/panel1.jpg";
  const THEIRS = "artifacts/sess-other/output/private.png";
  const HTML =
    `<html><body><img src="https://test-bucket.s3.amazonaws.com/${MINE}?X-Amz-Expires=1&X-Amz-Signature=old">` +
    `<img src="https://d1.cloudfront.net/${THEIRS}"></body></html>`;

  beforeEach(() => {
    process.env.ARTIFACTS_REQUIRE_AUTH = "true";
    h.s3Send.mockImplementation(async (cmd: any) => {
      if (cmd.__type === "GetObj") {
        const isHtml = String(cmd.input.Key).endsWith(".html");
        const body = isHtml ? HTML : BODY;
        return {
          ContentType: isHtml ? "text/html" : "video/mp4",
          ContentLength: body.length,
          Body: { transformToByteArray: async () => new TextEncoder().encode(body) },
        };
      }
      return {};
    });
  });

  const resolve = async (base: string, keys: string[]) => {
    const r = await fetch(`${base}/api/artifact-urls`, {
      method: "POST",
      headers: { authorization: `Bearer ${OWNER}`, "content-type": "application/json" },
      body: JSON.stringify({ keys }),
    });
    return ((await r.json()) as any).urls as Record<string, { url: string }>;
  };

  it("resolves an HTML key to a same-origin ticket URL — no login token, no raw S3 URL", async () => {
    const base = await listen();
    const urls = await resolve(base, [PAGE, "artifacts/sess-own/output/a.mp4"]);
    const page = urls[PAGE].url;
    expect(page.startsWith(`/api/artifact?key=${encodeURIComponent(PAGE)}&view=`)).toBe(true);
    expect(page).not.toContain("access_token");
    expect(page).not.toContain("X-Amz-Signature");
    // Everything else is unchanged: media still resolves to a presigned S3 URL.
    expect(urls["artifacts/sess-own/output/a.mp4"].url).toContain("X-Amz-Signature");
  });

  it("opens the page with ONLY the ticket, re-signs links the viewer owns, leaves others untouched", async () => {
    const base = await listen();
    const page = (await resolve(base, [PAGE]))[PAGE].url;
    const r = await fetch(`${base}${page}`); // no Authorization header, no access_token
    expect(r.status).toBe(200);
    const html = await r.text();
    expect(html).toContain(`https://s3.example/${MINE}?X-Amz-Signature=sig`);
    expect(html).not.toContain("X-Amz-Signature=old");
    expect(html).toContain(`https://d1.cloudfront.net/${THEIRS}`); // not the viewer's: as authored
    expect(html).not.toContain(`s3.example/${THEIRS}`);
  });

  it("serves the page sandboxed, so its scripts cannot act as the signed-in app", async () => {
    const base = await listen();
    const page = (await resolve(base, [PAGE]))[PAGE].url;
    const r = await fetch(`${base}${page}`);
    const csp = r.headers.get("content-security-policy") ?? "";
    expect(csp.startsWith("sandbox")).toBe(true);
    expect(csp).toContain("allow-scripts"); // interactive pages keep working
    expect(csp).not.toContain("allow-same-origin"); // …but get an opaque origin
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
    await r.text();
    // EVERY response is isolated now, media included: the stored type is writer-controlled, so
    // "it says video" is not evidence that a browser will not render it as a document.
    const m = await art(base, "artifacts/sess-own/output/a.mp4", bearer(OWNER));
    expect(m.headers.get("content-security-policy")).toContain("sandbox");
    expect(m.headers.get("x-content-type-options")).toBe("nosniff");
    await m.text();
  });

  it("a ticket opens ONLY its own document", async () => {
    const base = await listen();
    const page = (await resolve(base, [PAGE]))[PAGE].url;
    const ticket = new URLSearchParams(page.split("?")[1]).get("view")!;
    const r = await art(base, "artifacts/sess-own/output/a.mp4", undefined, `&view=${encodeURIComponent(ticket)}`);
    expect(r.status).toBe(401);
    expect(h.s3Send).not.toHaveBeenCalledWith(expect.objectContaining({ input: expect.objectContaining({ Key: "artifacts/sess-own/output/a.mp4" }) }));
  });

  it("a forged or tampered ticket is refused before S3", async () => {
    const base = await listen();
    const page = (await resolve(base, [PAGE]))[PAGE].url;
    const ticket = new URLSearchParams(page.split("?")[1]).get("view")!;
    const [v, exp, actor, sig] = ticket.split(".");
    const asOther = [v, exp, Buffer.from("someone-else").toString("base64url"), sig].join(".");
    for (const bad of ["v1.9999999999.YQ.AAAA", asOther, `${ticket}x`]) {
      const r = await art(base, PAGE, undefined, `&view=${encodeURIComponent(bad)}`);
      expect(r.status, bad).toBe(401);
    }
    void actor;
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("a ticket for another owner's page still cannot read it (ownership is re-checked)", async () => {
    // Even a VALID ticket is only as good as its viewer: it is re-authorized on every read. The
    // resolver refuses to mint one, and a hand-minted (genuinely signed) one is still refused.
    process.env.ARTIFACT_VIEW_SECRET = "test-secret";
    try {
      const base = await listen();
      const other = "artifacts/sess-other/output/x.html";
      expect((await resolve(base, [other]))[other]).toBeUndefined();
      const { mintViewTicket } = await import("./artifacts/viewTicket");
      const valid = mintViewTicket(other, OWNER, "test-secret").ticket;
      const r = await art(base, other, undefined, `&view=${encodeURIComponent(valid)}`);
      expect(r.status).toBe(400);
      expect(h.s3Send).not.toHaveBeenCalled();
    } finally {
      delete process.env.ARTIFACT_VIEW_SECRET;
    }
  });
});

describe("finding #5 — frame tickets for EVERY document type, and isolation on EVERY response", () => {
  // The stored type is writer-controlled: each object below is served with the type its S3 row
  // claims, and the proxy must police it.
  const TYPES: Record<string, string> = {
    "artifacts/sess-own/output/r.pdf": "application/pdf",
    "artifacts/sess-own/output/n.xml": "text/xml",
    "artifacts/sess-own/output/x.js": "application/javascript",
    "artifacts/sess-own/output/a.zip": "application/zip",
    "artifacts/sess-own/output/a.mp4": "video/mp4",
    "artifacts/sess-own/output/page.pdf": "text/html", // a ".pdf" whose stored type is really HTML
  };
  beforeEach(() => {
    process.env.ARTIFACTS_REQUIRE_AUTH = "true";
    h.s3Send.mockImplementation(async (cmd: any) => {
      if (cmd.__type === "GetObj") {
        return {
          ContentType: TYPES[cmd.input.Key] ?? "application/octet-stream",
          ContentLength: BODY.length,
          Body: { transformToByteArray: async () => new TextEncoder().encode(BODY) },
        };
      }
      return {};
    });
  });
  const resolveFrame = async (base: string, keys: string[]) => {
    const r = await fetch(`${base}/api/artifact-urls`, {
      method: "POST",
      headers: { authorization: `Bearer ${OWNER}`, "content-type": "application/json" },
      body: JSON.stringify({ keys, frame: true }),
    });
    return ((await r.json()) as any).urls as Record<string, { url: string; expiresAt: number }>;
  };

  it("{frame:true} resolves EVERY key (PDF, XML, media) to a same-origin ticket URL — never a login token", async () => {
    const base = await listen();
    const keys = Object.keys(TYPES);
    const urls = await resolveFrame(base, keys);
    for (const k of keys) {
      expect(urls[k].url.startsWith(`/api/artifact?key=${encodeURIComponent(k)}&view=v1.`), k).toBe(true);
      expect(urls[k].url).not.toContain("access_token");
      expect(urls[k].expiresAt).toBeGreaterThan(Date.now() / 1000);
    }
  });

  it("a frame ticket opens its PDF with no Authorization header: inline, nosniff, and NO sandbox (Chromium blocks sandboxed PDFs)", async () => {
    const base = await listen();
    const k = "artifacts/sess-own/output/r.pdf";
    const r = await fetch(`${base}${(await resolveFrame(base, [k]))[k].url}`);
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toBe("application/pdf");
    expect(r.headers.get("content-disposition")).toMatch(/^inline;/);
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
    expect(r.headers.get("content-security-policy")).toBeNull();
    await r.text();
  });

  it("a frame ticket is valid for its ONE key only", async () => {
    const base = await listen();
    const k = "artifacts/sess-own/output/r.pdf";
    const ticket = new URLSearchParams((await resolveFrame(base, [k]))[k].url.split("?")[1]).get("view")!;
    const r = await art(base, "artifacts/sess-own/output/n.xml", undefined, `&view=${encodeURIComponent(ticket)}`);
    expect(r.status).toBe(401);
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
    await r.text();
  });

  it("header matrix through the route: XML sandboxed, JS downgraded to an octet-stream attachment, unknown attached, HTML-in-a-.pdf sandboxed", async () => {
    const base = await listen();
    const get = async (k: string) => {
      const r = await art(base, k, bearer(OWNER));
      await r.text();
      return r;
    };
    const xml = await get("artifacts/sess-own/output/n.xml");
    expect(xml.headers.get("content-type")).toBe("text/xml");
    expect(xml.headers.get("content-security-policy")).toContain("sandbox");
    expect(xml.headers.get("content-security-policy")).not.toContain("allow-same-origin");

    const js = await get("artifacts/sess-own/output/x.js");
    expect(js.headers.get("content-type")).toBe("application/octet-stream");
    expect(js.headers.get("content-disposition")).toMatch(/^attachment;/);

    const zip = await get("artifacts/sess-own/output/a.zip");
    expect(zip.headers.get("content-type")).toBe("application/zip");
    expect(zip.headers.get("content-disposition")).toMatch(/^attachment;/);

    const fake = await get("artifacts/sess-own/output/page.pdf");
    expect(fake.headers.get("content-type")).toBe("text/html");
    expect(fake.headers.get("content-security-policy")).toContain("sandbox");

    for (const r of [xml, js, zip, fake]) expect(r.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("refusals carry the isolation headers too (401 from the middleware, 400 from the handler)", async () => {
    const base = await listen();
    const unauth = await art(base, "artifacts/sess-own/output/a.mp4");
    const foreign = await art(base, "artifacts/sess-other/output/a.mp4", bearer(OWNER));
    const badKey = await art(base, "secrets/x", bearer(OWNER));
    for (const [r, status] of [[unauth, 401], [foreign, 400], [badKey, 400]] as const) {
      expect(r.status).toBe(status);
      expect(r.headers.get("x-content-type-options")).toBe("nosniff");
      expect(r.headers.get("content-security-policy")).toContain("sandbox");
      await r.text();
    }
  });

  it("ungated mode too: nosniff + sandbox on every response", async () => {
    delete process.env.ARTIFACTS_REQUIRE_AUTH;
    const base = await listen();
    const r = await art(base, "artifacts/sess-own/output/n.xml");
    expect(r.status).toBe(200);
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
    expect(r.headers.get("content-security-policy")).toContain("sandbox");
    await r.text();
  });
});
