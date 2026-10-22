import { describe, expect, it } from "vitest";
import { buildTurnContext, PROJECT_TOKEN_HEADER, TURN_CONTEXT_BODY_KEY, type TurnContext } from "./turnContext";

const PID = "p0123456789abcdef0123456789abcdef";

// Mirrors `TurnContext.parse` in agent/src/project_search/context.py: anything it would reject means the
// runtime binds no search for the turn, so every built context must pass these checks.
function agentParses(raw: unknown): boolean {
  if (!raw || typeof raw !== "object") return false;
  const r = raw as Record<string, any>;
  const ap = r.active_project;
  if (!ap || typeof ap !== "object" || !/^p[0-9a-f]{32}$/.test(ap.id) || ap.collection_id !== ap.id) return false;
  return r.project_access === "ok" || r.project_access === "unavailable";
}

describe("buildTurnContext", () => {
  it("emits the design Component 2 __turn_context JSON", () => {
    const ctx = buildTurnContext({
      turnId: "t-1",
      activeProject: { id: PID, name: "Trailer cuts", personal: false },
      webSearch: false,
      projectAccess: "ok",
    });
    const wire = JSON.parse(JSON.stringify({ prompt: "hi", [TURN_CONTEXT_BODY_KEY]: ctx }));
    expect(wire.__turn_context).toEqual({
      turn_id: "t-1",
      active_project: { id: PID, name: "Trailer cuts", collection_id: PID, personal: false },
      web_search: false,
      project_access: "ok",
    } satisfies TurnContext);
    expect(agentParses(wire.__turn_context)).toBe(true);
    expect(Object.keys(wire.__turn_context).sort()).toEqual(
      ["active_project", "project_access", "turn_id", "web_search"],
    );
  });

  it("carries the unavailable path, a Personal project, and web search", () => {
    const ctx = buildTurnContext({
      turnId: "t-2",
      activeProject: { id: PID, name: "Personal", personal: true },
      webSearch: true,
      projectAccess: "unavailable",
    });
    expect(ctx.project_access).toBe("unavailable");
    expect(ctx.active_project.personal).toBe(true);
    expect(ctx.web_search).toBe(true);
    expect(agentParses(ctx)).toBe(true);
  });

  it("refuses input the runtime parser would drop", () => {
    const base = {
      turnId: "t-3",
      activeProject: { id: PID, name: "x", personal: false },
      webSearch: false,
      projectAccess: "ok" as const,
    };
    expect(() => buildTurnContext({ ...base, activeProject: { ...base.activeProject, id: "default" } })).toThrow();
    expect(() => buildTurnContext({ ...base, projectAccess: "maybe" as never })).toThrow();
    expect(() => buildTurnContext({ ...base, turnId: "" })).toThrow();
  });

  it("never carries the Project_Token, and re-exports the header name", () => {
    expect(PROJECT_TOKEN_HEADER).toBe("X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken");
    const ctx = buildTurnContext({
      turnId: "t-4",
      activeProject: { id: PID, name: "x", personal: false },
      webSearch: false,
      projectAccess: "ok",
    });
    expect(JSON.stringify(ctx)).not.toMatch(/token"/i);
  });
});
