import { SurfaceHeader, surfaceNestedStyle, surfaceStyle } from './SurfaceHeader';

/**
 * The Skills surface header — title, description, and the rule beneath them.
 *
 * Rendered on EVERY Skills branch, not just the inventory; see `SurfaceHeader`
 * for why that matters and for the metrics, which Agents shares.
 *
 * It lives in its own module because it has TWO hosts, not one. `SkillsCenter`
 * renders it for the inventory, detail and harness branches; `WorkspaceView`
 * renders it for the EDITOR branch, which replaces `SkillsCenter` outright rather
 * than nesting inside it. Keeping the definition in either host would make the
 * other import that host's whole subtree just to reach a header.
 */
export function SkillsSurfaceHeader() {
  return (
    <SurfaceHeader
      title="Skills"
      subtitle="Capabilities an agent can load. Author them here, then attach them to an agent."
    />
  );
}

/** Shell for a Skills branch: fixed header, one child taking the rest. */
export const skillsSurfaceStyle = surfaceStyle;

/** Takes the height the header leaves — see `surfaceNestedStyle`. */
export const skillsNestedStyle = surfaceNestedStyle;
