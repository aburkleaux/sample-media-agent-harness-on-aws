/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import RenderDeepLink, {
  validateDeepLink,
  manifestFetchUrl,
  readDeepLinkParams,
} from "./RenderDeepLink";
import shots from "../components/renderer/__fixtures__/shot-segmentation.manifest.json";

const SESSION = "slack-" + "a".repeat(64);
const KEY = `artifacts/${SESSION}/render/shot.json`;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.history.replaceState({}, "", "/");
});

describe("validateDeepLink — confinement", () => {
  it("accepts a well-formed session-scoped render key", () => {
    expect(validateDeepLink(SESSION, KEY)).toBeNull();
  });

  it("accepts a valid key with no sessionId provided", () => {
    expect(validateDeepLink("", KEY)).toBeNull();
  });

  it("rejects a missing key", () => {
    expect(validateDeepLink(SESSION, "")).toMatch(/missing/i);
  });

  it("rejects a non-artifact key (e.g. secrets)", () => {
    expect(validateDeepLink("", "secrets/creds.json")).toMatch(/unauthorized/i);
  });

  it("rejects a non-render artifact key (input, not render)", () => {
    expect(validateDeepLink("", `artifacts/${SESSION}/input/file.mp4`)).toMatch(/unauthorized/i);
  });

  it("rejects a path-traversal attempt", () => {
    expect(validateDeepLink("", "artifacts/../secrets/render/x.json")).toMatch(/unauthorized/i);
  });

  it("rejects a non-.json render key", () => {
    expect(validateDeepLink("", `artifacts/${SESSION}/render/x.mp4`)).toMatch(/unauthorized/i);
  });

  it("rejects a key whose session segment != declared sessionId", () => {
    expect(validateDeepLink(SESSION, `artifacts/slack-other/render/x.json`)).toMatch(
      /does not belong/i,
    );
  });
});

describe("manifestFetchUrl / readDeepLinkParams", () => {
  it("builds the public read-through URL with an encoded key", () => {
    expect(manifestFetchUrl(KEY)).toBe(`/api/artifact?key=${encodeURIComponent(KEY)}`);
  });

  it("reads sessionId and manifestKey from the query string", () => {
    const p = readDeepLinkParams(`?sessionId=${SESSION}&manifestKey=${encodeURIComponent(KEY)}`);
    expect(p.sessionId).toBe(SESSION);
    expect(p.manifestKey).toBe(KEY);
  });
});

describe("RenderDeepLink component", () => {
  it("fetches the public manifest and renders it via the existing router", async () => {
    window.history.replaceState(
      {},
      "",
      `/render-view?sessionId=${SESSION}&manifestKey=${encodeURIComponent(KEY)}`,
    );
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify(shots),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RenderDeepLink />);

    // The shot-segmentation manifest routes to a video template.
    const video = await waitFor(() => {
      const v = document.querySelector("video");
      if (!v) throw new Error("video not rendered yet");
      return v;
    });
    expect(video).toBeTruthy();
    // Fetched the public, key-confined path — no auth, no presign. (Now with an
    // abort signal for the per-attempt timeout.)
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/artifact?key=${encodeURIComponent(KEY)}`,
      expect.objectContaining({ signal: expect.anything() }),
    );
  });

  it("refuses an unauthorized key WITHOUT any network call", async () => {
    window.history.replaceState(
      {},
      "",
      `/render-view?manifestKey=${encodeURIComponent("secrets/creds.json")}`,
    );
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<RenderDeepLink />);

    expect(await screen.findByText(/unauthorized manifest key/i)).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows a bounded error when the manifest read fails", async () => {
    window.history.replaceState(
      {},
      "",
      `/render-view?sessionId=${SESSION}&manifestKey=${encodeURIComponent(KEY)}`,
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));

    render(<RenderDeepLink />);

    expect(await screen.findByText(/could not display this render/i)).toBeTruthy();
  });

  it("retries a transient 410 (with backoff) before surfacing the error", async () => {
    vi.useFakeTimers();
    window.history.replaceState(
      {},
      "",
      `/render-view?sessionId=${SESSION}&manifestKey=${encodeURIComponent(KEY)}`,
    );
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 410, text: async () => "" });
    vi.stubGlobal("fetch", fetchMock);

    render(<RenderDeepLink />);

    // A just-produced manifest can 410 momentarily — retry rather than fail on the first.
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(700);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1400);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(2100);
    expect(fetchMock).toHaveBeenCalledTimes(4);

    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText(/could not display this render/i)).toBeTruthy();
  });
});
