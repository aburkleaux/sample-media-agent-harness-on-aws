import { describe, expect, it, vi } from "vitest";
import { hydrateRuntimeArns } from "./runtimeArns";

const ARN = (n: string) => `arn:aws:bedrock-agentcore:us-west-2:1:runtime/${n}`;
const ALL = { AGENT_RUNTIME_ARN: ARN("main"), TASK_AGENT_RUNTIME_ARN: ARN("task"), RENDER_AGENT_RUNTIME_ARN: ARN("render") };

describe("hydrateRuntimeArns", () => {
  it("fills the missing ARNs from the parameter path", async () => {
    const env: NodeJS.ProcessEnv = { RUNTIME_ARN_PARAM_PATH: "/p-staging/runtime-arns" };
    const fetchByPath = vi.fn().mockResolvedValue({ ...ALL, PLANNING_AGENT_RUNTIME_ARN: ARN("plan") });
    await hydrateRuntimeArns({ env, fetchByPath, log: () => {} });
    expect(fetchByPath).toHaveBeenCalledWith("/p-staging/runtime-arns");
    expect(env).toMatchObject(ALL);
    expect(env.PLANNING_AGENT_RUNTIME_ARN).toBeUndefined(); // only the proxy's three
  });

  it("lets env win and skips SSM entirely when nothing is missing or no path is set", async () => {
    const fetchByPath = vi.fn();
    await hydrateRuntimeArns({ env: { ...ALL, RUNTIME_ARN_PARAM_PATH: "/x" }, fetchByPath });
    await hydrateRuntimeArns({ env: {}, fetchByPath });
    expect(fetchByPath).not.toHaveBeenCalled();
  });

  it("retries until the parameters exist (a new environment writes them after the runtimes)", async () => {
    const env: NodeJS.ProcessEnv = { RUNTIME_ARN_PARAM_PATH: "/x" };
    const fetchByPath = vi.fn()
      .mockRejectedValueOnce(new Error("throttled"))
      .mockResolvedValueOnce({ AGENT_RUNTIME_ARN: ARN("main") })
      .mockResolvedValueOnce(ALL);
    const sleep = vi.fn().mockResolvedValue(undefined);
    await hydrateRuntimeArns({ env, fetchByPath, sleep, retryMs: 10, log: () => {} });
    expect(fetchByPath).toHaveBeenCalledTimes(3);
    expect(env).toMatchObject(ALL);
  });

  it("gives up after the wait and never throws", async () => {
    const env: NodeJS.ProcessEnv = { RUNTIME_ARN_PARAM_PATH: "/x" };
    const fetchByPath = vi.fn().mockResolvedValue({});
    const log = vi.fn();
    await hydrateRuntimeArns({ env, fetchByPath, sleep: async () => {}, retryMs: 10, maxWaitMs: 30, log });
    expect(fetchByPath).toHaveBeenCalledTimes(4);
    expect(log.mock.calls.at(-1)?.[0]).toContain("still missing");
  });
});
