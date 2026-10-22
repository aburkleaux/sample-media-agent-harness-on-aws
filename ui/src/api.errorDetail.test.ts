// @vitest-environment jsdom
/**
 * `skillsAction` error shape.
 *
 * Runtime actions answer with {error: <code>, message: <human text>}. The helper
 * used to throw only the code, so a registration that failed for a specific,
 * explainable reason surfaced in the UI as the bare string
 * "registration_failed" — the explanation was dropped before it reached the
 * user, and it never reached the console either because callers render it into a
 * banner rather than logging.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const AGENT_URL = "/api";

vi.mock("./auth/authFetch", () => ({
  authFetch: vi.fn(),
}));

import { authFetch } from "./auth/authFetch";
import { registerMcpServer } from "./api";

function sse(body: unknown): Response {
  return new Response(`data: ${JSON.stringify(body)}\n\n`, { status: 200 });
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("skillsAction error surfacing", () => {
  it("includes the server's message, not just the code", async () => {
    vi.mocked(authFetch).mockResolvedValue(
      sse({
        error: "registration_failed",
        message: "The auth header value looks incomplete: got only 'Bearer'.",
      }),
    );
    await expect(
      registerMcpServer({ name: "x", url: "https://m.example.com/mcp" }),
    ).rejects.toThrow(/looks incomplete/);
  });

  it("keeps the code available for diagnosis", async () => {
    vi.mocked(authFetch).mockResolvedValue(
      sse({ error: "registration_failed", message: "boom" }),
    );
    await registerMcpServer({ name: "x", url: "https://m/mcp" }).catch(
      (e: Error & { code?: string; detail?: string }) => {
        expect(e.code).toBe("registration_failed");
        expect(e.detail).toBe("boom");
      },
    );
  });

  it("falls back to the code when there is no message", async () => {
    vi.mocked(authFetch).mockResolvedValue(sse({ error: "admin_required" }));
    await expect(
      registerMcpServer({ name: "x", url: "https://m/mcp" }),
    ).rejects.toThrow("admin_required");
  });
});
