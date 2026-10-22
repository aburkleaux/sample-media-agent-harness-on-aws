// External artifact download route — auth gate + ref validation + bucket allowlist.
//
// `GET /invoke/:agentId/download?ref=…` mints a presigned URL and 302s to it, so
// the agent can hand back ~196-char links instead of ~1700-char presigned URLs
// (a few of those exhaust its output budget and truncate the reply, leaving a
// SUCCEEDED job with no usable links).
//
// The bucket allowlist is the WHOLE security boundary here: unlike the manifest
// route there is no narrow key-shape regex to fall back on, because a media
// output can be any key. So the tests below lean hardest on (a) no presign on any
// auth failure, (b) no presign for a bucket outside the allowlist, and (c) no
// presign for a traversal-bearing ref.

import { describe, it, expect } from "vitest";

import { AgentTokenError, type VerifiedAgentToken } from "../auth/agentOidc";
import {
  createInvokeDownloadHandler,
  parseArtifactRef,
  type InvokeDownloadHandlerDeps,
} from "./invokeDownloadRoute";
import { deriveExternalSessionId } from "./externalSessionKey";
import { authorizeExternalRef } from "./externalDownloadScope";
import { artifactOwnerTag, externalArtifactOwnerTag } from "../artifactOwner";

const UPLOAD_BUCKET = "app-uploads";
const MEDIA_BUCKET = "m2c-proxy-outputs";
const SIGNED = "https://s3.example.com/signed?X-Amz-Signature=deadbeef";

interface CapturedRes {
  status: number | null;
  headers: Record<string, string>;
  body: string;
  headersSent: boolean;
  writableEnded: boolean;
}

function makeRes(): { res: any; captured: CapturedRes } {
  const captured: CapturedRes = {
    status: null,
    headers: {},
    body: "",
    headersSent: false,
    writableEnded: false,
  };
  const res: any = {
    get headersSent() { return captured.headersSent; },
    get writableEnded() { return captured.writableEnded; },
    writeHead(status: number, headers?: Record<string, string>) {
      captured.status = status;
      if (headers) Object.assign(captured.headers, headers);
      captured.headersSent = true;
      return res;
    },
    end(chunk?: any) {
      if (chunk) captured.body += String(chunk);
      captured.writableEnded = true;
      return res;
    },
    write(chunk?: any) {
      if (chunk) captured.body += String(chunk);
      return true;
    },
  };
  return { res, captured };
}

function makeReq(opts: {
  agentId?: string;
  authorization?: string;
  ref?: string;
  attach?: boolean;
  accept?: string;
  conversationId?: string;
}): any {
  const agentId = opts.agentId ?? "agent-1";
  const parts: string[] = [];
  if (opts.ref !== undefined) parts.push(`ref=${encodeURIComponent(opts.ref)}`);
  if (opts.attach) parts.push("attach=1");
  if (opts.conversationId !== undefined) parts.push(`conversation_id=${encodeURIComponent(opts.conversationId)}`);
  const q = parts.length ? `?${parts.join("&")}` : "";
  const headers: Record<string, string> = {};
  if (opts.authorization) headers.authorization = opts.authorization;
  if (opts.accept) headers.accept = opts.accept;
  return {
    params: { agentId },
    headers,
    url: `/invoke/${agentId}/download${q}`,
  };
}

const ENABLED_AGENT = {
  ownerUserId: "owner-1",
  externalAccessEnabled: true,
  integrationConfig: {
    enabled: true,
    issuer: "https://issuer.example.com",
    audience: "aud-1",
    allowedClientIds: [] as string[],
  },
};

interface PresignSpy {
  called: boolean;
  bucket?: string;
  key?: string;
  attach?: boolean;
}

