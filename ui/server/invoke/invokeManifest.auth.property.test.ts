// External render-manifest route — auth gate + key-prefix validation.
//
// `GET /invoke/:agentId/manifest` serves a render manifest JSON same-origin to an
// external OIDC caller. It MUST (a) apply the identical per-agent OIDC gate as
// `/invoke` and never read S3 on any auth failure, (b) reject any `key` that is
// not a render manifest under `artifacts/**/render/*.json` (no traversal, no
// arbitrary objects) BEFORE touching S3, and (c) only stream the object once both
// the caller and the key are accepted.
//
// The route takes injected deps (resolve / verify / readObject), so unlike the
// invoke POST it can be driven directly here — no SigV4 signer or live runtime.

import { describe, it, expect } from "vitest";
import fc from "fast-check";

import { AgentTokenError, type VerifiedAgentToken } from "../auth/agentOidc";
import {
  createInvokeManifestHandler,
  validateManifestKey,
  RENDER_MANIFEST_KEY_RE,
  type InvokeManifestHandlerDeps,
} from "./invokeManifestRoute";

// ---------------------------------------------------------------------------
// Minimal req/res doubles capturing status, headers, and body.
// ---------------------------------------------------------------------------

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
    get headersSent() {
      return captured.headersSent;
    },
    get writableEnded() {
      return captured.writableEnded;
    },
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
  key?: string;
}): any {
  const q = opts.key === undefined ? "" : `?key=${encodeURIComponent(opts.key)}`;
  return {
    params: { agentId: opts.agentId ?? "agent-1" },
    headers: opts.authorization ? { authorization: opts.authorization } : {},
    url: `/invoke/${opts.agentId ?? "agent-1"}/manifest${q}`,
  };
}

// A published agent row with external integration enabled.
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

const VALID_KEY = "artifacts/sess-abc/render/manifest-123.json";

/** Build handler deps, letting each test override the behavior it exercises. */
function makeDeps(
  overrides: Partial<InvokeManifestHandlerDeps> & {
    verify?: () => Promise<VerifiedAgentToken>;
    readObjectSpy?: { called: boolean };
  } = {},
): InvokeManifestHandlerDeps {
  const readObjectSpy = overrides.readObjectSpy ?? { called: false };
  return {
    assetIndexReady: overrides.assetIndexReady ?? true,
    uploadReady: overrides.uploadReady ?? true,
    resolveCurrentPublishedAgent:
      overrides.resolveCurrentPublishedAgent ??
      (async () => ENABLED_AGENT as any),
    verifyAgentToken:
      overrides.verifyAgentToken ??
      (overrides.verify
        ? async () => overrides.verify!()
        : async () => ({ sub: "caller-1", clientId: "client-1" })),
    readObject:
      overrides.readObject ??
      (async (_key: string, res: any) => {
        readObjectSpy.called = true;
        res.writeHead(200, { "content-type": "application/json" });
        res.end('{"id":"m","duration":1,"tracks":[],"clips":[],"createdAt":"x"}');
      }),
  };
}

// ---------------------------------------------------------------------------
// Key-prefix validation
// ---------------------------------------------------------------------------

