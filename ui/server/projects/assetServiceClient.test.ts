// Asset_Service client: target selection (Lambda vs the dev-only local URL, refused in production),
// the event shape, the dev-bypass actor confined to the LOCAL target, and reply mapping.
import { describe, expect, it } from "vitest";
import {
  createAssetServiceClient,
  mapAssetServicePayload,
  resolveAssetServiceTarget,
} from "./assetServiceClient";
import type { IssuerHttpRequest, IssuerHttpResponse } from "./tokenIssuerClient";

const PID = "p0123456789abcdef0123456789abcdef";

function recorder(reply: Partial<IssuerHttpResponse> = {}) {
  const seen: IssuerHttpRequest[] = [];
  const transport = async (req: IssuerHttpRequest): Promise<IssuerHttpResponse> => {
    seen.push(req);
    return { status: 200, headers: {}, body: JSON.stringify({ ok: true, value: { asset: { assetId: "x" } } }), ...reply };
  };
  return { seen, transport };
}

const signer = async ({ headers }: { headers: Record<string, string> }) => ({ ...headers, authorization: "AWS4-HMAC-SHA256 signed" });

describe("resolveAssetServiceTarget", () => {
  it("prefers LOCAL_ASSET_SERVICE_URL outside production, else ASSET_SERVICE_FN", () => {
    expect(resolveAssetServiceTarget({ LOCAL_ASSET_SERVICE_URL: "http://127.0.0.1:8099/", ASSET_SERVICE_FN: "fn" })).toEqual({
      kind: "local",
      url: "http://127.0.0.1:8099/invoke",
    });
    expect(resolveAssetServiceTarget({ ASSET_SERVICE_FN: "arn:aws:lambda:eu-west-1:123:function:asset" })).toMatchObject({
      kind: "lambda",
      region: "eu-west-1",
      hostname: "lambda.eu-west-1.amazonaws.com",
    });
    expect(resolveAssetServiceTarget({})).toMatchObject({ kind: "none" });
  });

  it("refuses the local URL when NODE_ENV=production (warns, falls back to the Lambda)", () => {
    const warnings: string[] = [];
    const t = resolveAssetServiceTarget(
      { NODE_ENV: "production", LOCAL_ASSET_SERVICE_URL: "http://127.0.0.1:8099", ASSET_SERVICE_FN: "fn" },
      (m) => warnings.push(m),
    );
    expect(t.kind).toBe("lambda");
    expect(warnings[0]).toContain("dev-only");
    expect(resolveAssetServiceTarget({ NODE_ENV: "production", LOCAL_ASSET_SERVICE_URL: "http://x" }, () => {}).kind).toBe("none");
  });
});

describe("createAssetServiceClient", () => {
  it("signs a RequestResponse Invoke of ASSET_SERVICE_FN with the proxy-caller event", async () => {
    const { seen, transport } = recorder();
    const client = createAssetServiceClient({ env: { ASSET_SERVICE_FN: "asset-fn", AWS_REGION: "us-west-2" }, transport, signer });
    const r = await client.invoke({ userToken: "jwt", projectId: PID, command: "remove", args: { assetId: "a" }, devBypassActor: "dev" });
    expect(r).toEqual({ ok: true, value: { asset: { assetId: "x" } } });
    expect(seen[0].url).toBe("https://lambda.us-west-2.amazonaws.com/2015-03-31/functions/asset-fn/invocations");
    expect(seen[0].headers.authorization).toContain("AWS4-HMAC-SHA256");
    expect(seen[0].headers["x-amz-invocation-type"]).toBe("RequestResponse");
    expect(JSON.parse(seen[0].body)).toEqual({ userToken: "jwt", projectId: PID, command: "remove", args: { assetId: "a" }, caller: "proxy" });
  });

  it("never sends the dev-bypass actor to the Lambda, only to the local service", async () => {
    const lambda = recorder();
    await createAssetServiceClient({ env: { ASSET_SERVICE_FN: "fn" }, transport: lambda.transport, signer }).invoke({
      projectId: PID, command: "capabilities", args: {}, devBypassActor: "local-dev-user",
    });
    expect(JSON.parse(lambda.seen[0].body).devBypassActor).toBeUndefined();

    const local = recorder();
    await createAssetServiceClient({ env: { LOCAL_ASSET_SERVICE_URL: "http://127.0.0.1:8099" }, transport: local.transport, signer }).invoke({
      projectId: PID, command: "capabilities", args: {}, devBypassActor: "local-dev-user",
    });
    expect(local.seen[0].url).toBe("http://127.0.0.1:8099/invoke");
    expect(local.seen[0].headers.authorization).toBeUndefined();
    expect(JSON.parse(local.seen[0].body).devBypassActor).toBe("local-dev-user");
  });

  it("maps every failure to 503 asset_service_unavailable", async () => {
    const cases: Partial<IssuerHttpResponse>[] = [
      { status: 500 },
      { headers: { "x-amz-function-error": "Unhandled" } },
      { body: "not json" },
      { body: JSON.stringify({ hello: 1 }) },
    ];
    for (const reply of cases) {
      const { transport } = recorder(reply);
      const r = await createAssetServiceClient({ env: { ASSET_SERVICE_FN: "fn" }, transport, signer }).invoke({
        projectId: PID, command: "remove", args: {},
      });
      expect(r).toMatchObject({ ok: false, status: 503, error: { code: "asset_service_unavailable" } });
    }
    const none = await createAssetServiceClient({ env: {} }).invoke({ projectId: PID, command: "remove", args: {} });
    expect(none).toMatchObject({ ok: false, status: 503 });
    const slow = await createAssetServiceClient({
      env: { LOCAL_ASSET_SERVICE_URL: "http://127.0.0.1:1" },
      transport: () => new Promise(() => {}),
      budgetMs: 20,
    }).invoke({ projectId: PID, command: "remove", args: {} });
    expect(slow).toMatchObject({ ok: false, status: 503, error: { reason: "timeout" } });
  });
});

describe("mapAssetServicePayload", () => {
  it("keeps a domain refusal's status and code", () => {
    expect(mapAssetServicePayload({ ok: false, status: 409, error: { code: "object_missing", message: "m" } })).toEqual({
      ok: false, status: 409, error: { code: "object_missing", message: "m" },
    });
    expect(mapAssetServicePayload({ ok: false, status: 200, error: { code: "x" } })).toMatchObject({ status: 503 });
  });
});
