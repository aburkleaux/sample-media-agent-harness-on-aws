// The proxy-authored turn context (media-harness-projects-access Req 3.8, 4.17, 9.10, 9.11; design
// Component 2 and "Interfaces provided to sibling specs").
//
// The turn gate stamps this object into the outbound chat body as `__turn_context`, replacing any copy the
// client sent. The Main_Agent parses it with `TurnContext.parse` in `agent/src/project_search/context.py`,
// which rejects the whole object (no search for the turn) unless `active_project.id` is a Project id,
// `active_project.collection_id === active_project.id`, and `project_access` is `ok` | `unavailable`.
// `buildTurnContext` enforces the same rules, so the proxy fails loudly instead of emitting a context the
// runtime would silently drop.
//
// Sibling specs read these fields: search-qa-rendering reads `active_project {id, name, collection_id}`;
// web-search-toggle reads `web_search`. The Project_Token never appears here: it travels in
// `PROJECT_TOKEN_HEADER` only (Req 4.15).
import { isProjectId } from "./ids";

export { PROJECT_TOKEN_HEADER } from "./tokenIssuerClient";

/** The body key the turn context is sent under. */
export const TURN_CONTEXT_BODY_KEY = "__turn_context";

/** `unavailable` is the Req 4.17 path: the issuer timed out or failed, so the turn has no token. */
export type ProjectAccess = "ok" | "unavailable";

/**
 * A card's Find_Similar reference resolved by the proxy (media-harness-search-qa-rendering Req 3.23). Stamped
 * only for a turn started from a Moment card's "Find similar"; the runtime reads it as `FindSimilar.parse`
 * (`agent/src/project_search/context.py`). The URI is not trusted for being here — the Search_Tool's
 * image-path check applies to it like any other `search_image_s3uri`.
 */
export interface TurnContextFindSimilar {
  /** `s3://<bucket>/<key>` the first search uses as `search_image_s3uri`. */
  image_uri: string;
  /** Display_Name from the registry row (not the client). */
  display_name: string;
  /** The Citation timecode span, e.g. `01:14`+en-dash+`01:36`. */
  timecode: string;
}

export interface TurnContextActiveProject {
  id: string;
  name: string;
  /** Always equal to `id`: the Project's M2C collection id. */
  collection_id: string;
  personal: boolean;
  /** Project settings: `false` = files are stored, not analyzed (no semantic search). Absent when unknown. */
  indexing?: boolean;
  /** Project settings: the members' description of the Project, for the agent. Absent when empty. */
  description?: string;
  /** Project settings: `true` = the Project has a knowledge graph (agents get the graph tools). Absent = no. */
  graph?: boolean;
}

/** The `__turn_context` JSON object, field for field (snake_case, as the runtime reads it). */
export interface TurnContext {
  turn_id: string;
  active_project: TurnContextActiveProject;
  /** The value for media-harness-web-search-toggle. */
  web_search: boolean;
  project_access: ProjectAccess;
  /**
   * media-harness-search-qa-rendering (Req 3.23): present only for a turn started from a card's Find_Similar
   * action. Absent on every other turn, so the body is unchanged. The runtime drops a malformed value.
   */
  find_similar?: TurnContextFindSimilar;
}

export interface BuildTurnContextInput {
  turnId: string;
  activeProject: { id: string; name: string; personal: boolean; indexing?: boolean; description?: string; graph?: boolean };
  webSearch: boolean;
  projectAccess: ProjectAccess;
  /** media-harness-search-qa-rendering (Req 3.23): the resolved Find_Similar object, when the turn has one. */
  findSimilar?: TurnContextFindSimilar;
}

/**
 * Builds the `__turn_context` object. Throws on input the runtime parser would reject (a malformed Project
 * id, an unknown access value or arm, an empty turn id), since that is a proxy bug, not a user error.
 */
export function buildTurnContext(input: BuildTurnContextInput): TurnContext {
  const { turnId, activeProject, webSearch, projectAccess, findSimilar } = input;
  if (typeof turnId !== "string" || turnId === "") {
    throw new Error("buildTurnContext: turnId must be a non-empty string");
  }
  if (!isProjectId(activeProject?.id)) {
    throw new Error("buildTurnContext: activeProject.id is not a Project id");
  }
  if (projectAccess !== "ok" && projectAccess !== "unavailable") {
    throw new Error("buildTurnContext: projectAccess must be ok or unavailable");
  }
  return {
    turn_id: turnId,
    active_project: {
      id: activeProject.id,
      name: String(activeProject.name ?? ""),
      collection_id: activeProject.id,
      personal: activeProject.personal === true,
      ...(typeof activeProject.indexing === "boolean" ? { indexing: activeProject.indexing } : {}),
      ...(activeProject.description ? { description: String(activeProject.description).slice(0, 500) } : {}),
      ...(activeProject.graph === true ? { graph: true } : {}),
    },
    web_search: webSearch === true,
    project_access: projectAccess,
    // Omitted entirely when absent, so a non-Find_Similar turn's context is byte-for-byte unchanged.
    ...(findSimilar ? { find_similar: findSimilar } : {}),
  };
}
