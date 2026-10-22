// The document-recall policy, case for case with the agent twin (both read the same fixture).
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { companionTerm, DOCUMENT_SCOPE, mergeDocumentHits, SCOPED_QUERY_PATTERN } from "./documentRecall";

const fx = JSON.parse(readFileSync(join(__dirname, "../../../infra/lib/projects/fixtures/document-recall-cases.json"), "utf8"));

describe("document recall (shared fixture)", () => {
  it("uses the fixture's pattern and document scope", () => {
    expect(SCOPED_QUERY_PATTERN).toBe(fx.scopedQueryPattern);
    expect(DOCUMENT_SCOPE).toBe(fx.documentScope);
  });
  it("chooses the companion query exactly as the agent does", () => {
    for (const c of fx.companionCases) {
      const query = typeof c.args.search_term === "string" && !c.args.search_image_s3uri ? c.args.search_term : "";
      expect(companionTerm(query, c.kinds), JSON.stringify(c)).toBe(c.expect);
    }
  });
  it("merges exactly as the agent does", () => {
    for (const c of fx.mergeCases) {
      const got = mergeDocumentHits(c.primary, c.companion).map((h: any) => [h.asset_id, h.start_ms]);
      expect(got, c.note).toEqual(c.expect);
    }
  });
});
