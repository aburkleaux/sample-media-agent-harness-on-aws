/**
 * "Open in Projects" from a chat Renderer tab (the `project-browser` template): switch the centre to the Projects
 * surface with that Project selected. WorkspaceView listens, as it does for `lab:open-project-graph`.
 */
export const OPEN_PROJECT_EVENT = 'lab:open-project';

/** `folder` (a file's location, `""` = the top level) opens that folder in the Assets grid. */
export function openProject(projectId: string, folder?: string): void {
  window.dispatchEvent(new CustomEvent(OPEN_PROJECT_EVENT, { detail: { projectId, ...(folder !== undefined ? { folder } : {}) } }));
}
