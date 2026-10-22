// AgentCore Web Search deployment flag on the proxy (media-harness-web-search-toggle, 2026-10-20 update).
// CDK sets WEB_SEARCH_ENABLED=true on the proxy task only where env-config `webSearch.enabled`; `/config` reports it
// as `webSearchAvailable` (the + menu toggle) and the turn gate forces `web_search` off without it.
import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";

import { webSearchFeatureEnabled } from "./access";

describe("webSearchFeatureEnabled", () => {
  it("is on only for an explicit true", () => {
    expect(webSearchFeatureEnabled({ WEB_SEARCH_ENABLED: "true" } as NodeJS.ProcessEnv)).toBe(true);
    expect(webSearchFeatureEnabled({ WEB_SEARCH_ENABLED: " TRUE " } as NodeJS.ProcessEnv)).toBe(true);
    expect(webSearchFeatureEnabled({ WEB_SEARCH_ENABLED: "false" } as NodeJS.ProcessEnv)).toBe(false);
    expect(webSearchFeatureEnabled({} as NodeJS.ProcessEnv)).toBe(false);
  });

  it("/config reports it as webSearchAvailable (with Projects on), and the turn gate reads it", () => {
    const app = readFileSync(resolve(__dirname, "../app.ts"), "utf8");
    expect(app).toContain("webSearchAvailable: projectsEnabled() && webSearchFeatureEnabled(),");
    const gate = readFileSync(resolve(__dirname, "turnGate.ts"), "utf8");
    expect(gate).toContain("const webSearch: boolean = webSearchFeatureEnabled() &&");
  });
});
