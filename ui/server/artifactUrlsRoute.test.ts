// Backbone of the "no broken links" media model: POST /api/artifact-urls resolves stable
// artifacts/… KEY refs to short-lived presigned URLs under the shared artifact-key policy. These
// tests pin the security-critical behavior (policy, key validation, fail-closed, download flag) with
// injected fakes — no AWS, no network.
import { describe, it, expect, vi } from "vitest";
import { Readable } from "stream";
import {
  sessionIdForKey,
  isArtifactKey,
  normalizeArtifactKey,
  authorizeArtifactKey,
  resolveArtifactUrls,
  handleArtifactUrls,
  attachmentDisposition,
  artifactKeyFromUrl,
  defaultAuthorizeM2cRef,
  defaultAuthorizeUnattributed,
  classifyArtifactKey,
  isM2cArtifactKey,
  m2cArtifactKey,
  parseM2cArtifactKey,
  ARTIFACT_URL_TTL_S,
  type AuthorizeM2cRef,
  type M2cAuthorizeDeps,
  type ResolveDeps,
  type LookupSessionOwnership,
  type AuthorizeUnattributed,
} from "./artifactUrlsRoute";
import { artifactOwnerTag, externalArtifactOwnerTag, ownerTaggedRenderSession } from "./artifactOwner";
import { InMemoryTransactStore } from "./projects/inMemoryStore";
import { mirrorKey, srcGuardKey } from "./projects/schema";

const OWNER = "actor-1";

/** sess-own → owned; sess-other → other-owner; anything else → unknown (no row / no actorId). */
const lookup: LookupSessionOwnership = async (sid, actor) => {
  if (sid === "sess-own") return actor === OWNER ? "owned" : "other-owner";
  if (sid === "sess-other") return "other-owner";
  return "unknown";
};

function deps(overrides: Partial<ResolveDeps> = {}): ResolveDeps {
  return {
    bucket: "artifacts-bucket",
    actorId: OWNER,
    presignGet: async (b, k, _e, o) => `https://s3/${b}/${k}?X-Amz-Signature=sig${o?.download ? "&dl=1" : ""}`,
    lookupSessionOwnership: lookup,
    now: () => 1_000_000_000_000, // fixed epoch ms
    ...overrides,
  };
}

describe("TTL", () => {
  it("is one hour", () => {
    expect(ARTIFACT_URL_TTL_S).toBe(3600);
  });
});

describe("isArtifactKey / sessionIdForKey", () => {
  it("accepts artifact keys, including flat and shared-namespace keys", () => {
    for (const k of ["artifacts/sess/output/a.mp4", "artifacts/flat.png", "artifacts/durable/h.mp4"]) {
      expect(isArtifactKey(k), k).toBe(true);
    }
  });
  it("refuses traversal, backslashes, and non-artifact keys", () => {
    for (const k of ["artifacts/sess/../secrets.json", "artifacts/sess\\x", "skills/x.txt", "artifacts/", "", undefined, 42]) {
      expect(isArtifactKey(k as unknown), String(k)).toBe(false);
    }
  });
  it("extracts the session id from a session-scoped artifact key", () => {
    expect(sessionIdForKey("artifacts/sess-own/output/final.mp4")).toBe("sess-own");
    expect(sessionIdForKey("artifacts/sess-own/render/abc.json")).toBe("sess-own");
  });
  it("has no session for flat keys and the shared namespaces", () => {
    expect(sessionIdForKey("artifacts/onlyonesegment")).toBeNull();
    expect(sessionIdForKey("artifacts/durable/hash.mp4")).toBeNull();
    expect(sessionIdForKey("artifacts/external/hash.png")).toBeNull();
    expect(sessionIdForKey("artifacts/uploads/input/x.mp4")).toBeNull();
  });
  it("normalizes a doubled artifacts/ prefix", () => {
    expect(normalizeArtifactKey("artifacts/artifacts/s/a.mp4")).toBe("artifacts/s/a.mp4");
    expect(normalizeArtifactKey("artifacts/s/a.mp4")).toBe("artifacts/s/a.mp4");
  });
});

