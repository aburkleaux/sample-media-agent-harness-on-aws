// The Moments tab's own search: POST /projects/:pid/moments/search (no agent turn, no LLM).
import { describe, it, expect, vi } from "vitest";
import { PassThrough } from "node:stream";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  dispatchMomentsSearch, hitsOfResult, highlightTerms, KINDS, matchedText, mcpToolCaller, parseMomentsSearchRequest,
  shapeMomentCards,
} from "./momentsSearch";

const PID = "p" + "a".repeat(32);
const V1 = "video:11111111-1111-4111-8111-111111111111";
const V2 = "video:22222222-2222-4222-8222-222222222222";
const I1 = "image:33333333-3333-4333-8333-333333333333";
const D1 = "document:44444444-4444-4444-8444-444444444444";

const hit = (asset_id: string, start_ms: number, end_ms: number, extra: Record<string, unknown> = {}) =>
  ({ asset_id, start_ms, end_ms, final_score: 0.9, source_index: "scene", text: "a man drives", ...extra });

function reqWith(body: unknown, auth = "Bearer user-token") {
  const r: any = new PassThrough();
  r.headers = { authorization: auth };
  r.end(typeof body === "string" ? body : JSON.stringify(body));
  return r;
}

function resCapture() {
  const out: { status?: number; body?: any } = {};
  const res: any = {
    headersSent: false,
    writeHead(status: number) { out.status = status; this.headersSent = true; },
    end(b: string) { out.body = b ? JSON.parse(b) : undefined; },
  };
  return { res, out };
}

const rows = (list: Array<[string, string, string, string?]>) =>
  list.map(([assetId, kind, displayName, status]) => ({ assetId, kind, displayName, status: status ?? "indexed" }));

function deps(overrides: Record<string, unknown> = {}) {
  const calls: Array<[string, string, Record<string, unknown>]> = [];
  return {
    calls,
    d: {
      checkAccess: vi.fn(async () => ({ ok: true })) as any,
      issueProjectToken: vi.fn(async () => ({ status: "ok", token: "proj-token", exp: 0 })) as any,
      store: { batchGet: vi.fn(async () => rows([[V1, "video", "Meridian.mp4"], [V2, "video", "Gone.mp4", "removed"], [I1, "image", "still.jpg"]])) },
      toolName: "target___hybrid-search___search_content",
      callTool: vi.fn(async (token: string, name: string, args: Record<string, unknown>) => {
        calls.push([token, name, args]);
        return JSON.stringify({ results: [hit(V1, 1000, 4000), hit(V2, 0, 1000), hit(I1, 0, 1000), hit(V1, 1000, 4000)] });
      }),
      ...overrides,
    },
  };
}

const ctx = { actor: "actor-1", pid: PID, parts: [PID, "moments", "search"], method: "POST" };

describe("parseMomentsSearchRequest", () => {
  it("accepts a query, optional kinds and k", () => {
    expect(parseMomentsSearchRequest({ query: "  night drive @Meridian.mp4 #topic " }))
      .toEqual({ query: "night drive @Meridian.mp4 #topic", images: null, kinds: null, k: 50 });
    expect(parseMomentsSearchRequest({ query: "x", kinds: ["video", "video"], k: 5 })).toEqual({ query: "x", images: null, kinds: ["video"], k: 5 });
    expect(parseMomentsSearchRequest({ query: "x", kinds: ["video", "image", "audio", "document"] })).toMatchObject({ kinds: null });
    expect(parseMomentsSearchRequest({ query: "x", kinds: ["video", "image", "audio"] })).toMatchObject({ kinds: ["video", "image", "audio"] });
  });
  it("rejects bad input", () => {
    for (const b of [null, [], {}, { query: "  " }, { query: "x".repeat(201) }, { query: "x", kinds: ["pdf"] }, { query: "x", k: 0 }, { query: "x", k: 51 }]) {
      expect(typeof parseMomentsSearchRequest(b)).toBe("string");
    }
  });
});

