// The proxy's upload naming/kind twins agree with the Python Asset_Service over the shared fixture
// (`agent/src/ingestion/fixtures/upload_cases.json`, asserted on the Python side by
// `agent/tests/test_ingestion_remove.py::test_upload_cases_fixture_matches_python`).
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  DEFAULT_UPLOAD_LIMITS,
  ingestionCapabilities,
  kindFor,
  sanitizeFileName,
  uploadLimitsFromEnv,
  uploadObjectKey,
} from "./ingestionNames";
import { isAssetId, kindFromAssetId } from "./schema";

const FIXTURE = path.resolve(__dirname, "../../../agent/src/ingestion/fixtures/upload_cases.json");
const cases = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  sanitize: { name: string; sanitized: string }[];
  kind: { contentType: string; name: string; kind: string }[];
};

describe("sanitizeFileName (twin of names.py sanitize)", () => {
  it.each(cases.sanitize)("sanitizes %#", ({ name, sanitized }) => {
    expect(sanitizeFileName(name)).toBe(sanitized);
  });

  it("is idempotent, bounded, and never empty", () => {
    for (const { name } of cases.sanitize) {
      const once = sanitizeFileName(name);
      expect(sanitizeFileName(once)).toBe(once);
      expect(once.length).toBeGreaterThan(0);
      expect(once.length).toBeLessThanOrEqual(200);
      expect(once).toMatch(/^[A-Za-z0-9._-]+$/);
    }
  });

  it("derives the exact key register_upload HEADs", () => {
    const pid = "p0123456789abcdef0123456789abcdef";
    const aid = "document:6f1c0a2b-1111-4222-8333-444455556666";
    expect(uploadObjectKey(pid, aid, "My notes (v2).txt")).toBe(`projects/${pid}/${aid}/My_notes__v2_.txt`);
  });
});

describe("kindFor (twin of limits.py kind_for)", () => {
  it.each(cases.kind)("classifies $contentType / $name", ({ contentType, name, kind }) => {
    expect(kindFor(contentType, name)).toBe(kind);
  });
});

describe("document asset ids", () => {
  it("accepts document:<uuid4> and reads its kind", () => {
    const id = "document:6f1c0a2b-1111-4222-8333-444455556666";
    expect(isAssetId(id)).toBe(true);
    expect(kindFromAssetId(id)).toBe("document");
    expect(isAssetId("doc:6f1c0a2b-1111-4222-8333-444455556666")).toBe(false);
  });
});

describe("capabilities", () => {
  it("are the contract values by default", () => {
    expect(ingestionCapabilities({})).toEqual({
      maxFilesPerBatch: 20,
      limits: { video: 5368709120, audio: 5368709120, image: 52428800, document: 524288000 },
      multipartThresholdBytes: 104857600,
      partSizeBytes: 67108864,
    });
  });

  it("take positive-integer overrides from INGESTION_CONFIG.uploadLimits only", () => {
    const env = { INGESTION_CONFIG: JSON.stringify({ uploadLimits: { image: 1000, video: "x", audio: -5, document: 1.5 } }) };
    expect(uploadLimitsFromEnv(env)).toEqual({ ...DEFAULT_UPLOAD_LIMITS, image: 1000 });
    expect(uploadLimitsFromEnv({ INGESTION_CONFIG: "{not json" })).toEqual(DEFAULT_UPLOAD_LIMITS);
  });
});