function makeDeps(
  overrides: Partial<InvokeDownloadHandlerDeps> & {
    verify?: () => Promise<VerifiedAgentToken>;
    presignSpy?: PresignSpy;
  } = {},
): InvokeDownloadHandlerDeps {
  const spy = overrides.presignSpy ?? ({ called: false } as PresignSpy);
  return {
    assetIndexReady: overrides.assetIndexReady ?? true,
    allowedBuckets: overrides.allowedBuckets ?? [UPLOAD_BUCKET, MEDIA_BUCKET],
    defaultBucket: overrides.defaultBucket ?? UPLOAD_BUCKET,
    mediaBucket: overrides.mediaBucket ?? MEDIA_BUCKET,
    queryActorJobs: overrides.queryActorJobs ?? (async () => JOBS),
    resolveCurrentPublishedAgent:
      overrides.resolveCurrentPublishedAgent ?? (async () => ENABLED_AGENT as any),
    verifyAgentToken:
      overrides.verifyAgentToken ??
      (overrides.verify
        ? async () => overrides.verify!()
        : async () => ({ sub: "caller-1", clientId: "client-1" }) as VerifiedAgentToken),
    presignGet:
      overrides.presignGet ??
      (async (bucket: string, key: string, attach: boolean) => {
        spy.called = true;
        spy.bucket = bucket;
        spy.key = key;
        spy.attach = attach;
        return SIGNED;
      }),
  };
}

const BEARER = "Bearer tok";
const ISSUER = "https://issuer.example.com";
/** The verified caller's derived session (agent-1, issuer, caller-1, default conversation). */
const CALLER_SESSION = deriveExternalSessionId("agent-1", ISSUER, "caller-1");
const OTHER_CALLER_SESSION = deriveExternalSessionId("agent-1", ISSUER, "caller-2");
const MY_EXEC = "07cd2c90-2ab2-5c3b-be9e-385a7e06a79b";
const OTHER_EXEC = "11111111-2222-3333-4444-555555555555";
const sfnArn = (exec: string) => `arn:aws:states:us-west-2:1:execution:m2c-media-processing:${exec}`;
/** The Owner's job rows: one ran in THIS caller's session, one in another caller's. */
const JOBS = [
  { jobId: sfnArn(MY_EXEC), actorId: "owner-1", sessionId: CALLER_SESSION },
  { jobId: sfnArn(OTHER_EXEC), actorId: "owner-1", sessionId: OTHER_CALLER_SESSION },
];
const GOOD_REF = `s3://${MEDIA_BUCKET}/micro-workflow/audio-stem/${MY_EXEC}/audio_speech.wav`;

// ---------------------------------------------------------------------------
// parseArtifactRef
// ---------------------------------------------------------------------------

