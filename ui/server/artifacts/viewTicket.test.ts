import { describe, it, expect } from "vitest";
import {
  DOCUMENT_SANDBOX_CSP,
  documentViewUrl,
  isActiveDocumentType,
  isViewedDocumentKey,
  mintViewTicket,
  verifyViewTicket,
} from "./viewTicket";

const KEY = "artifacts/s1/output/story.html";
const SECRET = "k".repeat(64);
const NOW = 1_800_000_000_000;

describe("view tickets", () => {
  it("round-trips to the actor it was minted for", () => {
    const { ticket, expiresAt } = mintViewTicket(KEY, "actor-1", SECRET, NOW);
    expect(verifyViewTicket(ticket, KEY, SECRET, NOW)).toBe("actor-1");
    expect(expiresAt).toBe(NOW / 1000 + 3600);
  });

  it("is bound to its key, its secret, and its lifetime", () => {
    const { ticket } = mintViewTicket(KEY, "actor-1", SECRET, NOW);
    expect(verifyViewTicket(ticket, "artifacts/s1/output/other.html", SECRET, NOW)).toBeNull();
    expect(verifyViewTicket(ticket, KEY, "another-secret", NOW)).toBeNull();
    expect(verifyViewTicket(ticket, KEY, SECRET, NOW + 3601_000)).toBeNull();
  });

  it("cannot be re-pointed at another actor or stretched", () => {
    const { ticket } = mintViewTicket(KEY, "actor-1", SECRET, NOW);
    const [v, exp, , sig] = ticket.split(".");
    const otherActor = [v, exp, Buffer.from("actor-2").toString("base64url"), sig].join(".");
    const longer = [v, String(Number(exp) + 86400), ticket.split(".")[2], sig].join(".");
    expect(verifyViewTicket(otherActor, KEY, SECRET, NOW)).toBeNull();
    expect(verifyViewTicket(longer, KEY, SECRET, NOW)).toBeNull();
  });

  it("never throws on garbage", () => {
    for (const bad of [undefined, null, 42, "", "v1", "v2.1.YQ.AA", "v1.x.YQ.AA", "v1.9999999999..AA"]) {
      expect(verifyViewTicket(bad as unknown, KEY, SECRET, NOW)).toBeNull();
    }
    expect(verifyViewTicket("v1.9999999999.YQ.AA", KEY, "", NOW)).toBeNull();
    expect(() => mintViewTicket(KEY, "", SECRET)).toThrow();
  });

  it("builds the same-origin document URL", () => {
    expect(documentViewUrl(KEY, "t.1")).toBe(`/api/artifact?key=${encodeURIComponent(KEY)}&view=t.1`);
    expect(documentViewUrl(KEY)).toBe(`/api/artifact?key=${encodeURIComponent(KEY)}`);
  });
});

describe("document classification and isolation", () => {
  it("routes HTML pages, not media, through the proxy", () => {
    expect(isViewedDocumentKey(KEY)).toBe(true);
    expect(isViewedDocumentKey("artifacts/s/out/a.HTM")).toBe(true);
    expect(isViewedDocumentKey("artifacts/s/out/a.mp4")).toBe(false);
    expect(isViewedDocumentKey("artifacts/s/out/a.pdf")).toBe(false);
  });

  it("sandboxes every active document type", () => {
    expect(isActiveDocumentType("text/html; charset=utf-8")).toBe(true);
    expect(isActiveDocumentType("image/svg+xml")).toBe(true);
    expect(isActiveDocumentType("application/xhtml+xml")).toBe(true);
    expect(isActiveDocumentType("video/mp4")).toBe(false);
    expect(isActiveDocumentType("application/pdf")).toBe(false);
    expect(isActiveDocumentType(undefined)).toBe(false);
  });

  it("isolates the origin but keeps interactive pages working", () => {
    const tokens = DOCUMENT_SANDBOX_CSP.split(/\s+/);
    expect(tokens[0]).toBe("sandbox");
    expect(tokens).not.toContain("allow-same-origin");
    expect(tokens).toEqual(expect.arrayContaining(["allow-scripts", "allow-forms", "allow-popups"]));
  });
});
