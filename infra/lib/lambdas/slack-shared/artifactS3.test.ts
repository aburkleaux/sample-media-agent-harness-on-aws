// Our artifacts-CDN URLs are identifiers: the Slack Lambdas HEAD/GET them by key in S3 (the CDN may
// be private), while any other URL keeps using HTTP. Mock S3 + mock fetch; no AWS, no network.
// Run (from infra/): npx ts-node lib/lambdas/slack-shared/artifactS3.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { artifactKeyFromUrl, cdnPrivateFromEnv, extractArtifactUrls, rewriteArtifactUrlsForPrivateCdn } from "./notify";
import { headArtifact, uploadFromUrl, fetchArtifactBytes } from "./slackClient";

const D = "d999.cloudfront.net";
const BUCKET = "skills-bucket";

function fakeS3() {
  const calls: Array<{ name: string; input: any }> = [];
  return {
    calls,
    async send(cmd: any) {
      const name = cmd.constructor.name;
      calls.push({ name, input: cmd.input });
      if (name === "HeadObjectCommand") return { ContentLength: 1234, ContentType: "video/mp4" };
      if (name === "GetObjectCommand") {
        return { Body: { transformToByteArray: async () => new Uint8Array([1, 2, 3]) } };
      }
      throw new Error("unexpected " + name);
    },
  };
}

/** Swap global fetch for a recorder answering Slack's upload API and plain HTTP artifact reads. */
async function withFetch(fn: (urls: Array<{ url: string; method: string }>) => Promise<void>): Promise<void> {
  const orig = globalThis.fetch;
  const urls: Array<{ url: string; method: string }> = [];
  (globalThis as any).fetch = async (url: string, init?: any) => {
    urls.push({ url: String(url), method: init?.method ?? "GET" });
    const u = String(url);
    if (u.includes("files.getUploadURLExternal")) {
      return { ok: true, json: async () => ({ ok: true, upload_url: "https://files.slack.com/up", file_id: "F1" }) };
    }
    if (u.includes("files.completeUploadExternal")) return { ok: true, json: async () => ({ ok: true }) };
    if (u === "https://files.slack.com/up") return { ok: true };
    return {
      ok: true,
      headers: new Map([["content-length", "77"], ["content-type", "image/png"]]),
      arrayBuffer: async () => new Uint8Array([9]).buffer,
    };
  };
  try {
    await fn(urls);
  } finally {
    (globalThis as any).fetch = orig;
  }
}

test("artifactKeyFromUrl: our CDN artifact URLs → key; everything else → null", () => {
  assert.equal(artifactKeyFromUrl(`https://${D}/artifacts/s/output/a.mp4`, D), "artifacts/s/output/a.mp4");
  assert.equal(artifactKeyFromUrl(`https://${D}/artifacts/s/output/a%20b.mp4`, D), "artifacts/s/output/a b.mp4");
  assert.equal(artifactKeyFromUrl(`https://${D}/artifacts/artifacts/s/a.mp4`, D), "artifacts/s/a.mp4");
  for (const u of [
    `https://other.example.com/artifacts/s/a.mp4`,
    `https://${D}/skills/a.md`,
    `https://${D}/artifacts/`,
    `https://${D}/artifacts/s/..%2Fx`,
    `http://${D}/artifacts/s/a.mp4`,
    "not a url",
  ]) {
    assert.equal(artifactKeyFromUrl(u, D), null, u);
  }
  assert.equal(artifactKeyFromUrl(`https://${D}/artifacts/s/a.mp4`, ""), null);
});

test("headArtifact reads our artifact by key via S3 HeadObject — no HTTP", async () => {
  const s3 = fakeS3();
  await withFetch(async (urls) => {
    const head = await headArtifact(`https://${D}/artifacts/s/output/clip.mp4`, { s3, bucket: BUCKET, artifactsDomain: D });
    assert.deepEqual(head, { sizeBytes: 1234, contentType: "video/mp4", filename: "clip.mp4" });
    assert.equal(urls.length, 0);
  });
  assert.deepEqual(s3.calls, [{ name: "HeadObjectCommand", input: { Bucket: BUCKET, Key: "artifacts/s/output/clip.mp4" } }]);
});

test("headArtifact keeps HTTP HEAD for non-artifact URLs and when no bucket is configured", async () => {
  const s3 = fakeS3();
  await withFetch(async (urls) => {
    const a = await headArtifact("https://example.com/x/pic.png", { s3, bucket: BUCKET, artifactsDomain: D });
    assert.deepEqual(a, { sizeBytes: 77, contentType: "image/png", filename: "pic.png" });
    await headArtifact(`https://${D}/artifacts/s/a.png`, { s3, bucket: "", artifactsDomain: D });
    await headArtifact(`https://${D}/artifacts/s/b.png`);
    assert.deepEqual(urls.map((u) => u.method), ["HEAD", "HEAD", "HEAD"]);
  });
  assert.equal(s3.calls.length, 0);
});

test("uploadFromUrl reads our artifact bytes via S3 GetObject, never the CDN", async () => {
  const s3 = fakeS3();
  await withFetch(async (urls) => {
    const ok = await uploadFromUrl(
      "fake-bot-credential",
      { channel: "C1", threadTs: "1.2", url: `https://${D}/artifacts/s/output/clip.mp4`, filename: "clip.mp4" },
      { s3, bucket: BUCKET, artifactsDomain: D },
    );
    assert.equal(ok, true);
    assert.ok(urls.every((u) => !u.url.includes(D)), "no request to the artifacts CDN");
    assert.equal(urls.length, 3); // getUploadURLExternal, PUT, completeUploadExternal
  });
  assert.deepEqual(s3.calls, [{ name: "GetObjectCommand", input: { Bucket: BUCKET, Key: "artifacts/s/output/clip.mp4" } }]);
});

