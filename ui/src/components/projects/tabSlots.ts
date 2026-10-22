/**
 * Projects_Surface tab-slot registry (media-harness-projects-access task 13.5; design Component 11,
 * "Interfaces provided to sibling specs").
 *
 * The `Assets`, `Upload`, and `S3 location` slots are owned by media-harness-project-ingestion. It fills
 * them with `registerProjectTab(key, Component)`. Until it does, the surface never shows a placeholder:
 * an unregistered `assets` slot renders the `No assets yet` empty state (`ProjectAssetsSection`), and an
 * unregistered `upload` / `s3-location` slot has no toolbar button at all (`ProjectHeader`). Each slot
 * component receives the Selected_Project (its Target_Project, Req 10.6), the caller's role, and an
 * `onNotFound` callback that triggers the Req 10.10 lost-access path on the surface.
 */
import type { ComponentType } from 'react';

/** The three ingestion-owned slots. `Members` is owned here and is not a slot. */
export type ProjectTabKey = 'assets' | 'upload' | 's3-location';

export interface ProjectTabProps {
  /** The Selected_Project (its Target_Project). `activeAssetCount` is absent when unknown, never 0-as-unknown. */
  project: { id: string; name: string; personal: boolean; activeAssetCount?: number };
  role: 'owner' | 'member';
  /** Call when a request for this Project returns the uniform not-found (Req 10.10). */
  onNotFound(): void;
  /** The chat session on screen, if any: an asset opened here plays in that session's Renderer panel. */
  sessionId?: string | null;
  /**
   * The Project's indexing setting when the host already knows it (ProjectMeta.indexing; absent on the META means
   * ON). Undefined = unknown: the Assets section reads `GET /projects/:pid` itself, and only when it needs to.
   */
  indexing?: boolean;
}

const slots = new Map<ProjectTabKey, ComponentType<ProjectTabProps>>();

/** Register the component for one ingestion-owned tab. A later registration replaces an earlier one. */
export function registerProjectTab(key: ProjectTabKey, component: ComponentType<ProjectTabProps>): void {
  slots.set(key, component);
}

/** The registered component for a tab, or `undefined` when the slot is unfilled in this build. */
export function getProjectTab(key: ProjectTabKey): ComponentType<ProjectTabProps> | undefined {
  return slots.get(key);
}

/** Test-only: drop all registrations. */
export function __resetProjectTabsForTests(): void {
  slots.clear();
}