describe("parseArtifactRef", () => {
  it("parses an s3:// uri", () => {
    expect(parseArtifactRef(`s3://${MEDIA_BUCKET}/a/b.wav`, UPLOAD_BUCKET)).toEqual({
      bucket: MEDIA_BUCKET,
      key: "a/b.wav",
    });
  });

  it("resolves a bare key against the default bucket", () => {
    expect(parseArtifactRef("artifacts/x.mp4", UPLOAD_BUCKET)).toEqual({
      bucket: UPLOAD_BUCKET,
      key: "artifacts/x.mp4",
    });
  });

  it("honors attach-independent whitespace trimming", () => {
    expect(parseArtifactRef("  artifacts/x.mp4  ", UPLOAD_BUCKET)?.key).toBe("artifacts/x.mp4");
  });

  it.each([
    ["empty", ""],
    ["whitespace only", "   "],
    ["non-string", 42],
    ["traversal", "artifacts/../../etc/passwd"],
    ["traversal in s3 uri", `s3://${MEDIA_BUCKET}/a/../../b`],
    ["backslash", "artifacts\\x.mp4"],
    ["absolute bare key", "/artifacts/x.mp4"],
    ["s3 uri with no key", "s3://bucket-only"],
    ["s3 uri with empty bucket", "s3:///key"],
    ["prefix not an object", "artifacts/dir/"],
  ])("rejects %s", (_label, input) => {
    expect(parseArtifactRef(input as any, UPLOAD_BUCKET)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Auth gate — never presign on any failure
// ---------------------------------------------------------------------------

describe("auth gate", () => {
  it("302s to the presigned URL for an authorized caller", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ authorization: BEARER, ref: GOOD_REF }), res,
    );
    expect(captured.status).toBe(302);
    expect(captured.headers.location).toBe(SIGNED);
    expect(captured.headers["cache-control"]).toBe("no-store");
    expect(spy).toMatchObject({ called: true, bucket: MEDIA_BUCKET, attach: false });
  });

  // A cross-origin browser client CANNOT follow the 302: it lands on S3, which
  // sends no CORS headers, so `fetch` rejects with an opaque "Failed to fetch"
  // and the link reads "(failed)" even though the redirect was perfectly valid.
  // Observed live from the external console. JSON mode keeps the cross-origin
  // read on this API instead of on S3.
  it("returns the URL as JSON (not a 302) when the caller sends Accept: application/json", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ authorization: BEARER, ref: GOOD_REF, accept: "application/json" }), res,
    );
    expect(captured.status).toBe(200);
    expect(captured.headers["content-type"]).toBe("application/json");
    // CORS header is what makes the JSON readable from the console's origin.
    expect(captured.headers["access-control-allow-origin"]).toBe("*");
    expect(captured.headers["cache-control"]).toBe("no-store");
    expect(JSON.parse(captured.body)).toEqual({ url: SIGNED });
    // No redirect, so nothing sends the browser to a CORS-less origin.
    expect(captured.headers.location).toBeUndefined();
    expect(spy.called).toBe(true);
  });

  it("still 302s for a plain (curl / navigation) caller", async () => {
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps())(
      makeReq({ authorization: BEARER, ref: GOOD_REF, accept: "*/*" }), res,
    );
    expect(captured.status).toBe(302);
    expect(captured.headers.location).toBe(SIGNED);
  });

  it("honours JSON mode alongside attach=1", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ authorization: BEARER, ref: GOOD_REF, attach: true, accept: "application/json" }), res,
    );
    expect(captured.status).toBe(200);
    expect(spy.attach).toBe(true);
  });

  it("does not leak a URL as JSON when auth fails", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ ref: GOOD_REF, accept: "application/json" }), res,
    );
    expect(captured.status).toBe(401);
    expect(captured.body).not.toContain(SIGNED);
    expect(spy.called).toBe(false);
  });

  it("passes attach=1 through to the presigner", async () => {
    const spy: PresignSpy = { called: false };
    const { res } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ authorization: BEARER, ref: GOOD_REF, attach: true }), res,
    );
    expect(spy.attach).toBe(true);
  });

  it("401s with no Authorization header and never presigns", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ ref: GOOD_REF }), res,
    );
    expect(captured.status).toBe(401);
    expect(spy.called).toBe(false);
  });

  it.each([
    ["invalid_token", "invalid_token"],
    ["expired", "expired"],
    ["issuer_mismatch", "issuer_mismatch"],
    ["audience_mismatch", "audience_mismatch"],
    ["jwks_unavailable", "jwks_unavailable"],
  ])("maps verifier error %s to 401 without presigning", async (_l, category) => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(
      makeDeps({
        presignSpy: spy,
        verify: async () => { throw new AgentTokenError(category as any, "nope"); },
      }),
    )(makeReq({ authorization: BEARER, ref: GOOD_REF }), res);
    expect(captured.status).toBe(401);
    expect(spy.called).toBe(false);
  });

  it("403s a client_id outside allowed_client_ids", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(
      makeDeps({
        presignSpy: spy,
        resolveCurrentPublishedAgent: async () => ({
          ...ENABLED_AGENT,
          integrationConfig: { ...ENABLED_AGENT.integrationConfig, allowedClientIds: ["only-this"] },
        }) as any,
      }),
    )(makeReq({ authorization: BEARER, ref: GOOD_REF }), res);
    expect(captured.status).toBe(403);
    expect(captured.body).toContain("client_not_allowed");
    expect(spy.called).toBe(false);
  });

  it("401s when the verified token has no subject", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(
      makeDeps({ presignSpy: spy, verify: async () => ({ clientId: "c" } as any) }),
    )(makeReq({ authorization: BEARER, ref: GOOD_REF }), res);
    expect(captured.status).toBe(401);
    expect(spy.called).toBe(false);
  });

  it.each([
    ["unknown agent", async () => null],
    ["integration disabled", async () => ({
      ...ENABLED_AGENT,
      integrationConfig: { ...ENABLED_AGENT.integrationConfig, enabled: false },
    })],
    ["external access revoked", async () => ({ ...ENABLED_AGENT, externalAccessEnabled: false })],
  ])("404s for %s (no existence disclosure) and never presigns", async (_l, resolve) => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(
      makeDeps({ presignSpy: spy, resolveCurrentPublishedAgent: resolve as any }),
    )(makeReq({ authorization: BEARER, ref: GOOD_REF }), res);
    expect(captured.status).toBe(404);
    expect(spy.called).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Bucket allowlist — the security boundary
// ---------------------------------------------------------------------------

describe("bucket allowlist", () => {
  it("refuses a bucket outside the allowlist even for an authorized caller (400: survives CloudFront)", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ authorization: BEARER, ref: "s3://someone-elses-bucket/secret.txt" }), res,
    );
    expect(captured.status).toBe(400);
    expect(captured.body).toContain("bucket_not_allowed");
    expect(spy.called).toBe(false);
  });

  it("allows the caller's own work in the uploads bucket as well as the media output bucket", async () => {
    for (const ref of [
      `s3://${UPLOAD_BUCKET}/artifacts/${CALLER_SESSION}/output/b.wav`,
      GOOD_REF,
    ]) {
      const spy: PresignSpy = { called: false };
      const { res, captured } = makeRes();
      await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
        makeReq({ authorization: BEARER, ref }), res,
      );
      expect(captured.status, ref).toBe(302);
      expect(`s3://${spy.bucket}/${spy.key}`).toBe(ref);
    }
  });

  it("400s a traversal ref before any presign", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy }))(
      makeReq({ authorization: BEARER, ref: `s3://${MEDIA_BUCKET}/../../etc/passwd` }), res,
    );
    expect(captured.status).toBe(400);
    expect(spy.called).toBe(false);
  });

  it("400s a missing ref", async () => {
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps())(
      makeReq({ authorization: BEARER }), res,
    );
    expect(captured.status).toBe(400);
  });

  it("500s (fails closed) when no buckets are configured", async () => {
    const spy: PresignSpy = { called: false };
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ presignSpy: spy, allowedBuckets: [] }))(
      makeReq({ authorization: BEARER, ref: GOOD_REF }), res,
    );
    expect(captured.status).toBe(500);
    expect(spy.called).toBe(false);
  });

  it("500s when the asset index is unconfigured", async () => {
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(makeDeps({ assetIndexReady: false }))(
      makeReq({ authorization: BEARER, ref: GOOD_REF }), res,
    );
    expect(captured.status).toBe(500);
  });

  it("500s rather than leaking when the presigner throws", async () => {
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(
      makeDeps({ presignGet: async () => { throw new Error("kms down"); } }),
    )(makeReq({ authorization: BEARER, ref: GOOD_REF }), res);
    expect(captured.status).toBe(500);
    expect(captured.body).not.toContain("kms down");
  });
});

