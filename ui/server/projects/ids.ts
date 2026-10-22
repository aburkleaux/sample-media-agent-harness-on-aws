// Project ids (media-harness-projects-access Req 1.4, 1.5; design Component 1 `ids.ts`).
// "p" + the 32 lowercase hex digits of a UUID v4: 33 characters, unguessable, a valid M2C
// collection id (`^[a-z0-9][a-z0-9_-]{2,47}$`), and never `default`. The Python twin is
// agent/src/projects/ids.py; both suites read infra/lib/projects/fixtures/project-id-cases.json.
import { randomUUID } from "node:crypto";

const PROJECT_ID_RE = /^p[0-9a-f]{32}$/;

export function newProjectId(): string {
  return "p" + randomUUID().replaceAll("-", "");
}

export function isProjectId(s: unknown): s is string {
  return typeof s === "string" && PROJECT_ID_RE.test(s);
}
