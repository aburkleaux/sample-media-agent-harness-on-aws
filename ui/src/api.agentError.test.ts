/**
 * `invokeAgentStreaming` error statuses (media-harness-projects-access task 13.3, Req 3.11).
 *
 * The `/api` turn gate answers `409 project_access_lost` with a JSON body naming the Personal_Project. The
 * client used to drop every error body; it now reads it for a 409 ONLY, so `parseProjectAccessLost` works,
 * while every other status throws the same message as before without touching the body.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const authFetch = vi.fn();
vi.mock("./auth/authFetch", () => ({ authFetch: (...a: unknown[]) => authFetch(...a) }));

import { invokeAgentStreaming, AgentRequestError } from "./api";
import { parseProjectAccessLost } from "./projects/api";

function errorResponse(status: number, statusText: string, body: unknown) {
  const json = vi.fn(async () => body);
  return { res: { ok: false, status, statusText, json }, json };
}

beforeEach(() => authFetch.mockReset());

describe("invokeAgentStreaming error statuses", () => {
  it("reads the 409 body so project_access_lost is recognised", async () => {
    const body = { error: { code: "project_access_lost", personalProject: { id: "pPersonal", name: "Personal" } } };
    authFetch.mockResolvedValueOnce(errorResponse(409, "Conflict", body).res);
    const err = await invokeAgentStreaming({ prompt: "x" }, () => {}).catch((e) => e);
    expect(err).toBeInstanceOf(AgentRequestError);
    expect(err.message).toBe("Agent request failed: 409 Conflict");
    expect(parseProjectAccessLost(err.status, err.body)).toEqual({ id: "pPersonal", name: "Personal" });
  });

  it("keeps the old message for other statuses and does not read their body", async () => {
    const { res, json } = errorResponse(423, "Locked", { error: "Session is locked" });
    authFetch.mockResolvedValueOnce(res);
    const err = await invokeAgentStreaming({ prompt: "x" }, () => {}).catch((e) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe("Agent request failed: 423 Locked");
    expect(err.status).toBe(423);
    expect(err.body).toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });
});
