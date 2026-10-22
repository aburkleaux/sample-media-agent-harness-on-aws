// Session-row fields for Projects (media-harness-projects-access task 6.11; design Component 1 "selection
// reuses PATCH /sessions/:id", Data Models "Session row additions", D7; Req 2.6, 3.3, 3.6).
//
// `app.ts`'s `/sessions` handler calls these two functions and nothing else from Projects. Both return
// "nothing to add" when `projectsEnabled()` is false, so an env without Projects (prod, D7) writes exactly
// the session rows it wrote before this feature: no Personal lookup, no new attributes.
//
//   * `sessionCreateFields` — the extra attributes `POST /sessions` puts in its one `PutItem`:
//     `activeProjectId` (the Actor's Personal_Project). The `PutItem` is conditional on
//     `attribute_not_exists(sessionId)`.
//   * `sessionPatchFields` — the attributes `PATCH /sessions/:id` may add to its update expression for
//     `{activeProjectId, webSearch}`, after `checkAccess` confirms the Actor may use the Project. A denial
//     is the uniform 404 and the route writes nothing. A different project on a session that already has a turn
//     is `409 session_project_fixed` (D1, R2). The result only ever names `activeProjectId`,
//     `activeProjectSetAt`, and `webSearch`.
import { checkAccess, ensurePersonalProject, PROJECT_NOT_FOUND_BODY, PROJECT_NOT_FOUND_STATUS, projectsEnabled } from "./access";
import { projectChangeRefused, SESSION_PROJECT_FIXED_STATUS, sessionProjectFixedBody } from "./turnGate";
import type { TurnGateSessionStore } from "./turnGate";

export interface SessionCreateFields {
  /** Absent only when the Personal_Project could not be resolved; the turn gate falls back to it. */
  activeProjectId?: string;
}

/**
 * Extra attributes for a new session row, or `null` when Projects are disabled (the row is unchanged).
 *
 * A registry failure while resolving the Personal_Project (busy, id allocation, a DynamoDB error) does not
 * block session creation: the row is written without `activeProjectId`, and the turn gate resolves the
 * Personal_Project when the session's first turn arrives (design Component 2 step 1).
 */
export async function sessionCreateFields(
  actorSub: string,
  _nowMs: number,
): Promise<SessionCreateFields | null> {
  if (!projectsEnabled()) return null;
  const personalId = await ensurePersonalProject(actorSub).then(
    (p) => p.projectId,
    (err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`[projects] POST /sessions could not resolve the Personal_Project: ${msg.slice(0, 200)}`);
      return undefined;
    },
  );
  const fields: SessionCreateFields = {};
  if (personalId) fields.activeProjectId = personalId;
  return fields;
}

/** Attributes `PATCH /sessions/:id` may set for Projects. */
export interface SessionPatchFields {
  activeProjectId?: string;
  /** Epoch milliseconds; set whenever `activeProjectId` is. */
  activeProjectSetAt?: number;
  webSearch?: boolean;
}

/** The session being patched, for the binding check (D1, R2). */
export interface SessionPatchBinding {
  sessionId: string;
  /** Reads the Actor's own row (`dynamoSessionStore`); `null` skips the check. */
  sessions: TurnGateSessionStore | null;
}

export type SessionPatchResult =
  | { ok: true; set: SessionPatchFields }
  | { ok: false; status: number; body: unknown };

/**
 * Validate `{activeProjectId, webSearch}` from a `PATCH /sessions/:id` body. Returns the attributes to add to
 * the update expression, or the response to send instead of writing. With Projects disabled both keys are
 * ignored, as they were before this feature (D7).
 *
 * With `binding`, a different `activeProjectId` on a session that already has a turn is refused with the turn
 * gate's `409 session_project_fixed` (the same `projectChangeRefused` rule). A row read failure does not block the
 * write: like the gate, the binding check fails open.
 */
export async function sessionPatchFields(
  actorSub: string,
  body: Record<string, unknown>,
  nowMs: number,
  binding?: SessionPatchBinding,
): Promise<SessionPatchResult> {
  if (!projectsEnabled()) return { ok: true, set: {} };
  const set: SessionPatchFields = {};
  if (body.webSearch !== undefined) {
    if (typeof body.webSearch !== "boolean") {
      return { ok: false, status: 400, body: { error: "webSearch must be a boolean" } };
    }
    set.webSearch = body.webSearch;
  }
  if (body.activeProjectId !== undefined) {
    // `checkAccess` refuses a non-string or malformed id before any read; every denial is one 404.
    const access = await checkAccess(actorSub, body.activeProjectId as string);
    if (!access.ok) return { ok: false, status: PROJECT_NOT_FOUND_STATUS, body: PROJECT_NOT_FOUND_BODY };
    const projectId = access.project.projectId;
    if (binding?.sessions) {
      const row = await binding.sessions.read(binding.sessionId, actorSub).catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        console.warn(`[projects] PATCH /sessions could not read the session row: ${msg.slice(0, 200)}`);
        return null;
      });
      if (projectChangeRefused(row, projectId)) {
        return { ok: false, status: SESSION_PROJECT_FIXED_STATUS, body: sessionProjectFixedBody(row!.activeProjectId!) };
      }
    }
    set.activeProjectId = projectId;
    set.activeProjectSetAt = nowMs;
  }
  return { ok: true, set };
}
