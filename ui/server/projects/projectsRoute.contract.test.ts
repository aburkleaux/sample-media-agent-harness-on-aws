/**
 * `/projects` must be registered in all four places (AGENTS.md "Adding a proxy route").
 *
 * A new `ui/server` path fails silently when any one is missing:
 *   1. `app.ts` mount      — without it the request falls through to the `/api` catch-all or 404s.
 *   2. `authPaths`         — without it `req.actorId` is never set and the handler 401s every call.
 *   3. `PROTECTED_PREFIXES`— without it `authFetch` sends no token and the proxy 401s every call.
 *   4. CloudFront `additionalBehaviors` — an explicit allowlist. An unlisted path falls through to the
 *      SPA bucket and returns `index.html` with HTTP 200, which reads as "the API returned raw HTML".
 *      CloudFront patterns are exact, so the bare `/projects` and `/projects/*` are both required.
 *
 * Also asserts the proxy image copies the `ui/src/` directories that `ui/server/projects/` imports:
 * `proxy.Dockerfile` copies only selected `src/` folders, so an import of an uncopied one passes every
 * test here and fails only in `npm run build:server` inside the image build.
 *
 * Reads the real sources (in the style of `capability-api.routing.contract.test.ts`) so the assertions
 * track the files rather than a copy that can rot.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(resolve(here, rel), "utf8");

function serverAuthPaths(): string[] {
  const m = read("../app.ts").match(/const authPaths\s*=\s*\[([^\]]*)\]/);
  if (!m) throw new Error("could not locate authPaths in ui/server/app.ts");
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

function clientProtectedPrefixes(): string[] {
  const m = read("../../src/auth/authFetch.ts").match(/const PROTECTED_PREFIXES:[^=]*=\s*\[([\s\S]*?)\];/);
  if (!m) throw new Error("could not locate PROTECTED_PREFIXES in authFetch.ts");
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

function cloudFrontBehaviorPaths(): string[] {
  // The stack defines MORE THAN ONE `additionalBehaviors` block (the UI CDN plus
  // the artifacts CDN's `projects/*` refusal behaviour). A single non-greedy
  // match would lock onto whichever appears first and miss the UI behaviors, so
  // scan every block and collect `uiApiBehavior` paths across all of them.
  const blocks = [
    ...read("../../../infra/lib/agent-infra-stack.ts").matchAll(/additionalBehaviors:\s*\{([\s\S]*?)\n {6}\},/g),
  ];
  if (blocks.length === 0) throw new Error("could not locate additionalBehaviors in the CDK stack");
  return blocks.flatMap((b) => [...b[1].matchAll(/"([^"]+)":\s*uiApiBehavior/g)].map((x) => x[1]));
}

describe("/projects routing registrations", () => {
  it("mounts the projects handler in app.ts after the authPaths middleware", () => {
    const src = read("../app.ts");
    const mount = src.indexOf('app.use("/projects", createProjectsHandler(');
    const authLoop = src.indexOf("for (const p of authPaths)");
    expect(mount, "app.use(\"/projects\", createProjectsHandler(...)) is missing").toBeGreaterThan(-1);
    expect(authLoop).toBeGreaterThan(-1);
    // Express runs middleware in registration order: mounted before the auth loop, the handler
    // would run with no `req.actorId`.
    expect(mount).toBeGreaterThan(authLoop);
    expect(src).toMatch(/import \{ createProjectsHandler \} from "\.\/projects\/routes";/);
  });

  it("protects /projects in the proxy authPaths", () => {
    expect(serverAuthPaths()).toContain("/projects");
  });

  it("attaches the access token client-side", () => {
    expect(clientProtectedPrefixes()).toContain("/projects");
  });

  it("has both the exact and wildcard CloudFront behaviors on the proxy origin", () => {
    const paths = cloudFrontBehaviorPaths();
    expect(paths).toContain("/projects");
    expect(paths).toContain("/projects/*");
    // The same behavior `/sessions` uses (the proxy ALB origin).
    expect(paths).toContain("/sessions");
  });

  it("copies every ui/src directory that ui/server/projects imports into the proxy image", () => {
    const dockerfile = read("../../proxy.Dockerfile");
    const dirs = new Set<string>();
    for (const f of readdirSync(here)) {
      if (!f.endsWith(".ts") || f.endsWith(".test.ts")) continue;
      for (const m of read(f).matchAll(/from "\.\.\/\.\.\/src\/([^/"]+)\//g)) dirs.add(m[1]);
    }
    expect(dirs.size, "expected registry.ts's src/projects import").toBeGreaterThan(0);
    for (const d of dirs) {
      expect(dockerfile, `proxy.Dockerfile must COPY src/${d}`).toContain(`COPY src/${d} ./src/${d}`);
    }
  });

  it("found non-empty lists everywhere", () => {
    // Guards against a regex that silently matches nothing and passes vacuously.
    expect(serverAuthPaths().length).toBeGreaterThan(3);
    expect(clientProtectedPrefixes().length).toBeGreaterThan(3);
    expect(cloudFrontBehaviorPaths().length).toBeGreaterThan(5);
  });
});
