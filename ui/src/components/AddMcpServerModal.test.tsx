// @vitest-environment jsdom
/**
 * Add MCP server form — behaviours worth pinning.
 *
 * Focus is on the rules that are easy to regress silently: a probe result must
 * not outlive the values it described, and a pasted config must never carry a
 * credential in without the user retyping it.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import AddMcpServerModal from "./AddMcpServerModal";

vi.mock("../api", () => ({
  testMcpServer: vi.fn(),
  registerMcpServer: vi.fn(),
}));

import { testMcpServer, registerMcpServer } from "../api";

const ok = {
  ok: true,
  tools: ["read_note", "search_notes"],
  tool_count: 2,
  detail: "",
};

function open() {
  const onRegistered = vi.fn();
  const onClose = vi.fn();
  render(
    <AddMcpServerModal open onClose={onClose} onRegistered={onRegistered} />
  );
  return { onRegistered, onClose };
}

const urlField = () => screen.getByPlaceholderText("https://mcp.example.com/mcp");
const nameField = () => screen.getByPlaceholderText("My Notes MCP");
// Disambiguated: the dialog itself is also labelled "Add MCP server".
const addButton = () =>
  screen.getByRole("button", { name: "Add MCP server" }) as HTMLButtonElement;

beforeEach(() => vi.clearAllMocks());
// The modal portals into document.body, so leaving it mounted would leak DOM
// between tests and make getByText ambiguous.
afterEach(() => cleanup());

describe("probe result", () => {
  it("reports the discovered tools, which is what catches a wrong path", async () => {
    vi.mocked(testMcpServer).mockResolvedValue(ok);
    open();
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    fireEvent.click(screen.getByText("Test connection"));
    await waitFor(() => screen.getByText(/Connected · 2 tools/));
    expect(screen.getByText(/read_note, search_notes/)).toBeTruthy();
  });

  it("reports zero tools as a caveat rather than a bare success", async () => {
    vi.mocked(testMcpServer).mockResolvedValue({
      ok: true, tools: [], tool_count: 0,
      detail: "Connected, but this server exposes no tools",
    });
    open();
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    fireEvent.click(screen.getByText("Test connection"));
    await waitFor(() => screen.getByText(/exposes no tools/));
  });

  it("shows the real failure message", async () => {
    vi.mocked(testMcpServer).mockResolvedValue({
      ok: false, tools: [], tool_count: 0,
      detail: "Reachable, but this doesn't look like an MCP endpoint",
    });
    open();
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/" } });
    fireEvent.click(screen.getByText("Test connection"));
    await waitFor(() => screen.getByText(/doesn't look like an MCP endpoint/));
  });

  it("discards the result when a tested value changes", async () => {
    // A stale green tick is worse than none: it would vouch for a URL the user
    // has since edited.
    vi.mocked(testMcpServer).mockResolvedValue(ok);
    open();
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    fireEvent.click(screen.getByText("Test connection"));
    await waitFor(() => screen.getByText(/Connected · 2 tools/));
    fireEvent.change(urlField(), { target: { value: "https://other.example.com/mcp" } });
    expect(screen.queryByText(/Connected · 2 tools/)).toBeNull();
  });
});

describe("submission", () => {
  it("is allowed without testing first", async () => {
    // Some servers refuse an unauthenticated initialize from an unknown client;
    // gating Add on a probe that cannot pass would strand those users.
    vi.mocked(registerMcpServer).mockResolvedValue({
      target_name: "usr_abc", target_id: "t1", status: "synchronizing",
      server_name: "My Notes", endpoint: "https://m.example.com/mcp",
    });
    const { onRegistered } = open();
    fireEvent.change(nameField(), { target: { value: "My Notes" } });
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    fireEvent.click(addButton());
    await waitFor(() => expect(onRegistered).toHaveBeenCalled());
    expect(vi.mocked(testMcpServer)).not.toHaveBeenCalled();
  });

  it("hands back a synchronizing record so the host can poll", async () => {
    vi.mocked(registerMcpServer).mockResolvedValue({
      target_name: "usr_abc", target_id: "t1", status: "synchronizing",
      server_name: "My Notes", endpoint: "https://m.example.com/mcp",
    });
    const { onRegistered } = open();
    fireEvent.change(nameField(), { target: { value: "My Notes" } });
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    fireEvent.click(addButton());
    await waitFor(() =>
      expect(onRegistered).toHaveBeenCalledWith(
        expect.objectContaining({ status: "synchronizing" })
      )
    );
  });

  it("keeps the form open and shows the reason on failure", async () => {
    vi.mocked(registerMcpServer).mockRejectedValue(
      new Error("URL must point at a publicly reachable host")
    );
    const { onClose } = open();
    fireEvent.change(nameField(), { target: { value: "My Notes" } });
    fireEvent.change(urlField(), { target: { value: "https://10.0.0.1/mcp" } });
    fireEvent.click(addButton());
    await waitFor(() => screen.getByRole("alert"));
    expect(screen.getByRole("alert").textContent).toContain("publicly reachable");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("requires a name and a url", () => {
    open();
    const add = addButton();
    expect(add.disabled).toBe(true);
    fireEvent.change(nameField(), { target: { value: "My Notes" } });
    expect((addButton()).disabled).toBe(true);
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    expect((addButton()).disabled).toBe(false);
  });
});

describe("import", () => {
  const paste = (text: string) => {
    fireEvent.click(screen.getByText("Import"));
    fireEvent.change(screen.getByLabelText("Paste JSON config"), {
      target: { value: text },
    });
    fireEvent.click(screen.getByText("Fill the form from this"));
  };

  it("fills the form rather than submitting directly", () => {
    open();
    paste(JSON.stringify({
      mcpServers: { "my-server": { url: "https://imported.example.com/mcp" } },
    }));
    expect((urlField() as HTMLInputElement).value).toBe("https://imported.example.com/mcp");
    expect((nameField() as HTMLInputElement).value).toBe("my-server");
    expect(vi.mocked(registerMcpServer)).not.toHaveBeenCalled();
  });

  it("never imports a credential silently", () => {
    open();
    paste(JSON.stringify({
      mcpServers: {
        s: {
          url: "https://imported.example.com/mcp",
          headers: { Authorization: "Bearer super-secret" },
        },
      },
    }));
    // The header NAME is adopted; the value is deliberately left blank so the
    // user knows what credential they are handing over.
    expect((screen.getByLabelText("Header value") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Header name") as HTMLInputElement).value).toBe("Authorization");
  });

  it("sends the header value whole, for the runtime to split", async () => {
    // The prefix used to be a separate field, which made a trailing space
    // load-bearing and invisible: "Key" and "Key " differ by one character and
    // produce an identical-looking 401.
    vi.mocked(testMcpServer).mockResolvedValue(ok);
    open();
    fireEvent.change(urlField(), { target: { value: "https://m.example.com/mcp" } });
    fireEvent.change(screen.getByLabelText("Header value"), {
      target: { value: "Key abc:def" },
    });
    fireEvent.click(screen.getByText("Test connection"));
    await waitFor(() => expect(vi.mocked(testMcpServer)).toHaveBeenCalled());
    expect(vi.mocked(testMcpServer).mock.calls[0][0]).toMatchObject({
      header_value: "Key abc:def",
    });
  });

  it("refuses a config with several servers rather than guessing", () => {
    open();
    paste(JSON.stringify({ mcpServers: { a: { url: "https://a/mcp" }, b: { url: "https://b/mcp" } } }));
    expect(screen.getByText(/2 servers. Paste one at a time/)).toBeTruthy();
  });

  it("does not apply an unparseable config", () => {
    // Asserting behaviour rather than the engine's JSON error text, which
    // differs between Node and jsdom.
    open();
    paste("{not json");
    fireEvent.click(screen.getByText("Remote"));
    expect((urlField() as HTMLInputElement).value).toBe("");
  });

  it("reports a config with no url", () => {
    open();
    paste(JSON.stringify({ mcpServers: { a: { command: "npx" } } }));
    expect(screen.getByText(/no url/)).toBeTruthy();
  });
});