test("fetchArtifactBytes keeps HTTP GET for non-artifact URLs", async () => {
  const s3 = fakeS3();
  await withFetch(async (urls) => {
    const bytes = await fetchArtifactBytes("https://example.com/x.bin", { s3, bucket: BUCKET, artifactsDomain: D });
    assert.deepEqual([...bytes], [9]);
    assert.deepEqual(urls, [{ url: "https://example.com/x.bin", method: "GET" }]);
  });
  assert.equal(s3.calls.length, 0);
});

// ---- artifactsCdnPrivate: posted reply text links the UI opener, not the 403ing CDN ----
const UI = "d111.cloudfront.net";

test("rewrites our CDN artifact URLs to https://<UI_DOMAIN>/open?key=<urlencoded key>", () => {
  const text =
    `Clip: https://${D}/artifacts/sess-1/output/final%20cut.mp4, ` +
    `poster [p](https://${D}/artifacts/sess-1/output/p%C3%B6ster.png).`;
  assert.equal(
    rewriteArtifactUrlsForPrivateCdn(text, D, UI),
    `Clip: https://${UI}/open?key=${encodeURIComponent("artifacts/sess-1/output/final cut.mp4")}, ` +
      `poster [p](https://${UI}/open?key=${encodeURIComponent("artifacts/sess-1/output/pöster.png")}).`,
  );
});

test("leaves other hosts, non-artifact paths, and Slack link labels alone", () => {
  const text =
    `See https://example.com/artifacts/x.png and https://${D}/projects/p/a/x.png ` +
    `and <https://${D}/artifacts/s/output/a.mp4|the video>.`;
  const out = rewriteArtifactUrlsForPrivateCdn(text, D, UI);
  assert.ok(out.includes("https://example.com/artifacts/x.png"), "another host is untouched");
  assert.ok(out.includes(`https://${D}/projects/p/a/x.png`), "a non-artifacts path is untouched");
  assert.ok(out.includes(`<https://${UI}/open?key=${encodeURIComponent("artifacts/s/output/a.mp4")}|the video>`));
});

test("collapses a doubled artifacts/ prefix and rejects traversal", () => {
  assert.equal(
    rewriteArtifactUrlsForPrivateCdn(`https://${D}/artifacts/artifacts/s/a.png`, D, UI),
    `https://${UI}/open?key=${encodeURIComponent("artifacts/s/a.png")}`,
  );
  const evil = `https://${D}/artifacts/../secret`;
  assert.equal(rewriteArtifactUrlsForPrivateCdn(evil, D, UI), evil);
});

test("is a no-op without a UI domain or artifacts domain; accepts a UI domain with a scheme", () => {
  const t = `https://${D}/artifacts/s/a.png`;
  assert.equal(rewriteArtifactUrlsForPrivateCdn(t, D, ""), t);
  assert.equal(rewriteArtifactUrlsForPrivateCdn(t, "", UI), t);
  assert.equal(rewriteArtifactUrlsForPrivateCdn(t, D, `https://${UI}/`), `https://${UI}/open?key=${encodeURIComponent("artifacts/s/a.png")}`);
});

test("native uploads still find the ORIGINAL artifact URLs (only the posted copy is rewritten)", () => {
  const reply = `Done: https://${D}/artifacts/s/output/a.mp4`;
  const posted = rewriteArtifactUrlsForPrivateCdn(reply, D, UI);
  assert.deepEqual(extractArtifactUrls(posted, D), []);
  assert.deepEqual(extractArtifactUrls(reply, D), [`https://${D}/artifacts/s/output/a.mp4`]);
});

test("ARTIFACTS_CDN_PRIVATE parsing", () => {
  for (const v of ["true", "TRUE", "1", "yes", " true "]) assert.equal(cdnPrivateFromEnv(v), true, v);
  for (const v of [undefined, "", "false", "0", "no"]) assert.equal(cdnPrivateFromEnv(v), false, String(v));
});

test("artifactKeyFromUrl: a projects-session OUTPUT url → key; a project ASSET url → null (asset-lineage Phase 3 (a))", () => {
  const pid = "p" + "a".repeat(32);
  const key = `projects/${pid}/sessions/4f9d0d02-1111-4222-8333-444455556666/output/clip.mp4`;
  assert.equal(artifactKeyFromUrl(`https://${D}/${key}`, D), key);
  assert.equal(artifactKeyFromUrl(`https://${D}/projects/${pid}/video:x/a.mp4`, D), null);
  assert.equal(artifactKeyFromUrl(`https://${D}/projects/${pid}/sessions/4f9d0d02-1111-4222-8333-444455556666/render/m.json`, D), null);
  assert.equal(artifactKeyFromUrl(`https://${D}/projects/${pid}/sessions/external/output/a.mp4`, D), null);
});

test("the project-session key rule matches the shared layout fixture (asset-lineage Phase 3 (a))", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const fs = require("node:fs") as typeof import("node:fs");
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const path = require("node:path") as typeof import("node:path");
  const fixture = JSON.parse(fs.readFileSync(
    path.resolve(__dirname, "../../../../agent/tests/fixtures/session_layout_cases.json"), "utf8"));
  let checked = 0;
  for (const c of fixture.keys as Array<{ key: string; isProjectSessionOutput: boolean }>) {
    if (!c.key.startsWith("projects/")) continue;
    const got = artifactKeyFromUrl(`https://${D}/${c.key}`, D);
    assert.equal(got === c.key, c.isProjectSessionOutput, `${c.key}: expected ${c.isProjectSessionOutput}, got ${got}`);
    checked++;
  }
  assert.ok(checked >= 8, `fixture covers the projects/ cases (${checked})`);
});
