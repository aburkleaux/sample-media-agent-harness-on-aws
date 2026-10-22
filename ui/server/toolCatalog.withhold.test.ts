/**
 * Withholding the M2C hybrid-search tools from the proxy catalog
 * (media-harness-projects-access task 11.8, proxy half; design Component 8).
 *
 * The runtime (`agent/src/mcp_manager.py`) and the proxy drop the SAME two prefixes
 * (`hybrid-search___`, `hybrid-search-admin___`), matched against the full tool name and the name
 * after one `___` split. These tests pin that behaviour on the proxy side, both for the pure helpers
 * and end-to-end through `catalogWithoutRuntime`.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  WITHHELD_TOOL_PREFIXES,
  catalogWithoutRuntime,
  isWithheldToolName,
  withholdTools,
  type CatalogTool,
} from "./toolCatalog";

describe("isWithheldToolName", () => {
  it("drops a bare hybrid-search tool and a namespaced one (match full name and once-split remainder)", () => {
    for (const name of [
      "hybrid-search___search_content",
      "hybrid-search-admin___reindex",
      "m2c-search___hybrid-search___search_content",
      "mediaworks-harness-m2c-search___hybrid-search-admin___create_collection",
    ]) {
      expect(isWithheldToolName(name), name).toBe(true);
    }
  });

  it("keeps everything whose full and once-split forms do not start with a withheld prefix", () => {
    for (const name of [
      "search_content",
      "normal_tool",
      "gw___search_content",
      // Prefixes carry the `___`, so a tool merely NAMED `hybrid-search-helper` is not withheld.
      "hybrid-search-helper",
      "hybrid-search",
      // The withheld token only matters as the first `___` segment of the remainder, not deeper.
      "gw___wrap___hybrid-search___x",
    ]) {
      expect(isWithheldToolName(name), name).toBe(false);
    }
  });

  it("exposes exactly the shared prefixes (agent fixture withheld-tool-prefixes.json)", () => {
    // The agent fixture is the source of truth — the two lists drift silently otherwise.
    const fixture = JSON.parse(readFileSync(resolve(__dirname, "../../agent/src/project_search/withheld-tool-prefixes.json"), "utf8"));
    expect([...WITHHELD_TOOL_PREFIXES]).toEqual(fixture);
  });

  it("withholds the knowledge-graph tools (Search_Gateway only)", () => {
    expect(isWithheldToolName("project-graph___graph_query")).toBe(true);
    expect(isWithheldToolName("tgt___project-graph___graph_find")).toBe(true);
    expect(isWithheldToolName("graph_query")).toBe(false);
  });
});

describe("withholdTools", () => {
  it("removes withheld tools and preserves the order of the rest", () => {
    const tools: CatalogTool[] = [
      { name: "a_tool", description: "" },
      { name: "m2c___hybrid-search___search_content", description: "" },
      { name: "b_tool", description: "" },
      { name: "hybrid-search-admin___reindex", description: "" },
      { name: "c_tool", description: "" },
    ];
    expect(withholdTools(tools).map((t) => t.name)).toEqual(["a_tool", "b_tool", "c_tool"]);
  });
});

describe("catalogWithoutRuntime drops withheld tools from a gateway section", () => {
  const GATEWAY_URL = "https://abc123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp";

  // A minimal DynamoDBDocumentClient stand-in: builtins (GetCommand), the enabled gateway
  // descriptor and empty target-ownership (QueryCommand, by PK).
  const fakeDdb = {
    async send(cmd: any) {
      const input = cmd?.input ?? {};
      if (input.Key) return { Item: { tools: [{ name: "echo", description: "" }] } };
      const pk = input.ExpressionAttributeValues?.[":pk"];
      if (pk === "GATEWAY#REGISTERED") {
        return { Items: [{ id: "gw1", name: "Search GW", url: GATEWAY_URL, enabled: true }] };
      }
      if (pk === "TARGET#OWNER") return { Items: [] };
      return {};
    },
  } as any;

  it("filters hybrid-search tools returned by the control plane for a gateway", async () => {
    const gatewayTools = async (): Promise<CatalogTool[]> => [
      { name: "m2c-search___hybrid-search___search_content", description: "" },
      { name: "m2c-search___hybrid-search-admin___create_collection", description: "" },
      { name: "m2c-search___describe_tool", description: "" },
    ];

    const assembly = await catalogWithoutRuntime("us-west-2", fakeDdb, "projects-table", "actor-1", {
      gatewayTools,
    });

    expect(assembly).not.toBeNull();
    const gatewaySection = assembly!.sections.find((s) => s.label === "Gateway: Search GW");
    expect(gatewaySection).toBeTruthy();
    expect(gatewaySection!.tools.map((t) => t.name)).toEqual(["m2c-search___describe_tool"]);
  });

  it("withholds the prompt-registry admin target and write tools, keeps the reads", () => {
    for (const name of ["prompt-registry-admin___put_prompt", "prompt-registry-admin___get_prompt",
      "prompt-registry___put_prompt", "prompt-registry___promote_prompt", "prompt-registry___delete_prompt",
      "prompt-registry___deprecate_prompt"]) expect(isWithheldToolName(name), name).toBe(true);
    for (const name of ["prompt-registry___get_prompt", "prompt-registry___list_prompts", "prompt-registry___describe_tool"])
      expect(isWithheldToolName(name), name).toBe(false);
  });
});
