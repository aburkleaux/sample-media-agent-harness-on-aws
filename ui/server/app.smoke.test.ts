// Module-load smoke test for the proxy app.
//
// `buildApp()` wires every route at startup, so a stray identifier in any mount
// site is a `ReferenceError` the moment the container boots — not a lazy failure
// on first request. Nothing else in CI catches that:
//
//   - `tsconfig.json` has `"include": ["src"]`, so **`ui/server/**` is never
//     typechecked** by `npx tsc --noEmit`.
//   - `npm run build:server` is esbuild, which bundles without type checking and
//     emits the broken reference verbatim.
//
// That combination shipped `ReferenceError: mediaProxyBucket is not defined` to
// Fargate, where the task crash-looped (exit 1) through the whole ECS
// stabilization window. It came from a *clean* three-way merge: staging's
// `0d35d90` revert deleted the `const mediaProxyBucket` declaration while our
// side still referenced it at the download-route mount site. Git had no textual
// conflict to report, and every suite stayed green.
//
// So: call `buildApp()` for real. It needs no arguments (see `index.ts`).
//
// `AGENT_RUNTIME_ARN` is a deliberate hard requirement — `buildApp()` throws a
// named error without it, which is a *configuration* check, not the failure this
// file guards. The distinction matters: a clear "env var is required" throw is
// the designed behavior, whereas a `ReferenceError` is a code defect. So the
// minimal env below sets only that ARN, leaving every optional var unset to
// exercise the "unset ⇒ route disabled / allowlist narrows" paths.

import { describe, it, expect, beforeEach, afterEach } from "vitest";

import { buildApp } from "./app";

// Vars the app reads at wire-up time. Cleared to prove `buildApp()` survives a
// bare environment (the "unset ⇒ route disabled / allowlist narrows" paths)
// rather than only working when everything happens to be configured.
const WIRE_UP_VARS = [
  "AGENT_RUNTIME_ARN",
  "UPLOAD_BUCKET",
  "MEDIA_PROXY_BUCKET",
  "SESSIONS_TABLE",
  "JOB_REGISTRY_TABLE",
  "ASSET_INDEX_TABLE",
  "AGENT_RUNTIME_ARN",
  "TASK_AGENT_RUNTIME_ARN",
  "RENDER_AGENT_RUNTIME_ARN",
  "MEMORY_ID",
];

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const k of WIRE_UP_VARS) saved[k] = process.env[k];
});

afterEach(() => {
  for (const k of WIRE_UP_VARS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

/** The one var `buildApp()` hard-requires; everything else is optional. */
const RUNTIME_ARN =
  "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/test_agent-AAAAAAAAAA";

describe("buildApp() module load", () => {
  it("wires every route with only the required env var set", () => {
    for (const k of WIRE_UP_VARS) delete process.env[k];
    process.env.AGENT_RUNTIME_ARN = RUNTIME_ARN;

    // A ReferenceError here means an identifier at some mount site is undefined
    // — the exact failure that crash-looped the container. Optional vars being
    // unset must degrade, not crash.
    expect(() => buildApp()).not.toThrow();
  });

  it("wires every route without throwing when the environment is populated", () => {
    process.env.AGENT_RUNTIME_ARN = RUNTIME_ARN;
    process.env.TASK_AGENT_RUNTIME_ARN = RUNTIME_ARN;
    process.env.RENDER_AGENT_RUNTIME_ARN = RUNTIME_ARN;
    process.env.UPLOAD_BUCKET = "test-uploads";
    process.env.MEDIA_PROXY_BUCKET = "test-m2c-proxy";
    process.env.SESSIONS_TABLE = "test-sessions";
    process.env.JOB_REGISTRY_TABLE = "test-jobs";
    process.env.ASSET_INDEX_TABLE = "test-assets";

    expect(() => buildApp()).not.toThrow();
  });

  it("fails with a named configuration error, not a ReferenceError, when the required ARN is absent", () => {
    for (const k of WIRE_UP_VARS) delete process.env[k];
    delete process.env.AGENT_RUNTIME_ARN;

    expect(() => buildApp()).toThrow(/AGENT_RUNTIME_ARN env var is required/);
    // Guards the distinction this file exists to make.
    expect(() => buildApp()).not.toThrow(ReferenceError);
  });

  it("returns an app exposing the external invoke + download routes", () => {
    process.env.AGENT_RUNTIME_ARN = RUNTIME_ARN;
    process.env.UPLOAD_BUCKET = "test-uploads";
    process.env.MEDIA_PROXY_BUCKET = "test-m2c-proxy";
    process.env.ASSET_INDEX_TABLE = "test-assets";

    const app = buildApp();
    expect(typeof app.listen).toBe("function");

    // Assert the download route is actually mounted, not merely that wiring
    // didn't crash. A silently-absent route is the other half of this failure
    // mode: CloudFront rewrites the resulting 404 to 200 /index.html, so it
    // surfaces as the agent "replying with raw HTML".
    const paths = collectRoutePaths(app);
    expect(paths).toContain("/invoke/:agentId/download");
    expect(paths).toContain("/invoke/:agentId");
  });
});

/** Flatten registered route paths out of the Express router stack. */
function collectRoutePaths(app: any): string[] {
  const out: string[] = [];
  const stack = app?._router?.stack ?? app?.router?.stack ?? [];
  for (const layer of stack) {
    if (layer?.route?.path) {
      const p = layer.route.path;
      if (Array.isArray(p)) out.push(...p);
      else out.push(p);
    }
  }
  return out;
}
