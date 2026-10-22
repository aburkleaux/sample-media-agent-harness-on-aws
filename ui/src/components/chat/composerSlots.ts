/**
 * Composer extension slots (media-harness-projects-access task 13.2; design Component 4 and
 * "Interfaces provided to sibling specs").
 *
 * Two sibling specs plug UI into the chat composer without this spec importing their code:
 *
 *   * media-harness-web-search-toggle registers the Web_Search_Toggle that the Plus_Menu shows
 *     beside a globe, after `Attach files` (Req 3.4), with {@link registerWebSearchToggleSlot}.
 *   * media-harness-project-ingestion registers the Upload_Sheet that ChatCanvas mounts above
 *     the composer while chosen files are uploading, with {@link registerUploadSheet}
 *     (`projects/upload/registerUploadUi.ts`).
 *
 * An unregistered slot renders nothing: the Plus_Menu omits the web-search row, and
 * {@link UploadSheetMount} renders `null`. Registering again replaces the previous component;
 * the returned function unregisters it (only if it is still the registered one).
 */
import React, { useSyncExternalStore } from 'react';

/** Props the Plus_Menu passes to the Web_Search_Toggle. */
export interface WebSearchToggleSlotProps {
  /** The chat session whose `webSearch` value the toggle reads and writes; null before one exists. */
  sessionId: string | null;
  /** True while the chat session is locked. */
  locked: boolean;
}

/**
 * Props the composer passes to the Upload_Sheet. The sheet lists the session's chosen files that are not
 * attachment chips yet (from the chat attachment store, by `sessionId`) and carries the `Add to <project>` switch.
 */
export interface UploadSheetProps {
  /** The chat session whose attachments the sheet lists. */
  sessionId: string;
  /** The Active_Project the switch names; null when Projects are off or it is not known (no switch then). */
  projectId: string | null;
  projectName: string | null;
  /** The switch: ON adds chosen files to the project as well as the chat; OFF is "this chat only". */
  addToProject: boolean;
  locked: boolean;
  onAddToProjectChange: (on: boolean) => void;
  /** Dismiss the failed and cancelled files (closing the sheet when nothing is in progress). */
  onClose: () => void;
  /** Opens the Projects_Surface on `projectId` (ingestion Req 1.29). */
  onViewInProjects: (projectId: string) => void;
}

type Listener = () => void;

function createSlot<P>() {
  let current: React.ComponentType<P> | null = null;
  const listeners = new Set<Listener>();
  const emit = () => listeners.forEach((l) => l());
  return {
    register(component: React.ComponentType<P>): () => void {
      current = component;
      emit();
      return () => {
        if (current === component) {
          current = null;
          emit();
        }
      };
    },
    get(): React.ComponentType<P> | null {
      return current;
    },
    subscribe(listener: Listener): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

const webSearchToggleSlot = createSlot<WebSearchToggleSlotProps>();
const uploadSheetSlot = createSlot<UploadSheetProps>();

/** Register the Web_Search_Toggle shown in the Plus_Menu. Returns an unregister function. */
export function registerWebSearchToggleSlot(component: React.ComponentType<WebSearchToggleSlotProps>): () => void {
  return webSearchToggleSlot.register(component);
}

/** Register the Upload_Sheet ChatCanvas mounts above the composer. Returns an unregister function. */
export function registerUploadSheet(component: React.ComponentType<UploadSheetProps>): () => void {
  return uploadSheetSlot.register(component);
}

/** The registered Web_Search_Toggle, or null; re-renders the caller when the registration changes. */
export function useWebSearchToggleSlot(): React.ComponentType<WebSearchToggleSlotProps> | null {
  return useSyncExternalStore(webSearchToggleSlot.subscribe, webSearchToggleSlot.get, webSearchToggleSlot.get);
}

/** The registered Upload_Sheet, or null; re-renders the caller when the registration changes. */
export function useUploadSheet(): React.ComponentType<UploadSheetProps> | null {
  return useSyncExternalStore(uploadSheetSlot.subscribe, uploadSheetSlot.get, uploadSheetSlot.get);
}

/** Renders the registered Web_Search_Toggle, or nothing. */
export function WebSearchToggleSlot(props: WebSearchToggleSlotProps): React.ReactElement | null {
  const Component = useWebSearchToggleSlot();
  return Component ? React.createElement(Component, props) : null;
}

/**
 * The Upload_Sheet mount point. ChatCanvas renders it above the composer while the session has chosen files
 * that are not attachment chips yet. Renders nothing when no sheet is registered.
 */
export function UploadSheetMount(props: UploadSheetProps): React.ReactElement | null {
  const Component = useUploadSheet();
  return Component ? React.createElement(Component, props) : null;
}