describe("search kinds", () => {
  it("match the shared fixture (agent compact.ASSET_KINDS, SPA SEARCH_KINDS)", () => {
    const fixture = JSON.parse(readFileSync(join(__dirname, "../../../infra/lib/projects/fixtures/search-kinds.json"), "utf8"));
    expect([...KINDS]).toEqual(fixture.kinds);
  });
  it("accepts a document filter and keeps document hits as document cards", () => {
    expect(parseMomentsSearchRequest({ query: "x", kinds: ["document"] })).toMatchObject({ kinds: ["document"] });
    const m = new Map(rows([[D1, "document", "report.pdf"], [V1, "video", "Meridian.mp4"]]).map((r) => [r.assetId, r]));
    const page3 = hit(D1, 20_004, 20_005, { text: "violence rating PG-13" });
    const cards = shapeMomentCards(PID, [page3, hit(V1, 1000, 4000)], m, ["document"]);
    expect(cards.map((c) => [c.kind, c.ref.asset_id, c.start_ms])).toEqual([["document", D1, 20_004]]);
    expect(shapeMomentCards(PID, [page3], m, null).map((c) => c.kind)).toEqual(["document"]);
  });
});

describe("card shaping (mirrors render/moments.py)", () => {
  it("keeps rank order, drops removed / unknown / malformed hits and duplicate keys", () => {
    const m = new Map(rows([[V1, "video", "Meridian.mp4"], [V2, "video", "Gone.mp4", "removed"]]).map((r) => [r.assetId, r]));
    const cards = shapeMomentCards(PID, [hit(V1, 1000, 4000), hit(V2, 0, 1000), hit(V1, 1000, 4000), { asset_id: V1, start_ms: 5, end_ms: 5, final_score: 1 }, hit(I1, 0, 1)], m, null);
    expect(cards.map((c) => [c.id, c.ref.asset_id, c.display_name])).toEqual([["s1", V1, "Meridian.mp4"]]);
    expect(cards[0]).toMatchObject({ ref: { project_id: PID, frame_s3_key: null }, kind: "video", citation: null, text: "a man drives" });
  });
  it("applies the kind filter and carries a shot hit's frame key", () => {
    const m = new Map(rows([[V1, "video", "Meridian.mp4"], [I1, "image", "still.jpg"]]).map((r) => [r.assetId, r]));
    const shot = hit(V1, 0, 2000, { source_index: "shot", frame_s3_key: "projects/p/v/frame_1.jpg", text: "ignored" });
    expect(shapeMomentCards(PID, [shot, hit(I1, 0, 1000)], m, ["image"]).map((c) => c.ref.asset_id)).toEqual([I1]);
    const [c] = shapeMomentCards(PID, [shot], m, null);
    expect(c.ref.frame_s3_key).toBe("projects/p/v/frame_1.jpg");
    expect(c.text).toBe("");
  });
  it("highlight terms and matched text", () => {
    expect(highlightTerms({ text: ["a <em>night</em> <em>Night</em> <em> <b>drive</b> </em> &amp; <em>&lt;x&gt;</em>"] }))
      .toEqual(["night", "drive", "<x>"]);
    expect(matchedText({ text: "y".repeat(600) })[1]).toBe(true);
    expect(matchedText({ named_entities: ["Kawhi"] })).toEqual(['["Kawhi"]', false]);
    expect(hitsOfResult("not json")).toEqual([]);
  });
});