describe("authorizeArtifactKey (the contract policy)", () => {
  it("allows the session owner", async () => {
    expect(await authorizeArtifactKey("artifacts/sess-own/output/a.mp4", OWNER, lookup)).toBe("allow");
  });
  it("denies (not_found) a session row owned by a DIFFERENT actor", async () => {
    expect(await authorizeArtifactKey("artifacts/sess-other/output/a.mp4", OWNER, lookup)).toBe("not_found");
    expect(await authorizeArtifactKey("artifacts/sess-own/output/a.mp4", "intruder", lookup)).toBe("not_found");
  });
  it("REFUSES a missing row / row without actorId (unknown) unless a readable project registered it", async () => {
    // Was "allow" for every signed-in user: a deleted session's folder, a legacy render-<ts> dir.
    expect(await authorizeArtifactKey("artifacts/no-row/output/a.mp4", OWNER, lookup)).toBe("not_found");
    const registered: AuthorizeUnattributed = async (k) => (k === "artifacts/no-row/output/a.mp4" ? "allow" : "not_found");
    expect(await authorizeArtifactKey("artifacts/no-row/output/a.mp4", OWNER, lookup, undefined, undefined, undefined, registered)).toBe("allow");
    expect(await authorizeArtifactKey("artifacts/no-row/output/b.mp4", OWNER, lookup, undefined, undefined, undefined, registered)).toBe("not_found");
  });
  it("allows the shared namespaces (durable/, external/) WITHOUT an ownership lookup", async () => {
    const spy = vi.fn(lookup);
    for (const k of ["artifacts/durable/x.mp4", "artifacts/external/y.png"]) {
      expect(await authorizeArtifactKey(k, OWNER, spy), k).toBe("allow");
    }
    expect(spy).not.toHaveBeenCalled();
  });
  it("REFUSES flat keys and legacy untagged uploads WITHOUT a session lookup (any signed-in user could read them)", async () => {
    const spy = vi.fn(lookup);
    for (const k of ["artifacts/flat.mp4", "artifacts/1784754468569-clip.mp4", "artifacts/uploads/z.mov",
      "artifacts/uploads/input/1784754468569-clip.mp4"]) {
      expect(await authorizeArtifactKey(k, OWNER, spy), k).toBe("not_found");
    }
    expect(spy).not.toHaveBeenCalled();
  });
  it("allows a flat / legacy upload key that a project the actor can read registered", async () => {
    const seen: string[] = [];
    const registered: AuthorizeUnattributed = async (k, actor) => { seen.push(`${actor}:${k}`); return "allow"; };
    expect(await authorizeArtifactKey("artifacts/uploads/input/1-a.mp4", OWNER, lookup, undefined, undefined, undefined, registered)).toBe("allow");
    expect(seen).toEqual([`${OWNER}:artifacts/uploads/input/1-a.mp4`]);
  });
  it("fails closed when the registered-location check throws", async () => {
    const boom: AuthorizeUnattributed = async () => { throw new Error("ddb down"); };
    expect(await authorizeArtifactKey("artifacts/flat.mp4", OWNER, lookup, undefined, undefined, undefined, boom)).toBe("not_found");
  });
  it("serves an owner-tagged upload to its uploader only, without any lookup", async () => {
    const spy = vi.fn(lookup);
    const unattributed = vi.fn<AuthorizeUnattributed>(async () => "allow");
    const mine = `artifacts/uploads/${artifactOwnerTag(OWNER)}/input/1-a.mp4`;
    expect(await authorizeArtifactKey(mine, OWNER, spy, undefined, undefined, undefined, unattributed)).toBe("allow");
    // Another user, even one a project grants unattributed reads to, never gets someone else's tag.
    expect(await authorizeArtifactKey(mine, "intruder", spy, undefined, undefined, undefined, unattributed)).toBe("not_found");
    // An external caller's upload is never an internal user's.
    const ext = `artifacts/uploads/${externalArtifactOwnerTag("agent-1", "https://idp", OWNER)}/input/1-a.mp4`;
    expect(await authorizeArtifactKey(ext, OWNER, spy, undefined, undefined, undefined, unattributed)).toBe("not_found");
    expect(spy).not.toHaveBeenCalled();
    expect(unattributed).not.toHaveBeenCalled();
  });
  it("serves an owner-tagged render session's manifest to its owner only", async () => {
    const sid = ownerTaggedRenderSession(OWNER, 1_700_000_000_000);
    const key = `artifacts/${sid}/render/m.json`;
    expect(await authorizeArtifactKey(key, OWNER, lookup)).toBe("allow");
    expect(await authorizeArtifactKey(key, "intruder", lookup)).toBe("not_found");
  });
  it("refuses malformed keys as not_resolvable without a lookup", async () => {
    const spy = vi.fn(lookup);
    for (const k of ["artifacts/sess/../y", "artifacts/a\\b", "skills/z", ""]) {
      expect(await authorizeArtifactKey(k, OWNER, spy), k).toBe("not_resolvable");
    }
    expect(spy).not.toHaveBeenCalled();
  });
  it("fails closed: a lookup error is not_found", async () => {
    const boom: LookupSessionOwnership = async () => { throw new Error("ddb down"); };
    expect(await authorizeArtifactKey("artifacts/sess-own/output/a.mp4", OWNER, boom)).toBe("not_found");
  });
  it("checks ownership on the NORMALIZED key (a doubled prefix cannot dodge the session check)", async () => {
    expect(await authorizeArtifactKey("artifacts/artifacts/sess-other/output/a.mp4", OWNER, lookup)).toBe("not_found");
  });
});

