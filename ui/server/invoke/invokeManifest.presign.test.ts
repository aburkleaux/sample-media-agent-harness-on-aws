// External render-manifest route — artifact URL rewrite.
//
// When the artifacts CDN is private, the external console cannot load the CloudFront media URLs a
// manifest carries. After the (unchanged) 5-step gate and key validation, the route loads the
// manifest and replaces every string that is one of OUR artifact URLs with a presigned GET (1h).
// Bare keys and foreign URLs are left alone; the stored manifest is never written.
import { describe, it, expect, vi } from "vitest";
import { AgentTokenError } from "../auth/agentOidc";
import {
  createInvokeManifestHandler,
  presignManifestArtifactUrls,
  MANIFEST_MEDIA_URL_TTL_S,
  type InvokeManifestHandlerDeps,
} from "./invokeManifestRoute";

const D = "d111.cloudfront.net";
const KEY = "artifacts/sess-abc/render/manifest-123.json";

const ENABLED_AGENT = {
  ownerUserId: "owner-1",
  externalAccessEnabled: true,
  integrationConfig: { enabled: true, issuer: "https://issuer.example.com", audience: "aud-1", allowedClientIds: [] },
};

const MANIFEST = {
  id: "m",
  sourceKey: "artifacts/sess-abc/output/final.mp4",
  tracks: [
    { src: `https://${D}/artifacts/sess-abc/output/final.mp4`, label: "main" },
    { src: "/api/artifact?key=artifacts/sess-abc/output/audio.wav" },
    { src: "https://third-party.example.com/artifacts/x.mp4" },
  ],
  clips: [{ sourceUrl: `https://${D}/artifacts/sess-abc/output/final.mp4`, start: 0, duration: 3 }],
  poster: `https://${D}/skills/not-an-artifact.png`,
  nested: { deep: [[`https://${D}/artifacts/durable/h.png`]] },
  n: 1,
  ok: true,
  nil: null,
};

function makeRes() {
  const out = { status: 0, headers: {} as Record<string, string>, body: "", headersSent: false, writableEnded: false };
  const res: any = {
    get headersSent() { return out.headersSent; },
    get writableEnded() { return out.writableEnded; },
    writeHead(s: number, h?: Record<string, string>) { out.status = s; Object.assign(out.headers, h ?? {}); out.headersSent = true; return res; },
    end(c?: any) { if (c) out.body += String(c); out.writableEnded = true; return res; },
  };
  return { res, out };
}
const req = (auth = "Bearer t", key = KEY) => ({
  params: { agentId: "agent-1" },
  headers: auth ? { authorization: auth } : {},
  url: `/invoke/agent-1/manifest?key=${encodeURIComponent(key)}`,
});

function deps(overrides: Partial<InvokeManifestHandlerDeps> = {}) {
  const presignArtifact = vi.fn(async (key: string, ttl: number) => `https://s3.example/${key}?X-Amz-Expires=${ttl}&X-Amz-Signature=s`);
  const readObjectText = vi.fn(async () => JSON.stringify(MANIFEST));
  const d: InvokeManifestHandlerDeps = {
    assetIndexReady: true,
    uploadReady: true,
    resolveCurrentPublishedAgent: async () => ENABLED_AGENT as any,
    verifyAgentToken: async () => ({ sub: "caller-1", clientId: "c" }),
    readObjectText,
    presignArtifact,
    artifactsDomain: D,
    ...overrides,
  };
  return { d, presignArtifact, readObjectText };
}

describe("presignManifestArtifactUrls", () => {
  it("replaces only our artifact URLs, deduping signatures per key", async () => {
    const presign = vi.fn(async (k: string) => `SIGNED:${k}`);
    const out: any = await presignManifestArtifactUrls(MANIFEST, D, presign);
    expect(out.tracks[0].src).toBe("SIGNED:artifacts/sess-abc/output/final.mp4");
    expect(out.tracks[1].src).toBe("SIGNED:artifacts/sess-abc/output/audio.wav");
    expect(out.tracks[2].src).toBe("https://third-party.example.com/artifacts/x.mp4");
    expect(out.clips[0].sourceUrl).toBe("SIGNED:artifacts/sess-abc/output/final.mp4");
    expect(out.nested.deep[0][0]).toBe("SIGNED:artifacts/durable/h.png");
    // Untouched: bare key identifiers, non-artifact CDN paths, non-strings.
    expect(out.sourceKey).toBe(MANIFEST.sourceKey);
    expect(out.poster).toBe(MANIFEST.poster);
    expect(out.tracks[0].label).toBe("main");
    expect([out.n, out.ok, out.nil]).toEqual([1, true, null]);
    // final.mp4 appears twice but is signed once; every signature uses the 1h TTL.
    expect(presign).toHaveBeenCalledTimes(3);
    for (const call of presign.mock.calls) expect(call[1]).toBe(MANIFEST_MEDIA_URL_TTL_S);
    expect(MANIFEST_MEDIA_URL_TTL_S).toBe(3600);
  });

  it("does not mutate the input", async () => {
    const copy = JSON.parse(JSON.stringify(MANIFEST));
    await presignManifestArtifactUrls(copy, D, async () => "X");
    expect(copy).toEqual(MANIFEST);
  });
});

