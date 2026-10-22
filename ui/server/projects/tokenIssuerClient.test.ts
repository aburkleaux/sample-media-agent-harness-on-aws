import { describe, expect, it } from "vitest";
import {
  createTokenIssuerClient,
  mapIssuerPayload,
  PROJECT_TOKEN_HEADER,
  resolveIssuerTarget,
  type IssuerHttpRequest,
  type IssuerHttpResponse,
  type IssuerTransport,
  type LambdaSigner,
} from "./tokenIssuerClient";

const PID = "p0123456789abcdef0123456789abcdef";
const FN_ARN = "arn:aws:lambda:us-east-1:111122223333:function:mediaworks-harness-token-issuer";

function ok(body: unknown, headers: Record<string, string> = {}): IssuerHttpResponse {
  return { status: 200, headers, body: JSON.stringify(body) };
}

function harness(env: NodeJS.ProcessEnv, respond: (req: IssuerHttpRequest) => Promise<IssuerHttpResponse>, budgetMs = 2_000) {
  const requests: IssuerHttpRequest[] = [];
  const signed: Parameters<LambdaSigner>[0][] = [];
  const warnings: string[] = [];
  const transport: IssuerTransport = (req) => {
    requests.push(req);
    return respond(req);
  };
  const signer: LambdaSigner = async (req) => {
    signed.push(req);
    return { ...req.headers, authorization: "AWS4-HMAC-SHA256 test" };
  };
  const client = createTokenIssuerClient({ env, transport, signer, budgetMs, warn: (m) => warnings.push(m) });
  return { client, requests, signed, warnings };
}

describe("PROJECT_TOKEN_HEADER", () => {
  it("matches the agent's header name", () => {
    expect(PROJECT_TOKEN_HEADER).toBe("X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken");
  });
});

describe("Lambda path", () => {
  it("signs an Invoke of TOKEN_ISSUER_FN and maps ok", async () => {
    const h = harness({ TOKEN_ISSUER_FN: FN_ARN }, async () => ok({ status: "ok", token: "jwt", exp: 1_900_000_000, jti: "j" }));
    await expect(h.client.issueProjectToken("user-tok", PID)).resolves.toEqual({ status: "ok", token: "jwt", exp: 1_900_000_000 });
    expect(h.signed).toHaveLength(1);
    expect(h.signed[0].region).toBe("us-east-1");
    expect(h.requests[0].url).toBe(
      `https://lambda.us-east-1.amazonaws.com/2015-03-31/functions/${encodeURIComponent(FN_ARN)}/invocations`,
    );
    expect(h.requests[0].headers.authorization).toMatch(/^AWS4-HMAC-SHA256/);
    expect(JSON.parse(h.requests[0].body)).toEqual({ userToken: "user-tok", projectId: PID });
  });

  it("maps not_found, unauthenticated, error, function errors, and non-2xx", async () => {
    const cases: [IssuerHttpResponse, string][] = [
      [ok({ status: "not_found" }), "not_found"],
      [ok({ status: "unauthenticated" }), "unauthenticated"],
      [ok({ status: "error" }), "unavailable"],
      [ok({ errorMessage: "boom" }, { "x-amz-function-error": "Unhandled" }), "unavailable"],
      [{ status: 403, headers: {}, body: "{}" }, "unavailable"],
      [{ status: 200, headers: {}, body: "not json" }, "unavailable"],
    ];
    for (const [res, want] of cases) {
      const h = harness({ TOKEN_ISSUER_FN: "token-issuer", AWS_REGION: "us-west-2" }, async () => res);
      expect((await h.client.issueProjectToken("u", PID)).status).toBe(want);
    }
  });

  it("returns unavailable on a transport failure, and when unconfigured", async () => {
    const h = harness({ TOKEN_ISSUER_FN: "token-issuer" }, async () => {
      throw new Error("ECONNRESET");
    });
    expect(await h.client.issueProjectToken("u", PID)).toEqual({ status: "unavailable", reason: "request failed: ECONNRESET" });
    const none = harness({}, async () => ok({ status: "ok", token: "t", exp: 1 }));
    expect((await none.client.issueProjectToken("u", PID)).status).toBe("unavailable");
    expect(none.requests).toHaveLength(0);
  });

  it("aborts at the budget and reports unavailable", async () => {
    let aborted = false;
    const h = harness(
      { TOKEN_ISSUER_FN: "token-issuer" },
      (req) =>
        new Promise(() => {
          req.signal.addEventListener("abort", () => {
            aborted = true;
          });
        }),
      30,
    );
    const t0 = Date.now();
    expect(await h.client.issueProjectToken("u", PID)).toEqual({ status: "unavailable", reason: "timeout" });
    expect(Date.now() - t0).toBeLessThan(1_000);
    expect(aborted).toBe(true);
  });

  it("short-circuits an empty User_Token and a malformed Project id", async () => {
    const h = harness({ TOKEN_ISSUER_FN: "token-issuer" }, async () => ok({ status: "ok", token: "t", exp: 1 }));
    expect(await h.client.issueProjectToken("", PID)).toEqual({ status: "unauthenticated" });
    expect(await h.client.issueProjectToken("u", "default")).toEqual({ status: "not_found" });
    expect(h.requests).toHaveLength(0);
  });
});

