/**
 * "Start a new chat in <project>" from anywhere in the chat (asset-lineage D1, R2.1).
 *
 * A session's project is fixed by its first turn, so the Plus_Menu answers a pick of another project with a NEW
 * chat in it. It cannot reach the host's `startNewSession(project)` (the Projects_Surface's `New chat` path) through
 * props, so it dispatches this window event and `WorkspaceView` runs that same path. Same shape as `chat:send`.
 */
export const NEW_CHAT_IN_PROJECT_EVENT = 'projects:new-chat';

export interface NewChatInProjectDetail {
  projectId: string;
  projectName: string;
}

/** Ask the host to open a new chat session whose Active_Project is `projectId`. */
export function requestNewChatInProject(projectId: string, projectName: string): void {
  window.dispatchEvent(
    new CustomEvent<NewChatInProjectDetail>(NEW_CHAT_IN_PROJECT_EVENT, { detail: { projectId, projectName } }),
  );
}

/** Subscribe the host; returns the unsubscribe. Events without a project id are ignored. */
export function onNewChatInProjectRequest(handler: (detail: NewChatInProjectDetail) => void): () => void {
  const listener = (e: Event) => {
    const d = (e as CustomEvent<Partial<NewChatInProjectDetail>>).detail;
    if (d && typeof d.projectId === 'string' && d.projectId) {
      handler({ projectId: d.projectId, projectName: typeof d.projectName === 'string' ? d.projectName : '' });
    }
  };
  window.addEventListener(NEW_CHAT_IN_PROJECT_EVENT, listener);
  return () => window.removeEventListener(NEW_CHAT_IN_PROJECT_EVENT, listener);
}
