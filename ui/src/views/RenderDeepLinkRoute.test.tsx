/**
 * @vitest-environment jsdom
 */
// `/render-view` must stay login-free where the artifacts CDN is public, and require sign-in (and
// come back to the same URL) where it is private. Also pins `safeReturnTo`, the open-redirect guard
// on the sign-in round-trip.
import "@testing-library/jest-dom";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

vi.mock("./RenderDeepLink", () => ({
  default: ({ authenticated }: { authenticated?: boolean }) => (
    <div data-testid="deep-link">{authenticated ? "authenticated" : "public"}</div>
  ),
}));
vi.mock("../auth/AuthGate", () => ({
  default: ({ children }: { children: (c: { onShowLanding: () => void }) => unknown }) => (
    <div data-testid="auth-gate">{children({ onShowLanding: () => {} }) as never}</div>
  ),
}));

import RenderDeepLinkRoute from "./RenderDeepLinkRoute";
import { safeReturnTo } from "../auth/authManager";
import { __resetRuntimeConfigForTests } from "../config/runtimeConfig";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // /config is loaded once per page and cached (config/runtimeConfig.ts); each case is a fresh page.
  __resetRuntimeConfigForTests();
});

function stubConfig(body: unknown, ok = true) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok, json: async () => body }));
}

describe("RenderDeepLinkRoute", () => {
  it("renders the public page with no AuthGate when the CDN is public", async () => {
    stubConfig({ artifactsRequireAuth: false });
    render(<RenderDeepLinkRoute />);
    await waitFor(() => expect(screen.getByTestId("deep-link")).toHaveTextContent("public"));
    expect(screen.queryByTestId("auth-gate")).toBeNull();
  });

  it("renders the signed-in page inside AuthGate when the CDN is private", async () => {
    stubConfig({ artifactsRequireAuth: true });
    render(<RenderDeepLinkRoute />);
    await waitFor(() => expect(screen.getByTestId("auth-gate")).toBeInTheDocument());
    expect(screen.getByTestId("deep-link")).toHaveTextContent("authenticated");
  });

  it("falls back to the public page when /config cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<RenderDeepLinkRoute />);
    await waitFor(() => expect(screen.getByTestId("deep-link")).toHaveTextContent("public"));
  });
});

describe("safeReturnTo", () => {
  it("returns a same-origin relative path carried in the OIDC state", () => {
    expect(safeReturnTo({ returnTo: "/render-view?sessionId=s&manifestKey=k" })).toBe(
      "/render-view?sessionId=s&manifestKey=k",
    );
    expect(safeReturnTo({ returnTo: "/" })).toBe("/");
  });

  it("refuses anything that could leave the origin or loop into the callback", () => {
    for (const bad of ["https://evil.example/", "//evil.example/x", "/\\evil", "evil", "/auth/callback?code=x", 42]) {
      expect(safeReturnTo({ returnTo: bad })).toBe("/");
    }
    expect(safeReturnTo(undefined)).toBe("/");
    expect(safeReturnTo(null)).toBe("/");
    expect(safeReturnTo({})).toBe("/");
  });
});