describe("LOCAL_TOKEN_ISSUER_URL (dev only)", () => {
  it("POSTs unsigned JSON to /issue with the same mapping", async () => {
    const h = harness({ LOCAL_TOKEN_ISSUER_URL: "http://127.0.0.1:8098/", TOKEN_ISSUER_FN: "token-issuer" }, async () =>
      ok({ status: "ok", token: "dev-jwt", exp: 1_900_000_000 }),
    );
    expect(await h.client.issueProjectToken("dev-user-tok", PID)).toEqual({ status: "ok", token: "dev-jwt", exp: 1_900_000_000 });
    expect(h.requests[0].url).toBe("http://127.0.0.1:8098/issue");
    expect(h.requests[0].headers).toEqual({ "content-type": "application/json" });
    expect(JSON.parse(h.requests[0].body)).toEqual({ userToken: "dev-user-tok", projectId: PID });
    expect(h.signed).toHaveLength(0);
  });

  it("is refused under NODE_ENV=production and falls back to TOKEN_ISSUER_FN", async () => {
    const h = harness(
      { NODE_ENV: "production", LOCAL_TOKEN_ISSUER_URL: "http://127.0.0.1:8098", TOKEN_ISSUER_FN: "token-issuer" },
      async () => ok({ status: "not_found" }),
    );
    await h.client.issueProjectToken("u", PID);
    await h.client.issueProjectToken("u", PID);
    expect(h.requests.every((r) => r.url.startsWith("https://lambda."))).toBe(true);
    expect(h.warnings).toHaveLength(1);
    expect(resolveIssuerTarget({ NODE_ENV: "production", LOCAL_TOKEN_ISSUER_URL: "http://x" }, () => {}).kind).toBe("none");
  });

  it("times out like the Lambda path", async () => {
    const h = harness({ LOCAL_TOKEN_ISSUER_URL: "http://127.0.0.1:8098" }, () => new Promise(() => {}), 20);
    expect(await h.client.issueProjectToken("u", PID)).toEqual({ status: "unavailable", reason: "timeout" });
  });
});

describe("mapIssuerPayload", () => {
  it("rejects an ok without a usable token or exp", () => {
    expect(mapIssuerPayload({ status: "ok", token: "", exp: 1 }).status).toBe("unavailable");
    expect(mapIssuerPayload({ status: "ok", token: "t", exp: "1" }).status).toBe("unavailable");
    expect(mapIssuerPayload({ status: "ok", token: "t", exp: 1.5 }).status).toBe("unavailable");
    expect(mapIssuerPayload(null).status).toBe("unavailable");
  });

  it("never puts the token into a reason", () => {
    const r = mapIssuerPayload({ status: "weird", token: "secret-token" });
    expect(JSON.stringify(r)).not.toContain("secret-token");
  });
});