describe("resolveArtifactUrls", () => {
  it("signs owned keys with a 1h TTL and epoch-second expiry", async () => {
    const presignGet = vi.fn(deps().presignGet);
    const r = await resolveArtifactUrls(
      ["artifacts/sess-own/output/a.mp4", "artifacts/sess-own/render/b.json"],
      deps({ presignGet }),
    );
    expect(Object.keys(r.urls).sort()).toEqual([
      "artifacts/sess-own/output/a.mp4",
      "artifacts/sess-own/render/b.json",
    ]);
    expect(r.errors).toEqual({});
    expect(r.urls["artifacts/sess-own/output/a.mp4"].url).toContain("X-Amz-Signature");
    expect(r.urls["artifacts/sess-own/output/a.mp4"].expiresAt).toBe(1_000_000_000 + 3600);
    expect(presignGet).toHaveBeenCalledWith("artifacts-bucket", "artifacts/sess-own/output/a.mp4", 3600, undefined);
  });

  it("signs shared-namespace keys for any authenticated actor, but not flat, untagged-upload or row-less session keys", async () => {
    const shared = ["artifacts/durable/x.mp4", "artifacts/external/y.png"];
    const refused = ["artifacts/flat.png", "artifacts/uploads/u/z.mov", "artifacts/uploads/input/1-z.mov", "artifacts/no-row/output/a.mp4"];
    const presignGet = vi.fn(deps().presignGet);
    const r = await resolveArtifactUrls([...shared, ...refused], deps({ actorId: "someone", presignGet }));
    expect(Object.keys(r.urls).sort()).toEqual([...shared].sort());
    expect(Object.keys(r.errors).sort()).toEqual([...refused].sort());
    for (const k of refused) expect(r.errors[k]).toEqual({ code: "not_found" });
    expect(presignGet).toHaveBeenCalledTimes(shared.length);
  });

  it("signs the caller's own tagged upload, and a row-less key a readable project registered", async () => {
    const mine = `artifacts/uploads/${artifactOwnerTag("someone")}/input/1-z.mov`;
    const registered: AuthorizeUnattributed = async (k) => (k === "artifacts/no-row/output/a.mp4" ? "allow" : "not_found");
    const r = await resolveArtifactUrls([mine, "artifacts/no-row/output/a.mp4"], deps({ actorId: "someone", authorizeUnattributed: registered }));
    expect(Object.keys(r.urls).sort()).toEqual([mine, "artifacts/no-row/output/a.mp4"].sort());
    expect(r.errors).toEqual({});
  });

  it("refuses a key for a session owned by another actor (uniform not_found, no url, no presign)", async () => {
    const presignGet = vi.fn(async () => "https://s3/x?X-Amz-Signature=s");
    const r = await resolveArtifactUrls(["artifacts/sess-other/output/a.mp4"], deps({ presignGet }));
    expect(r.urls).toEqual({});
    expect(r.errors["artifacts/sess-other/output/a.mp4"]).toEqual({ code: "not_found" });
    expect(presignGet).not.toHaveBeenCalled();
  });

  it("refuses non-resolvable keys (traversal / non-artifact)", async () => {
    const r = await resolveArtifactUrls(["artifacts/sess/../y", "skills/z"], deps());
    expect(r.urls).toEqual({});
    expect(r.errors).toEqual({
      "artifacts/sess/../y": { code: "not_resolvable" },
      "skills/z": { code: "not_resolvable" },
    });
  });

  it("looks up each session's ownership at most once per request", async () => {
    const spy = vi.fn(lookup);
    await resolveArtifactUrls(
      ["artifacts/sess-own/output/a.mp4", "artifacts/sess-own/output/b.mp4", "artifacts/sess-own/render/c.json"],
      deps({ lookupSessionOwnership: spy }),
    );
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("fails closed: an ownership-lookup error signs nothing", async () => {
    const r = await resolveArtifactUrls(["artifacts/sess-own/output/a.mp4"], deps({
      lookupSessionOwnership: async () => { throw new Error("ddb down"); },
    }));
    expect(r.urls).toEqual({});
    expect(r.errors["artifacts/sess-own/output/a.mp4"]).toEqual({ code: "not_found" });
  });

  it("passes the download flag to the presigner", async () => {
    const presignGet = vi.fn(deps().presignGet);
    const r = await resolveArtifactUrls(["artifacts/sess-own/output/a.mp4"], deps({ presignGet, download: true }));
    expect(presignGet).toHaveBeenCalledWith("artifacts-bucket", "artifacts/sess-own/output/a.mp4", 3600, { download: true });
    expect(r.urls["artifacts/sess-own/output/a.mp4"].url).toContain("dl=1");
  });

  it("keys results by the key as sent but signs the normalized key", async () => {
    const presignGet = vi.fn(deps().presignGet);
    const r = await resolveArtifactUrls(["artifacts/artifacts/sess-own/output/a.mp4"], deps({ presignGet }));
    expect(Object.keys(r.urls)).toEqual(["artifacts/artifacts/sess-own/output/a.mp4"]);
    expect(presignGet.mock.calls[0][1]).toBe("artifacts/sess-own/output/a.mp4");
  });
});

describe("attachmentDisposition", () => {
  it("is attachment with the key's basename", () => {
    expect(attachmentDisposition("artifacts/s/output/final cut.mp4")).toBe('attachment; filename="final cut.mp4"');
  });
  it("cannot be broken out of by quotes/CRLF, and carries filename* for non-ASCII", () => {
    expect(attachmentDisposition('artifacts/s/a"b\r\n.mp4')).toBe('attachment; filename="a_b__.mp4"; filename*=UTF-8\'\'a%22b%0D%0A.mp4');
    expect(attachmentDisposition("artifacts/s/é.mp4")).toBe("attachment; filename=\"_.mp4\"; filename*=UTF-8''%C3%A9.mp4");
  });
});

describe("artifactKeyFromUrl", () => {
  const D = "d123.cloudfront.net";
  it("recognises our CloudFront artifact URLs and /api/artifact URLs", () => {
    expect(artifactKeyFromUrl(`https://${D}/artifacts/s/output/a.mp4`, D)).toBe("artifacts/s/output/a.mp4");
    expect(artifactKeyFromUrl(`https://${D}/artifacts/s/output/a%20b.mp4?v=1`, D)).toBe("artifacts/s/output/a b.mp4");
    expect(artifactKeyFromUrl("/api/artifact?key=artifacts/s/output/a.mp4", D)).toBe("artifacts/s/output/a.mp4");
    expect(artifactKeyFromUrl("/api/artifact?key=artifacts%2Fs%2Fa.mp4&download=1", D)).toBe("artifacts/s/a.mp4");
    expect(artifactKeyFromUrl("https://ui.example.com/api/artifact?key=artifacts/s/a.mp4", D)).toBe("artifacts/s/a.mp4");
    expect(artifactKeyFromUrl(`https://${D}/artifacts/artifacts/s/a.mp4`, D)).toBe("artifacts/s/a.mp4");
  });
  it("leaves everything else alone (bare keys, other hosts, non-artifact paths, traversal)", () => {
    for (const s of [
      "artifacts/s/output/a.mp4",
      "https://other.example.com/artifacts/s/a.mp4",
      `https://${D}/skills/x.md`,
      `http://${D}/artifacts/s/a.mp4`,
      `https://${D}/artifacts/s/..%2F..%2Fx`,
      "/api/artifact?key=skills/x",
      "api/artifact?key=artifacts/s/a.mp4",
      "hello world",
      "",
    ]) {
      expect(artifactKeyFromUrl(s, D), s).toBeNull();
    }
  });
});

// ---- HTTP handler (body parsing + download flag) --------------------------------------------------

function fakeReq(body: unknown, actorId: string = OWNER): any {
  const r: any = Readable.from([Buffer.from(typeof body === "string" ? body : JSON.stringify(body))]);
  r.actorId = actorId || undefined;
  r.headers = {};
  return r;
}
function fakeRes() {
  const out = { status: 0, body: "" as string, headersSent: false };
  const res: any = {
    get headersSent() { return out.headersSent; },
    writeHead(s: number) { out.status = s; out.headersSent = true; return res; },
    end(b?: string) { if (b) out.body += b; return res; },
  };
  return { res, out };
}
const handlerDeps = () => ({
  bucket: "artifacts-bucket",
  presignGet: deps().presignGet,
  lookupSessionOwnership: lookup,
  now: () => 1_000_000_000_000,
});

describe("handleArtifactUrls", () => {
  it("returns the {urls, errors} contract", async () => {
    const { res, out } = fakeRes();
    await handleArtifactUrls(fakeReq({ keys: ["artifacts/sess-own/a.mp4", "artifacts/sess-other/b.mp4", "x/y"] }), res, handlerDeps());
    expect(out.status).toBe(200);
    const body = JSON.parse(out.body);
    expect(body.urls["artifacts/sess-own/a.mp4"]).toEqual({ url: expect.stringContaining("X-Amz-Signature"), expiresAt: 1_000_000_000 + 3600 });
    expect(body.errors).toEqual({ "artifacts/sess-other/b.mp4": { code: "not_found" }, "x/y": { code: "not_resolvable" } });
  });
  it("honours download: true", async () => {
    const { res, out } = fakeRes();
    await handleArtifactUrls(fakeReq({ keys: ["artifacts/sess-own/a.mp4"], download: true }), res, handlerDeps());
    expect(JSON.parse(out.body).urls["artifacts/sess-own/a.mp4"].url).toContain("dl=1");
  });
  it("400s a non-array keys body and > 100 keys; 401s with no actor", async () => {
    let r = fakeRes();
    await handleArtifactUrls(fakeReq({ keys: "artifacts/a" }), r.res, handlerDeps());
    expect(r.out.status).toBe(400);
    r = fakeRes();
    await handleArtifactUrls(fakeReq({ keys: Array.from({ length: 101 }, (_, i) => `artifacts/k${i}`) }), r.res, handlerDeps());
    expect(r.out.status).toBe(400);
    r = fakeRes();
    await handleArtifactUrls(fakeReq({ keys: [] }, ""), r.res, handlerDeps());
    expect(r.out.status).toBe(401);
  });
});

// ---- M2C references (asset-lineage D4/R4) --------------------------------------------------------
//
// `artifacts/m2c/<bucket>/<key>` names an object M2C wrote, referenced where it wrote it and never
// copied. It travels as an artifact key so nothing in the chain needed new plumbing, but it is
// neither session-owned nor shared: it is served only to an actor who can read a PROJECT that
// registered that location. The store here is the real `InMemoryTransactStore` with the real key
// builders — a looser fake would hide a key-shape mismatch (AGENTS.md).

const M2C_BUCKET = "m2cv6-proxy-bucket";
const M2C_KEY = "micro-workflow/media-processing/EXEC/proxy/transcoded.mp4";
const M2C_REF = `artifacts/m2c/${M2C_BUCKET}/${M2C_KEY}`;
const PID_MEMBER = "p" + "1".repeat(32);
const PID_OTHER = "p" + "2".repeat(32);

/** A store where PID_MEMBER registers the location and OWNER mirrors it. */
function m2cStore(opts: { mirrors?: string[]; registeredIn?: string[] } = {}) {
  const store = new InMemoryTransactStore();
  for (const pid of opts.mirrors ?? [PID_MEMBER]) {
    store.seed({ ...mirrorKey(OWNER, pid), role: "member" });
  }
  for (const pid of opts.registeredIn ?? [PID_MEMBER]) {
    store.seed({ ...srcGuardKey(pid, M2C_BUCKET, M2C_KEY), assetId: "video:asset-1" });
  }
  return store;
}

/** `checkAccess` stand-in: OWNER is a member of PID_MEMBER only. */
const m2cCheckAccess = async (actor: string, pid: string) => ({ ok: actor === OWNER && pid === PID_MEMBER });

function m2cAuthorizer(over: Partial<M2cAuthorizeDeps> = {}): AuthorizeM2cRef {
  return defaultAuthorizeM2cRef({
    m2cBucket: M2C_BUCKET,
    store: m2cStore(),
    checkAccess: m2cCheckAccess,
    ...over,
  });
}

describe("m2c reference encoding", () => {
  it("round-trips a bucket and key", () => {
    expect(m2cArtifactKey(M2C_BUCKET, M2C_KEY)).toBe(M2C_REF);
    expect(parseM2cArtifactKey(M2C_REF)).toEqual({ bucket: M2C_BUCKET, key: M2C_KEY });
    expect(isM2cArtifactKey(M2C_REF)).toBe(true);
  });
  it("is an artifact key, so every existing shape check accepts it", () => {
    expect(isArtifactKey(M2C_REF)).toBe(true);
    expect(artifactKeyFromUrl(`/api/artifact?key=${M2C_REF}`, "d123.cloudfront.net")).toBe(M2C_REF);
  });
  it("is NOT session-scoped: an m2c reference never matches a caller's session", () => {
    // `sessionIdForKey` reports the literal namespace, which is no actor's session, so any caller
    // comparing it against a known session refuses it. `authorizeArtifactKey` decides it earlier.
    expect(sessionIdForKey(M2C_REF)).toBe("m2c");
  });
  it("refuses anything that cannot name one object", () => {
    for (const k of [
      "artifacts/m2c/",
      `artifacts/m2c/${M2C_BUCKET}`,            // no key
      `artifacts/m2c/${M2C_BUCKET}/`,           // empty key
      `artifacts/m2c/${M2C_BUCKET}/a/prefix/`,  // a prefix, not an object
      "artifacts/m2c/B/x.mp4",                  // not a bucket name
      "artifacts/m2c/Not_A_Bucket/x.mp4",
      "artifacts/sess/output/a.mp4",
    ]) {
      expect(parseM2cArtifactKey(k), k).toBeNull();
      expect(isM2cArtifactKey(k), k).toBe(false);
    }
  });
});

describe("authorizeM2cRef (project membership is the gate)", () => {
  const ref = { bucket: M2C_BUCKET, key: M2C_KEY };

  it("allows an actor who can read a project that registers the location", async () => {
    expect(await m2cAuthorizer()(ref, OWNER)).toBe("allow");
  });
  it("refuses an actor with no project that registers it", async () => {
    // mirrors a project, but the guard row lives in a project the actor is not in
    expect(await m2cAuthorizer({ store: m2cStore({ registeredIn: [PID_OTHER] }) })(ref, OWNER)).toBe("not_found");
    // and an actor with no mirrors at all
    expect(await m2cAuthorizer({ store: m2cStore({ mirrors: [] }) })(ref, OWNER)).toBe("not_found");
  });
  it("refuses when the mirror exists but checkAccess denies (deleted project, removed member)", async () => {
    const authorize = m2cAuthorizer({ checkAccess: async () => ({ ok: false }) });
    expect(await authorize(ref, OWNER)).toBe("not_found");
  });
  it("refuses an unknown reference: a location no project registered", async () => {
    const authorize = m2cAuthorizer();
    expect(await authorize({ bucket: M2C_BUCKET, key: "never/registered.mp4" }, OWNER)).toBe("not_found");
  });
  it("refuses a bucket that is not the M2C proxy bucket, and a missing configuration", async () => {
    expect(await m2cAuthorizer()({ bucket: "some-other-bucket", key: M2C_KEY }, OWNER)).toBe("not_found");
    expect(await m2cAuthorizer({ m2cBucket: "" })(ref, OWNER)).toBe("not_found");
    expect(await m2cAuthorizer({ store: null })(ref, OWNER)).toBe("not_found");
  });
  it("fails closed: a store error is not_found, never a throw", async () => {
    const broken = { query: async () => { throw new Error("ddb down"); }, batchGet: async () => [] };
    expect(await m2cAuthorizer({ store: broken })(ref, OWNER)).toBe("not_found");
  });
});

describe("m2c references through the artifact-key policy and the resolver", () => {
  it("is refused when no m2c authorizer is wired (fail closed)", async () => {
    expect(await authorizeArtifactKey(M2C_REF, OWNER, lookup)).toBe("not_found");
  });
  it("never consults session ownership", async () => {
    const spy = vi.fn(lookup);
    expect(await authorizeArtifactKey(M2C_REF, OWNER, spy, undefined, m2cAuthorizer())).toBe("allow");
    expect(spy).not.toHaveBeenCalled();
  });
  it("authorizes one reference at most once per request, on the normalized key", async () => {
    const authorize = vi.fn(m2cAuthorizer());
    const r = await resolveArtifactUrls([M2C_REF, M2C_REF, `artifacts/${M2C_REF}`], deps({
      m2cBucket: M2C_BUCKET,
      authorizeM2c: authorize,
    }));
    expect(authorize).toHaveBeenCalledTimes(1);
    expect(Object.keys(r.urls).sort()).toEqual([`artifacts/${M2C_REF}`, M2C_REF].sort());
  });

  it("presigns the M2C PROXY bucket under the key the reference names", async () => {
    const presignGet = vi.fn(deps().presignGet);
    const r = await resolveArtifactUrls([M2C_REF], deps({
      presignGet,
      m2cBucket: M2C_BUCKET,
      authorizeM2c: m2cAuthorizer(),
    }));
    expect(presignGet).toHaveBeenCalledWith(M2C_BUCKET, M2C_KEY, 3600, undefined);
    expect(r.urls[M2C_REF]).toEqual({ url: expect.stringContaining("X-Amz-Signature"), expiresAt: 1_000_000_000 + 3600 });
    expect(r.errors).toEqual({});
  });

  it("carries the download flag", async () => {
    const presignGet = vi.fn(deps().presignGet);
    await resolveArtifactUrls([M2C_REF], deps({
      presignGet, download: true, m2cBucket: M2C_BUCKET, authorizeM2c: m2cAuthorizer(),
    }));
    expect(presignGet).toHaveBeenCalledWith(M2C_BUCKET, M2C_KEY, 3600, { download: true });
  });

  it("refuses a non-member and an unknown reference, with no presign", async () => {
    const presignGet = vi.fn(async () => "https://s3/x");
    const unknown = `artifacts/m2c/${M2C_BUCKET}/never/registered.mp4`;
    const r = await resolveArtifactUrls([M2C_REF, unknown], deps({
      presignGet,
      actorId: "intruder",
      m2cBucket: M2C_BUCKET,
      authorizeM2c: m2cAuthorizer(),
    }));
    expect(r.urls).toEqual({});
    expect(r.errors).toEqual({ [M2C_REF]: { code: "not_found" }, [unknown]: { code: "not_found" } });
    expect(presignGet).not.toHaveBeenCalled();
  });

  it("refuses a reference naming a bucket other than the configured M2C bucket", async () => {
    const foreign = `artifacts/m2c/some-other-bucket/${M2C_KEY}`;
    const r = await resolveArtifactUrls([foreign], deps({
      m2cBucket: M2C_BUCKET,
      authorizeM2c: async () => "allow", // even an (impossibly) permissive authorizer cannot widen it
    }));
    expect(r.urls).toEqual({});
    expect(r.errors[foreign]).toEqual({ code: "not_found" });
  });

  it("does not widen the existing session policy", async () => {
    const r = await resolveArtifactUrls(
      ["artifacts/sess-other/output/a.mp4", "artifacts/sess/../y"],
      deps({ m2cBucket: M2C_BUCKET, authorizeM2c: async () => "allow" }),
    );
    expect(r.urls).toEqual({});
    expect(r.errors["artifacts/sess-other/output/a.mp4"]).toEqual({ code: "not_found" });
    expect(r.errors["artifacts/sess/../y"]).toEqual({ code: "not_resolvable" });
  });

  it("is served by the handler end to end", async () => {
    const { res, out } = fakeRes();
    await handleArtifactUrls(fakeReq({ keys: [M2C_REF] }), res, {
      ...handlerDeps(),
      m2cBucket: M2C_BUCKET,
      authorizeM2c: m2cAuthorizer(),
    });
    expect(out.status).toBe(200);
    expect(JSON.parse(out.body).urls[M2C_REF].url).toContain(`${M2C_BUCKET}/${M2C_KEY}`);
  });
});

// ------------------------------------------------------------------ asset-lineage Phase 3 (a)
import { isServableArtifactKey, type CheckProjectMember } from "./artifactUrlsRoute";

describe("projects-session output keys (owner OR project member)", () => {
  const PID = "p" + "3".repeat(32);
  const SID = "4f9d0d02-1111-4222-8333-444455556666";
  const KEY = `projects/${PID}/sessions/${SID}/output/clip.mp4`;
  const own: LookupSessionOwnership = async (sid, actor) =>
    sid === SID ? (actor === OWNER ? "owned" : "other-owner") : "unknown";
  const member: CheckProjectMember = async (actor, pid) => actor === "member-1" && pid === PID;

  it("shape: only the output sub-tree is servable; isArtifactKey keeps meaning artifacts/", () => {
    expect(isServableArtifactKey(KEY)).toBe(true);
    expect(isArtifactKey(KEY)).toBe(false);
    expect(isServableArtifactKey(`projects/${PID}/video:x/a.mp4`)).toBe(false);
    expect(isServableArtifactKey(`projects/${PID}/sessions/${SID}/render/m.json`)).toBe(false);
  });

  it("owner allowed; member allowed; anybody else not_found", async () => {
    expect(await authorizeArtifactKey(KEY, OWNER, own, undefined, undefined, member)).toBe("allow");
    expect(await authorizeArtifactKey(KEY, "member-1", own, undefined, undefined, member)).toBe("allow");
    expect(await authorizeArtifactKey(KEY, "stranger", own, undefined, undefined, member)).toBe("not_found");
    // Without a membership check only the owner may read it.
    expect(await authorizeArtifactKey(KEY, "member-1", own)).toBe("not_found");
  });

  it("an unknown session owner is NOT enough (unlike a legacy key)", async () => {
    const unknown: LookupSessionOwnership = async () => "unknown";
    expect(await authorizeArtifactKey(KEY, "stranger", unknown, undefined, undefined, member)).toBe("not_found");
    // A legacy key with an unknown owner is no longer enough either (it needs a registering project).
    expect(await authorizeArtifactKey(`artifacts/${SID}/output/a.mp4`, "stranger", unknown)).toBe("not_found");
  });

  it("a lookup or membership ERROR fails closed", async () => {
    const boom: LookupSessionOwnership = async () => { throw new Error("ddb"); };
    expect(await authorizeArtifactKey(KEY, "member-1", boom, undefined, undefined, member)).toBe("not_found");
    const memberBoom: CheckProjectMember = async () => { throw new Error("ddb"); };
    expect(await authorizeArtifactKey(KEY, "stranger", own, undefined, undefined, memberBoom)).toBe("not_found");
  });

  it("a project ASSET key is not_resolvable", async () => {
    expect(await authorizeArtifactKey(`projects/${PID}/video:x/a.mp4`, OWNER, own, undefined, undefined, member))
      .toBe("not_resolvable");
  });

  it("resolveArtifactUrls presigns it in the artifacts bucket for a member", async () => {
    const r = await resolveArtifactUrls([KEY], deps({ actorId: "member-1", lookupSessionOwnership: own, checkProjectMember: member }));
    expect(r.urls[KEY].url).toContain(`artifacts-bucket/${KEY}`);
    const no = await resolveArtifactUrls([KEY], deps({ actorId: "stranger", lookupSessionOwnership: own, checkProjectMember: member }));
    expect(no.errors[KEY]).toEqual({ code: "not_found" });
  });

  it("artifactKeyFromUrl accepts the CDN and /api/artifact forms, not asset keys", () => {
    expect(artifactKeyFromUrl(`https://d.cloudfront.net/${KEY}`, "d.cloudfront.net")).toBe(KEY);
    expect(artifactKeyFromUrl(`/api/artifact?key=${encodeURIComponent(KEY)}`, "d.cloudfront.net")).toBe(KEY);
    expect(artifactKeyFromUrl(`https://d.cloudfront.net/projects/${PID}/video:x/a.mp4`, "d.cloudfront.net")).toBeNull();
    expect(artifactKeyFromUrl(`https://evil.example/${KEY}`, "d.cloudfront.net")).toBeNull();
  });
});

describe('voice previews (the shared TTS catalog) resolve for any signed-in actor', () => {
  it('serves exactly the voice-preview keys of the M2C bucket, without a project registration', async () => {
    const { defaultAuthorizeM2cRef, VOICE_PREVIEW_KEY_RE } = await import('./artifactUrlsRoute');
    const store = { query: vi.fn(async () => []), batchGet: vi.fn(async () => []) };
    const authorize = defaultAuthorizeM2cRef({ m2cBucket: 'm2c-proxy', store: store as never });
    const clone = 'micro-workflow/tts-on-aws/voices/85bc98d1-23dc-4e72-818f-78ae0f15ddd3/preview.wav';
    const seeded = '_model_artefacts/voice_catalog/default/35e22a27-1bf8-4e84-bcef-afc6184cd3cf/preview.wav';
    expect(await authorize({ bucket: 'm2c-proxy', key: clone }, 'actor-1')).toBe('allow');
    expect(await authorize({ bucket: 'm2c-proxy', key: seeded }, 'actor-1')).toBe('allow');
    // anything else still needs a project that references it
    expect(await authorize({ bucket: 'm2c-proxy', key: 'micro-workflow/tts-on-aws/voices/85bc98d1-23dc-4e72-818f-78ae0f15ddd3/ref.wav' }, 'actor-1')).toBe('not_found');
    expect(await authorize({ bucket: 'm2c-proxy', key: 'micro-workflow/tts/out/x/preview.wav' }, 'actor-1')).toBe('not_found');
    expect(await authorize({ bucket: 'other-bucket', key: clone }, 'actor-1')).toBe('not_found');
    expect(await authorize({ bucket: 'm2c-proxy', key: clone }, '')).toBe('not_found');
    expect(VOICE_PREVIEW_KEY_RE.test(`../${clone}`)).toBe(false);
  });
});


describe("classifyArtifactKey", () => {
  it("names the owner of every artifacts/ key shape", () => {
    const tag = artifactOwnerTag("a");
    const xtag = externalArtifactOwnerTag("agent", "https://idp", "sub");
    expect(classifyArtifactKey("artifacts/durable/h.mp4")).toEqual({ kind: "shared" });
    expect(classifyArtifactKey("artifacts/external/h.png")).toEqual({ kind: "shared" });
    expect(classifyArtifactKey(`artifacts/uploads/${tag}/input/1-a.mp4`)).toEqual({ kind: "tag", tag });
    expect(classifyArtifactKey(`artifacts/uploads/${xtag}/input/1-a.mp4`)).toEqual({ kind: "tag", tag: xtag });
    expect(classifyArtifactKey(`artifacts/${ownerTaggedRenderSession("a", 1)}/render/m.json`)).toEqual({ kind: "tag", tag });
    expect(classifyArtifactKey("artifacts/sess-1/output/a.mp4")).toEqual({ kind: "session", sessionId: "sess-1" });
    expect(classifyArtifactKey("artifacts/render-1787851114826-pmdrgo7mvih0/render/m.json"))
      .toEqual({ kind: "session", sessionId: "render-1787851114826-pmdrgo7mvih0" });
    for (const k of ["artifacts/flat.mp4", "artifacts/uploads/input/1-a.mp4", "artifacts/uploads/z.mov",
      `artifacts/uploads/${tag}`]) {
      expect(classifyArtifactKey(k), k).toEqual({ kind: "unattributed" });
    }
  });
});

describe("defaultAuthorizeUnattributed (registered in a readable project)", () => {
  const BUCKET = "artifacts-bucket";
  const KEY = "artifacts/uploads/input/1-legacy.mp4";
  const PID = "p" + "3".repeat(32);
  function store(registeredIn: string | null) {
    const s = new InMemoryTransactStore();
    s.seed({ ...mirrorKey(OWNER, PID), role: "member" });
    if (registeredIn) s.seed({ ...srcGuardKey(registeredIn, BUCKET, KEY), assetId: "video:asset-1" });
    return s;
  }
  const member = async (actor: string, pid: string) => ({ ok: actor === OWNER && pid === PID });
  it("allows a location registered in a project the actor can read", async () => {
    const auth = defaultAuthorizeUnattributed({ bucket: BUCKET, store: store(PID), checkAccess: member });
    expect(await auth(KEY, OWNER)).toBe("allow");
  });
  it("refuses an unregistered location, a non-member, another bucket's guard, and a missing store or bucket", async () => {
    expect(await defaultAuthorizeUnattributed({ bucket: BUCKET, store: store(null), checkAccess: member })(KEY, OWNER)).toBe("not_found");
    expect(await defaultAuthorizeUnattributed({ bucket: BUCKET, store: store(PID), checkAccess: async () => ({ ok: false }) })(KEY, OWNER)).toBe("not_found");
    expect(await defaultAuthorizeUnattributed({ bucket: "other-bucket", store: store(PID), checkAccess: member })(KEY, OWNER)).toBe("not_found");
    expect(await defaultAuthorizeUnattributed({ bucket: BUCKET, store: null })(KEY, OWNER)).toBe("not_found");
    expect(await defaultAuthorizeUnattributed({ bucket: "", store: store(PID), checkAccess: member })(KEY, OWNER)).toBe("not_found");
  });
});