describe("dispatchMomentsSearch", () => {
  it("searches the project with the caller's Project_Token and returns cards", async () => {
    const { d, calls } = deps();
    const { res, out } = resCapture();
    expect(await dispatchMomentsSearch(reqWith({ query: "night drive", kinds: ["video"] }), res, ctx, d)).toBe(true);
    expect(out.status).toBe(200);
    expect(d.issueProjectToken).toHaveBeenCalledWith("user-token", PID);
    expect(calls).toEqual([["proj-token", "target___hybrid-search___search_content", { collection_id: PID, search_term: "night drive", k: 50 }]]);
    expect(out.body.items.map((c: any) => c.ref.asset_id)).toEqual([V1]);
    expect(out.body).toMatchObject({ query: "night drive", kinds: ["video"], total: 1 });
  });
  // Staging session 853bdc11: a plain query never surfaced the indexed reports (video shots outrank PDF pages).
  it("a plain query also asks the document scope and shows its document hits", async () => {
    const { d, calls } = deps({
      store: { batchGet: vi.fn(async () => rows([[V1, "video", "Meridian.mp4"], [D1, "document", "report.pdf"]])) },
      callTool: vi.fn(async (token: string, name: string, args: Record<string, unknown>) => {
        calls.push([token, name, args]);
        return JSON.stringify({ results: String(args.search_term).endsWith(" #scene")
          ? [hit(D1, 20_004, 20_005, { final_score: 0.95 }), hit(V1, 9000, 9500, { final_score: 0.9 })]
          : [hit(V1, 1000, 4000, { final_score: 1.0 })] });
      }),
    });
    const { res, out } = resCapture();
    await dispatchMomentsSearch(reqWith({ query: "suggested rating" }), res, ctx, d);
    expect(calls.map((c) => c[2].search_term)).toEqual(["suggested rating", "suggested rating #scene"]);
    expect(out.body.items.map((c: any) => [c.kind, c.ref.asset_id])).toEqual([["video", V1], ["document", D1]]);
  });

  it("a failed document companion still returns the plain results", async () => {
    const { d } = deps({
      callTool: vi.fn(async (_t: string, _n: string, args: Record<string, unknown>) => {
        if (String(args.search_term).endsWith(" #scene")) throw new Error("companion down");
        return JSON.stringify({ results: [hit(V1, 1000, 4000)] });
      }),
    });
    const { res, out } = resCapture();
    await dispatchMomentsSearch(reqWith({ query: "night drive" }), res, ctx, d);
    expect(out.status).toBe(200);
    expect(out.body.items.map((c: any) => c.ref.asset_id)).toEqual([V1]);
  });

  it("is a uniform 404 for a non-member, before any token or search", async () => {
    const { d, calls } = deps({ checkAccess: vi.fn(async () => ({ ok: false })) });
    const { res, out } = resCapture();
    await dispatchMomentsSearch(reqWith({ query: "x" }), res, ctx, d);
    expect(out.status).toBe(404);
    expect(d.issueProjectToken).not.toHaveBeenCalled();
    expect(calls).toEqual([]);
  });
  it("maps issuer and gateway failures", async () => {
    for (const [issued, status] of [[{ status: "not_found" }, 404], [{ status: "unavailable", reason: "x" }, 503]] as const) {
      const { d } = deps({ issueProjectToken: vi.fn(async () => issued) });
      const { res, out } = resCapture();
      await dispatchMomentsSearch(reqWith({ query: "x" }), res, ctx, d);
      expect(out.status).toBe(status);
    }
    const { d } = deps({ callTool: vi.fn(async () => { throw new Error("boom"); }) });
    const { res, out } = resCapture();
    await dispatchMomentsSearch(reqWith({ query: "x" }), res, ctx, d);
    expect(out.status).toBe(502);
    expect(out.body.error.code).toBe("search_failed");
  });
  it("400s a bad body and leaves other paths alone", async () => {
    const { d } = deps();
    const { res, out } = resCapture();
    await dispatchMomentsSearch(reqWith("{nope"), res, ctx, d);
    expect(out.status).toBe(400);
    const other = resCapture();
    expect(await dispatchMomentsSearch(reqWith({}), other.res, { ...ctx, parts: [PID, "moments"] }, d)).toBe(false);
  });
});

