/**
 * Standalone entry point for the agentcore-proxy Express app — used by the
 * container image (`ui/proxy.Dockerfile`) that runs as an ECS Fargate task
 * in staging/prod. Local dev uses `ui/agentcore-proxy.ts` (a Vite plugin
 * wrapping the same `buildApp()`) instead of this file.
 */

import { buildApp } from "./app";
import { hydrateRuntimeArns } from "./runtimeArns";

const port = Number(process.env.PORT ?? 8080);

// The runtime ARNs are SSM parameters, not task env (no task-definition → runtime deploy edge).
void hydrateRuntimeArns().then(() => {
  buildApp().listen(port, () => {
    console.log(`[agentcore-proxy] listening on port ${port}`);
  });
});