describe("validateManifestKey", () => {
  it("accepts a well-formed render manifest key", () => {
    expect(validateManifestKey(VALID_KEY)).toBe(VALID_KEY);
    expect(validateManifestKey("artifacts/a/b/render/x_y-1.JSON".toLowerCase())).toBe(
      "artifacts/a/b/render/x_y-1.json",
    );
    expect(
      validateManifestKey("artifacts/2026/session-1/render/abc.json"),
    ).toBe("artifacts/2026/session-1/render/abc.json");
  });

  it("rejects non-render / traversal / malformed keys", () => {
    const bad = [
      "",
      "artifacts/render/x.json", // no session segment before render/
      "artifacts/sess/render/", // no filename
      "artifacts/sess/render/x.txt", // not .json
      "artifacts/sess/renders/x.json", // wrong dir
      "artifacts/sess/render/../../secret.json", // traversal
      "artifacts/../render/x.json", // traversal
      "/artifacts/sess/render/x.json", // leading slash
      "other/sess/render/x.json", // outside artifacts/
      "artifacts/sess/render/x.json/../y", // trailing traversal
      "artifacts\\sess\\render\\x.json", // backslashes
      "artifacts/sess/render/sub/x.json", // extra dir after render/
      42,
      null,
      undefined,
    ];
    for (const k of bad) {
      expect(validateManifestKey(k as unknown)).toBeNull();
    }
  });

  it("regex never matches a key containing '..' (property)", () => {
    fc.assert(
      fc.property(fc.string(), (s) => {
        const k = `artifacts/${s}/render/x.json`;
        if (k.includes("..")) {
          // validateManifestKey rejects any '..' regardless of regex.
          expect(validateManifestKey(k)).toBeNull();
        }
        // The regex itself must never accept a key with a slashless '..' segment.
        if (RENDER_MANIFEST_KEY_RE.test(k)) {
          expect(k.split("/").every((seg) => seg !== "..")).toBe(true);
        }
      }),
      { numRuns: 200 },
    );
  });
});

// ---------------------------------------------------------------------------
// Auth gate: never read S3 on any auth/authorization failure.
// ---------------------------------------------------------------------------

