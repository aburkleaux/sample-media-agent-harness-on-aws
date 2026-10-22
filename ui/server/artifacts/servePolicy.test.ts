// The /api/artifact serve policy: the stored content type is writer-controlled, so every response is
// sniff-proof and (PDF aside) sandboxed, executable/malformed types are downgraded to an octet-stream
// attachment, and a type nothing previews inline is attached.
import { describe, it, expect } from "vitest";
import {
  artifactServePolicy,
  isolationHeaders,
  mediaTypeEssence,
  safeDispositionFilename,
} from "./servePolicy";
import { DOCUMENT_SANDBOX_CSP, isActiveDocumentType } from "./viewTicket";

const CSP = "content-security-policy";
const NOSNIFF = "x-content-type-options";

describe("artifactServePolicy — header matrix by declared type", () => {
  const cases: [string | undefined, string, "inline" | "attachment", boolean][] = [
    // [declared type, served type, disposition, sandbox CSP?]
    ["text/html; charset=utf-8", "text/html; charset=utf-8", "inline", true],
    ["image/svg+xml", "image/svg+xml", "inline", true],
    ["application/xhtml+xml", "application/xhtml+xml", "inline", true],
    // The gap: these render scripts too, and used to go out unsandboxed.
    ["text/xml", "text/xml", "inline", true],
    ["application/xml", "application/xml", "inline", true],
    ["application/rss+xml", "application/rss+xml", "inline", true],
    ["application/mathml+xml", "application/mathml+xml", "inline", true],
    // Media and data keep inline preview (players, <img>, fetch-rendered tables), sandboxed anyway.
    ["video/mp4", "video/mp4", "inline", true],
    ["audio/mpeg", "audio/mpeg", "inline", true],
    ["image/png", "image/png", "inline", true],
    ["text/vtt", "text/vtt", "inline", true],
    ["application/json", "application/json", "inline", true],
    ["text/csv", "text/csv", "inline", true],
    ["text/plain", "text/plain", "inline", true],
    // PDF: inline, NOT sandboxed (Chromium refuses to render a sandboxed PDF), still nosniff.
    ["application/pdf", "application/pdf", "inline", false],
    // Executable: never served as itself.
    ["application/javascript", "application/octet-stream", "attachment", true],
    ["text/javascript; charset=utf-8", "application/octet-stream", "attachment", true],
    ["application/ecmascript", "application/octet-stream", "attachment", true],
    ["application/wasm", "application/octet-stream", "attachment", true],
    ["application/x-shockwave-flash", "application/octet-stream", "attachment", true],
    ["application/x-sh", "application/octet-stream", "attachment", true],
    ["text/x-component", "application/octet-stream", "attachment", true],
    // Malformed: an injected second type or header must not survive.
    ["text/html, application/pdf", "application/octet-stream", "attachment", true],
    ["text/html\r\nx-evil: 1", "application/octet-stream", "attachment", true],
    ["", "application/octet-stream", "attachment", true],
    [undefined, "application/octet-stream", "attachment", true],
    ["nonsense", "application/octet-stream", "attachment", true],
    // Unknown but well-formed: keep the type (fetch callers read it) but never render it inline.
    ["application/zip", "application/zip", "attachment", true],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "attachment", true],
    ["application/octet-stream", "application/octet-stream", "attachment", true],
  ];
  for (const [declared, served, disposition, sandboxed] of cases) {
    it(`${JSON.stringify(declared)} → ${served}, ${disposition}${sandboxed ? ", sandboxed" : ""}`, () => {
      const p = artifactServePolicy(declared, false);
      expect(p.contentType).toBe(served);
      expect(p.disposition).toBe(disposition);
      expect(p.headers[NOSNIFF]).toBe("nosniff");
      if (sandboxed) expect(p.headers[CSP]).toBe(DOCUMENT_SANDBOX_CSP);
      else expect(p.headers[CSP]).toBeUndefined();
    });
  }

  it("?download=1 always attaches, whatever the type", () => {
    for (const t of ["text/html", "application/pdf", "video/mp4", "image/png"]) {
      expect(artifactServePolicy(t, true).disposition).toBe("attachment");
    }
  });

  it("the sandbox never grants same-origin", () => {
    expect(DOCUMENT_SANDBOX_CSP.startsWith("sandbox")).toBe(true);
    expect(DOCUMENT_SANDBOX_CSP).not.toContain("allow-same-origin");
  });

  it("isolationHeaders(null) — the default every /api/artifact response starts with — is sandboxed + nosniff", () => {
    expect(isolationHeaders(null)).toEqual({ [NOSNIFF]: "nosniff", [CSP]: DOCUMENT_SANDBOX_CSP });
  });

  it("mediaTypeEssence lower-cases, strips params, rejects malformed", () => {
    expect(mediaTypeEssence("Text/HTML; charset=UTF-8")).toBe("text/html");
    expect(mediaTypeEssence("text/html, text/plain")).toBeNull();
    expect(mediaTypeEssence(undefined)).toBeNull();
  });

  it("safeDispositionFilename cannot break out of the quoted header value", () => {
    expect(safeDispositionFilename('artifacts/s/output/a"; x=1.html')).toBe("a_; x=1.html");
    expect(safeDispositionFilename("artifacts/s/output/résumé\r\n.pdf")).toBe("r_sum___.pdf");
  });
});

describe("isActiveDocumentType — every script-capable document type", () => {
  it("covers HTML and every XML type, not media or PDF", () => {
    for (const t of ["text/html; charset=utf-8", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml", "application/rss+xml"]) {
      expect(isActiveDocumentType(t), t).toBe(true);
    }
    for (const t of ["video/mp4", "application/pdf", "text/plain", undefined]) {
      expect(isActiveDocumentType(t), String(t)).toBe(false);
    }
  });
});