describe("GET /invoke/:agentId/manifest — rewrite mode", () => {
  it("answers the manifest with presigned media URLs for an authorized caller", async () => {
    const { d, readObjectText } = deps();
    const { res, out } = makeRes();
    await createInvokeManifestHandler(d)(req(), res);
    expect(out.status).toBe(200);
    expect(out.headers["content-type"]).toBe("application/json");
    expect(out.headers["cache-control"]).toBe("no-store");
    expect(readObjectText).toHaveBeenCalledWith(KEY);
    const body = JSON.parse(out.body);
    expect(body.tracks[0].src).toBe("https://s3.example/artifacts/sess-abc/output/final.mp4?X-Amz-Expires=3600&X-Amz-Signature=s");
    expect(body.tracks[1].src).toContain("https://s3.example/artifacts/sess-abc/output/audio.wav");
    expect(body.sourceKey).toBe("artifacts/sess-abc/output/final.mp4");
    expect(out.body).not.toContain(`https://${D}/artifacts/`);
  });

  it("passes a non-JSON body through unchanged", async () => {
    const { d, presignArtifact } = deps({ readObjectText: async () => "not json" });
    const { res, out } = makeRes();
    await createInvokeManifestHandler(d)(req(), res);
    expect(out.status).toBe(200);
    expect(out.body).toBe("not json");
    expect(presignArtifact).not.toHaveBeenCalled();
  });

  it("404s a missing manifest", async () => {
    const { d } = deps({
      readObjectText: async () => { const e: any = new Error("missing"); e.name = "NoSuchKey"; throw e; },
    });
    const { res, out } = makeRes();
    await createInvokeManifestHandler(d)(req(), res);
    expect(out.status).toBe(404);
  });

  it("keeps the gate: no token / bad token / bad key never read or presign", async () => {
    for (const [r, verify, status] of [
      [req(""), undefined, 401],
      [req("Bearer t"), async () => { throw new AgentTokenError("expired", "x"); }, 401],
      [req("Bearer t", "artifacts/sess/render/../x.json"), undefined, 400],
    ] as const) {
      const { d, presignArtifact, readObjectText } = deps(verify ? { verifyAgentToken: verify as any } : {});
      const { res, out } = makeRes();
      await createInvokeManifestHandler(d)(r, res);
      expect(out.status).toBe(status);
      expect(readObjectText).not.toHaveBeenCalled();
      expect(presignArtifact).not.toHaveBeenCalled();
    }
  });

  it("404s an unpublished agent without verifying, reading, or presigning", async () => {
    const verifyAgentToken = vi.fn(async () => ({ sub: "x" }));
    const { d, presignArtifact, readObjectText } = deps({ resolveCurrentPublishedAgent: async () => null, verifyAgentToken });
    const { res, out } = makeRes();
    await createInvokeManifestHandler(d)(req(), res);
    expect(out.status).toBe(404);
    expect(verifyAgentToken).not.toHaveBeenCalled();
    expect(readObjectText).not.toHaveBeenCalled();
    expect(presignArtifact).not.toHaveBeenCalled();
  });

  it("500s when neither reader is configured", async () => {
    const { d } = deps({ readObjectText: undefined, presignArtifact: undefined });
    const { res, out } = makeRes();
    await createInvokeManifestHandler(d)(req(), res);
    expect(out.status).toBe(500);
  });
});

describe("presignManifestArtifactUrls — projects-session outputs (asset-lineage Phase 3 (a))", () => {
  const PID = "p" + "5".repeat(32);
  const own = `projects/${PID}/sessions/sess-abcdef/output/clip.mp4`;
  const other = `projects/${PID}/sessions/sess-zzzzzz/output/clip.mp4`;

  it("presigns only the manifest's OWN session; never another session's output", async () => {
    const out: any = await presignManifestArtifactUrls(
      { a: `https://${D}/${own}`, b: `https://${D}/${other}` }, D, async (k) => `https://signed/${k}`, "sess-abcdef");
    expect(out.a).toBe(`https://signed/${own}`);
    expect(out.b).toBe(`https://${D}/${other}`);
  });
});