describe("mcpToolCaller (streamable HTTP)", () => {
  it("initializes, then calls the tool with the session, reading an SSE reply", async () => {
    const seen: Array<{ headers: Record<string, string>; body: any }> = [];
    const fake = vi.fn(async (_url: string, init: any) => {
      const body = JSON.parse(init.body);
      seen.push({ headers: init.headers, body });
      // The real AgentCore gateway's rule (staging 2026-10-11): a request that DECLARES revision 2026-07-28 must
      // carry `_meta` and Mcp-Method headers, else 400. The fake enforces it so echoing the version fails here too.
      if (init.headers["mcp-protocol-version"] === "2026-07-28" && !body.params?._meta) {
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: body.id,
          error: { code: -32602, message: "Missing required '_meta' for protocol version 2026-07-28" } }), { status: 400 });
      }
      if (body.method === "initialize") {
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2026-07-28" } }),
          { status: 200, headers: { "content-type": "application/json", "mcp-session-id": "sess-9" } });
      }
      if (body.method === "notifications/initialized") return new Response(null, { status: 202 });
      const sse = `event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: '{"results":[]}' }] } })}\n\n`;
      return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
    });
    const call = mcpToolCaller("https://gw/mcp", fake as any);
    expect(await call("tok", "t___search_content", { search_term: "x" })).toBe('{"results":[]}');
    const toolCall = seen.find((s) => s.body.method === "tools/call")!;
    expect(toolCall.headers.authorization).toBe("Bearer tok");
    expect(toolCall.headers["mcp-session-id"]).toBe("sess-9");
    expect(toolCall.headers["mcp-protocol-version"]).toBeUndefined();
    expect(toolCall.body.params).toEqual({ name: "t___search_content", arguments: { search_term: "x" } });
  });
  it("throws on a tool error result", async () => {
    const fake = vi.fn(async (_u: string, init: any) => {
      const body = JSON.parse(init.body);
      const result = body.method === "initialize" ? { protocolVersion: "2025-06-18" } : { isError: true, content: [{ type: "text", text: "denied" }] };
      return new Response(JSON.stringify({ jsonrpc: "2.0", id: body.id, result }), { status: 200, headers: { "content-type": "application/json" } });
    });
    await expect(mcpToolCaller("https://gw/mcp", fake as any)("tok", "t", {})).rejects.toThrow("denied");
  });
});

