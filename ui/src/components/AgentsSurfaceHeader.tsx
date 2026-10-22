import { SurfaceHeader, surfaceNestedStyle, surfaceStyle } from './SurfaceHeader';

/**
 * The Agents surface header — title, description, and the rule beneath them.
 *
 * Rendered on EVERY Agents branch (inventory, published detail, editor), the same
 * contract Skills already had. It used to belong to the inventory alone: opening a
 * published agent replaced it with that agent's own header, and opening the editor
 * replaced it with a bare "Edit Agent" row, so in both cases the one thing saying
 * which surface you were on disappeared at the moment you went one level into it.
 *
 * A module rather than a local in `WorkspaceView` for the same reason as
 * `SkillsSurfaceHeader`: the three branches are separate components, and a header
 * that any of them can forget to render is a header that drifts.
 */
export function AgentsSurfaceHeader() {
  return (
    <SurfaceHeader
      title="Agents"
      subtitle="Create and configure custom agents, then test and publish them."
    />
  );
}

/** Shell for an Agents branch: fixed header, one child taking the rest. */
export const agentsSurfaceStyle = surfaceStyle;

/** Takes the height the header leaves — see `surfaceNestedStyle`. */
export const agentsNestedStyle = surfaceNestedStyle;
