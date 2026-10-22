/**
 * Vite dev-server plugin that mounts the same Express app used in production
 * (`ui/server/app.ts`) onto `server.middlewares`. This is the local-dev-only
 * entry point — `ui/server/index.ts` is the standalone entry point used by
 * the container image (ECS Fargate in staging/prod).
 *
 * Express apps are themselves connect-style `(req, res, next)` middleware
 * functions, so the whole route table mounts in one `use()` call — no
 * per-route rewiring needed here. See `ui/server/app.ts` for the routes,
 * env vars, and all proxy logic.
 */

import type { Plugin } from "vite";
import { buildApp } from "./server/app";

export default function agentcoreProxy(): Plugin {
  return {
    name: "agentcore-proxy",
    configureServer(server) {
      const app = buildApp();
      server.middlewares.use(app);
    },
  };
}