// ------------------------------------------------------------------ search by image (design 2026-10-18 §2)
describe("search by image", () => {
  const FRAME = "micro-workflow/shot-segmentation/run-1/frames/frame_0003.jpg";
  const imgRows = [
    { assetId: V1, kind: "video", displayName: "Meridian.mp4", status: "indexed",
      steps: { "shot-segmentation": { outputPrefix: "s3://proxy/micro-workflow/shot-segmentation/run-1/" } } },
    { assetId: I1, kind: "image", displayName: "still.jpg", status: "indexed", rendition: { key: `projects/${PID}/${I1}/rendition.jpg` } },
    { assetId: V2, kind: "video", displayName: "Gone.mp4", status: "removed" },
  ];
  const imageDeps = (perUri: Record<string, unknown[]>, extra: Record<string, unknown> = {}) => deps({
    store: { batchGet: vi.fn(async () => imgRows), query: vi.fn(async () => []) },
    buckets: { skills: "skills", mediaProxy: "proxy" },
    callTool: vi.fn(async (_t: string, _n: string, args: Record<string, unknown>) =>
      JSON.stringify({ results: perUri[String(args.search_image_s3uri)] ?? [] })),
    ...extra,
  });
  const run = async (body: unknown, d: any) => {
    const { res, out } = resCapture();
    await dispatchMomentsSearch(reqWith(body), res, ctx, d);
    return out;
  };

  it("parses 1..3 images, and never both a query and images", () => {
    expect(parseMomentsSearchRequest({ images: [{ assetId: I1 }], match: "any" })).toMatchObject({ query: "", images: [{ assetId: I1 }] });
    for (const b of [{ query: "x", images: [{ assetId: I1 }] }, { images: [] }, { images: [{ assetId: I1 }, { assetId: I1 }, { assetId: I1 }, { assetId: I1 }] },
      { images: [{ assetId: V1 }] }, { images: [{ frameKey: "a/../b", sourceAssetId: V1 }] }, { images: [{ frameKey: FRAME }] },
      { images: [{ assetId: I1 }], match: "all" }]) {
      expect(typeof parseMomentsSearchRequest(b)).toBe("string");
    }
  });

  it("searches an image asset by its rendition, and a frame under its video's own ingestion output", async () => {
    const d = imageDeps({
      [`s3://skills/projects/${PID}/${I1}/rendition.jpg`]: [hit(V1, 1000, 4000, { final_score: 0.6 }), hit(I1, 0, 1000, { final_score: 0.5 })],
      [`s3://proxy/${FRAME}`]: [hit(V1, 1000, 4000, { final_score: 0.8 })],
    });
    const out = await run({ images: [{ assetId: I1 }, { frameKey: FRAME, sourceAssetId: V1 }], match: "any" }, d.d);
    expect(out.status).toBe(200);
    expect(d.d.callTool).toHaveBeenCalledTimes(2);
    // Merged "match any": one card per (asset, start), the best score, matched_by both images.
    const v1 = out.body.items.find((c: any) => c.ref.asset_id === V1);
    expect(v1.final_score).toBe(0.8);
    expect([...v1.matched_by].sort()).toEqual([0, 1]);
    expect(out.body.items.find((c: any) => c.ref.asset_id === I1).matched_by).toEqual([0]);
    expect(out.body.items[0].ref.asset_id).toBe(V1); // ranked by best score
  });

  it("refuses another project's (or a removed) asset, and an arbitrary frame key", async () => {
    const other = "image:99999999-9999-4999-8999-999999999999";
    expect((await run({ images: [{ assetId: other }] }, imageDeps({}).d)).status).toBe(404);
    expect((await run({ images: [{ frameKey: FRAME, sourceAssetId: V2 }] }, imageDeps({}).d)).status).toBe(404);
    const stray = await run({ images: [{ frameKey: "micro-workflow/shot-segmentation/someone-else/frames/f.jpg", sourceAssetId: V1 }] }, imageDeps({}).d);
    expect(stray.status).toBe(404);
    expect(stray.body.error.code).toBe("query_image_not_found");
  });

  it("accepts a frame under one of this project's JOB# output prefixes", async () => {
    const jobFrame = "micro-workflow/media-processing/job-9/frames/frame_0001.jpg";
    const d = imageDeps({ [`s3://proxy/${jobFrame}`]: [hit(V1, 0, 2000)] }, {
      store: { batchGet: vi.fn(async () => imgRows),
        query: vi.fn(async () => [{ outputPrefix: "s3://proxy/micro-workflow/media-processing/job-9/" }]) },
    });
    expect((await run({ images: [{ frameKey: jobFrame, sourceAssetId: V1 }] }, d.d)).status).toBe(200);
  });

  it("an image with no rendition gets one on demand (Asset_Service search_images) when the tab sends its chat", async () => {
    const noRendition = { store: { batchGet: vi.fn(async () => [{ assetId: I1, kind: "image", displayName: "old.jpg", status: "stored" }]), query: vi.fn() } };
    const made = `s3://skills/projects/${PID}/${I1}/rendition.jpg`;
    const searchImages = vi.fn(async () => ({ [I1]: { uri: made } }));
    const d = imageDeps({ [made]: [hit(V1, 0, 2000)] }, { ...noRendition, searchImages });
    const out = await run({ images: [{ assetId: I1 }], sessionId: "sess-1" }, d.d);
    expect(out.status).toBe(200);
    expect(searchImages).toHaveBeenCalledWith(PID, expect.any(String), [I1], "sess-1");
    // It can't become one (another chat's upload, unreadable): the same 404 as a missing image, not "preparing".
    const refused = imageDeps({}, { ...noRendition, searchImages: vi.fn(async () => ({ [I1]: { reason: "not_found" } })) });
    expect((await run({ images: [{ assetId: I1 }], sessionId: "sess-1" }, refused.d)).status).toBe(404);
  });

  it("an image with no search rendition yet is a 409, not a silent empty result", async () => {
    const d = imageDeps({}, { store: { batchGet: vi.fn(async () => [{ assetId: I1, kind: "image", displayName: "s.jpg", status: "queued" }]), query: vi.fn() } });
    const out = await run({ images: [{ assetId: I1 }] }, d.d);
    expect(out.status).toBe(409);
    expect(out.body.error.code).toBe("image_not_ready");
  });
});