describe("GET /invoke/:agentId/manifest auth gate", () => {
  it("500 when asset index or bucket is not configured (no read)", async () => {
    for (const partial of [{ assetIndexReady: false }, { uploadReady: false }]) {
      const spy = { called: false };
      const handler = createInvokeManifestHandler(
        makeDeps({ ...partial, readObjectSpy: spy } as any),
      );
      const { res, captured } = makeRes();
      await handler(makeReq({ authorization: "Bearer t", key: VALID_KEY }), res);
      expect(captured.status).toBe(500);
      expect(spy.called).toBe(false);
    }
  });

  it("404 when there is no current published version — WITHOUT verifying", async () => {
    let verifyCalled = false;
    const spy = { called: false };
    const handler = createInvokeManifestHandler(
      makeDeps({
        resolveCurrentPublishedAgent: async () => null,
        verifyAgentToken: async () => {
          verifyCalled = true;
          return { sub: "x" };
        },
        readObjectSpy: spy,
      }),
    );
    const { res, captured } = makeRes();
    await handler(makeReq({ authorization: "Bearer t", key: VALID_KEY }), res);
    expect(captured.status).toBe(404);
    expect(verifyCalled).toBe(false);
    expect(spy.called).toBe(false);
  });

  it("404 when external access is revoked — WITHOUT verifying", async () => {
    let verifyCalled = false;
    const spy = { called: false };
    const handler = createInvokeManifestHandler(
      makeDeps({
        resolveCurrentPublishedAgent: async () =>
          ({ ...ENABLED_AGENT, externalAccessEnabled: false }) as any,
        verifyAgentToken: async () => {
          verifyCalled = true;
          return { sub: "x" };
        },
        readObjectSpy: spy,
      }),
    );
    const { res, captured } = makeRes();
    await handler(makeReq({ authorization: "Bearer t", key: VALID_KEY }), res);
    expect(captured.status).toBe(404);
    expect(verifyCalled).toBe(false);
    expect(spy.called).toBe(false);
  });

  it("401 when the Bearer token is missing (no read)", async () => {
    const spy = { called: false };
    const handler = createInvokeManifestHandler(makeDeps({ readObjectSpy: spy }));
    const { res, captured } = makeRes();
    await handler(makeReq({ key: VALID_KEY }), res);
    expect(captured.status).toBe(401);
    expect(spy.called).toBe(false);
  });

  it("maps verifier AgentTokenError categories to their status (no read)", async () => {
    const cases: Array<[any, number]> = [
      ["invalid_token", 401],
      ["expired", 401],
      ["issuer_mismatch", 401],
      ["audience_mismatch", 401],
    ];
    for (const [category, status] of cases) {
      const spy = { called: false };
      const handler = createInvokeManifestHandler(
        makeDeps({
          verify: async () => {
            throw new AgentTokenError(category, `simulated ${category}`);
          },
          readObjectSpy: spy,
        }),
      );
      const { res, captured } = makeRes();
      await handler(
        makeReq({ authorization: "Bearer t", key: VALID_KEY }),
        res,
      );
      expect(captured.status).toBe(status);
      expect(spy.called).toBe(false);
    }
  });

  it("403 when the token client_id is not in the allowlist (no read)", async () => {
    const spy = { called: false };
    const handler = createInvokeManifestHandler(
      makeDeps({
        resolveCurrentPublishedAgent: async () =>
          ({
            ...ENABLED_AGENT,
            integrationConfig: {
              ...ENABLED_AGENT.integrationConfig,
              allowedClientIds: ["allowed-client"],
            },
          }) as any,
        verify: async () => ({ sub: "caller-1", clientId: "other-client" }),
        readObjectSpy: spy,
      }),
    );
    const { res, captured } = makeRes();
    await handler(makeReq({ authorization: "Bearer t", key: VALID_KEY }), res);
    expect(captured.status).toBe(403);
    expect(spy.called).toBe(false);
  });

  it("401 when the verified token has no subject (no read)", async () => {
    const spy = { called: false };
    const handler = createInvokeManifestHandler(
      makeDeps({
        verify: async () => ({ sub: "" as any, clientId: "c" }),
        readObjectSpy: spy,
      }),
    );
    const { res, captured } = makeRes();
    await handler(makeReq({ authorization: "Bearer t", key: VALID_KEY }), res);
    expect(captured.status).toBe(401);
    expect(spy.called).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// After auth: key validation gates the S3 read; a valid key streams the object.
// ---------------------------------------------------------------------------

describe("GET /invoke/:agentId/manifest key gate + stream", () => {
  it("400 for an authorized caller with a bad key — WITHOUT reading S3", async () => {
    const spy = { called: false };
    const handler = createInvokeManifestHandler(makeDeps({ readObjectSpy: spy }));
    const { res, captured } = makeRes();
    await handler(
      makeReq({
        authorization: "Bearer t",
        key: "artifacts/sess/render/../secret.json",
      }),
      res,
    );
    expect(captured.status).toBe(400);
    expect(captured.body).toContain("invalid_key");
    expect(spy.called).toBe(false);
  });

  it("400 when the key is absent — WITHOUT reading S3", async () => {
    const spy = { called: false };
    const handler = createInvokeManifestHandler(makeDeps({ readObjectSpy: spy }));
    const { res, captured } = makeRes();
    await handler(makeReq({ authorization: "Bearer t" }), res);
    expect(captured.status).toBe(400);
    expect(spy.called).toBe(false);
  });

  it("streams the manifest for an authorized caller with a valid key", async () => {
    const spy = { called: false };
    const handler = createInvokeManifestHandler(makeDeps({ readObjectSpy: spy }));
    const { res, captured } = makeRes();
    await handler(
      makeReq({ authorization: "Bearer t", key: VALID_KEY }),
      res,
    );
    expect(captured.status).toBe(200);
    expect(spy.called).toBe(true);
    expect(captured.body).toContain('"tracks"');
  });

  it("404 when a validated key's object is missing (NoSuchKey → 404)", async () => {
    const handler = createInvokeManifestHandler(
      makeDeps({
        readObject: async () => {
          const err: any = new Error("missing");
          err.name = "NoSuchKey";
          throw err;
        },
      }),
    );
    const { res, captured } = makeRes();
    await handler(
      makeReq({ authorization: "Bearer t", key: VALID_KEY }),
      res,
    );
    expect(captured.status).toBe(404);
  });
});