// ---------------------------------------------------------------------------
// Per-caller scope — a verified caller reads only THEIR OWN work
// ---------------------------------------------------------------------------
//
// The route used to check only the bucket, so any verified caller of any enabled agent could
// presign any key in either bucket for 12 hours: other sessions' artifacts, `users/<actor>/…`
// lab assets, plan manifests, other callers' media outputs.

describe("per-caller scope", () => {
  async function fetchRef(ref: string, extra: { conversationId?: string; jobs?: Record<string, any>[] } = {}) {
    const spy: PresignSpy = { called: false };
    let jobQueries = 0;
    const { res, captured } = makeRes();
    await createInvokeDownloadHandler(
      makeDeps({
        presignSpy: spy,
        queryActorJobs: async () => { jobQueries += 1; return extra.jobs ?? JOBS; },
      }),
    )(makeReq({ authorization: BEARER, ref, conversationId: extra.conversationId }), res);
    return { captured, spy, jobQueries };
  }

  it("refuses ANOTHER session's artifact, with one indistinguishable answer, before presigning", async () => {
    const { captured, spy } = await fetchRef(`s3://${UPLOAD_BUCKET}/artifacts/${OTHER_CALLER_SESSION}/output/x.mp4`);
    expect(captured.status).toBe(400);
    expect(captured.body).toContain("artifact_not_readable");
    expect(spy.called).toBe(false);
    // An internal user's session (a plain uuid) is equally off limits.
    const internal = await fetchRef(`s3://${UPLOAD_BUCKET}/artifacts/4681f2da-c97f-49e0-aea5-359741dfcb3e/input/v.mp4`);
    expect(internal.captured.status).toBe(400);
    expect(internal.captured.body).toBe(captured.body); // no probing which one exists
    expect(internal.spy.called).toBe(false);
  });

  it("refuses everything in the uploads bucket outside artifacts/ (lab assets, plans, job results)", async () => {
    for (const key of ["users/actor-9/drafts/skills/s1/SKILL.md", "plans/sess/plan/manifest.json", "jobs/j/result.json", "projects/p1/a/x.mp4"]) {
      const { captured, spy } = await fetchRef(`s3://${UPLOAD_BUCKET}/${key}`);
      expect(captured.status, key).toBe(400);
      expect(spy.called, key).toBe(false);
    }
    // …and a bare key resolves to the uploads bucket, so it gets the same rule.
    const bare = await fetchRef("users/actor-9/drafts/skills/s1/SKILL.md");
    expect(bare.captured.status).toBe(400);
  });

  it("serves the caller's OWN uploads (tagged by POST /uploads), and refuses anyone else's, legacy untagged ones, and flat keys", async () => {
    const mine = `artifacts/uploads/${externalArtifactOwnerTag("agent-1", ISSUER, "caller-1")}/input/1-clip.mp4`;
    expect((await fetchRef(`s3://${UPLOAD_BUCKET}/${mine}`)).captured.status).toBe(302);
    for (const key of [
      `artifacts/uploads/${externalArtifactOwnerTag("agent-1", ISSUER, "caller-2")}/input/1-clip.mp4`, // another caller
      `artifacts/uploads/${externalArtifactOwnerTag("agent-2", ISSUER, "caller-1")}/input/1-clip.mp4`, // same sub, other agent
      `artifacts/uploads/${artifactOwnerTag("caller-1")}/input/1-clip.mp4`, // an internal user's upload
      "artifacts/uploads/input/1784754468569-clip.mp4", // legacy, shared by everyone
      "artifacts/1784754468569-clip.mp4", // flat
    ]) {
      const { captured, spy } = await fetchRef(`s3://${UPLOAD_BUCKET}/${key}`);
      expect(captured.status, key).toBe(400);
      expect(spy.called, key).toBe(false);
    }
  });

  it("keeps shared namespaces readable, as they are for every internal user", async () => {
    for (const key of ["artifacts/external/3b1bfb92bd0f6e9934229bf0.jpg", "artifacts/durable/abc/u/clip.mp4"]) {
      const { captured } = await fetchRef(`s3://${UPLOAD_BUCKET}/${key}`);
      expect(captured.status, key).toBe(302);
    }
  });

  it("refuses an M2C output of ANOTHER caller's job, and of no job at all", async () => {
    const other = await fetchRef(`s3://${MEDIA_BUCKET}/micro-workflow/audio-stem/${OTHER_EXEC}/a.wav`);
    expect(other.captured.status).toBe(400);
    expect(other.spy.called).toBe(false);
    const orphan = await fetchRef(`s3://${MEDIA_BUCKET}/micro-workflow/audio-stem/99999999-0000-0000-0000-000000000000/a.wav`);
    expect(orphan.captured.status).toBe(400);
  });

  it("recognises an output listed on the caller's job even without an execution id in its path", async () => {
    const uri = `s3://${MEDIA_BUCKET}/some/other/layout/out.wav`;
    const { captured } = await fetchRef(uri, {
      jobs: [{ jobId: "fal-123", actorId: "owner-1", sessionId: CALLER_SESSION, artifactUrls: [uri] }],
    });
    expect(captured.status).toBe(302);
  });

  it("does not trust a job row of a different OWNER stamped with the caller's session", async () => {
    const { captured } = await fetchRef(GOOD_REF, {
      jobs: [{ jobId: sfnArn(MY_EXEC), actorId: "someone-else", sessionId: CALLER_SESSION }],
    });
    expect(captured.status).toBe(400);
  });

  it("scopes to the conversation in the link: another conversation's artifacts do not open", async () => {
    const convSession = deriveExternalSessionId("agent-1", ISSUER, "caller-1", "conv-7");
    const ref = `s3://${UPLOAD_BUCKET}/artifacts/${convSession}/output/c.mp4`;
    expect((await fetchRef(ref, { conversationId: "conv-7" })).captured.status).toBe(302);
    expect((await fetchRef(ref)).captured.status).toBe(400); // default conversation
    expect((await fetchRef(ref, { conversationId: "conv-8" })).captured.status).toBe(400);
    // Sanitized exactly like invoke and /jobs, so a decorated id still maps to the same session.
    expect((await fetchRef(ref, { conversationId: "conv-7<script>" })).captured.status).toBe(400);
    expect((await fetchRef(ref, { conversationId: "conv/-7".replace("/", "") })).captured.status).toBe(302);
  });

  it("only reads the job registry for an M2C ref", async () => {
    const { jobQueries } = await fetchRef(`s3://${UPLOAD_BUCKET}/artifacts/${CALLER_SESSION}/output/b.wav`);
    expect(jobQueries).toBe(0);
  });
});

describe("per-caller scope — projects-session outputs (asset-lineage Phase 3 (a))", () => {
  const PID = "p" + "6".repeat(32);
  const scope = { sessionId: CALLER_SESSION, ownerActorId: "owner-1", uploadBucket: UPLOAD_BUCKET, mediaBucket: "" };

  it("allows the caller's OWN session output and refuses any other session's — no member clause", async () => {
    const mine = { bucket: UPLOAD_BUCKET, key: `projects/${PID}/sessions/${CALLER_SESSION}/output/a.mp4` };
    const theirs = { bucket: UPLOAD_BUCKET, key: `projects/${PID}/sessions/${OTHER_CALLER_SESSION}/output/a.mp4` };
    expect(await authorizeExternalRef(mine, scope, async () => [])).toEqual({ ok: true });
    expect(await authorizeExternalRef(theirs, scope, async () => [])).toEqual({ ok: false, reason: "other_session" });
    // A project ASSET key stays outside the caller's reach.
    const asset = { bucket: UPLOAD_BUCKET, key: `projects/${PID}/video:x/a.mp4` };
    expect(await authorizeExternalRef(asset, scope, async () => [])).toEqual({ ok: false, reason: "outside_artifacts" });
  });
});
