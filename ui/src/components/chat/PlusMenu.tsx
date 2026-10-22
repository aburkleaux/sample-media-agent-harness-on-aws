/**
 * Plus_Menu (media-harness-projects-access task 13.2; design Component 4; Req 2.7, 3.2–3.5, 3.12, 3.15, 3.23,
 * 3.24).
 *
 * The `+` button beside the chat input and the menu it opens. Presentational: ChatCanvas wires every read and
 * write. Sections, in this order:
 *
 *   1. `Attach files` — opens the chat's multi-file picker (1 to 20 files). Each file uploads at once, into the
 *      Active_Project unless the upload sheet's switch says "this chat only", and goes with the next message.
 *   2. `Recent in <Active_Project>` — the project's newest assets (`RecentAssets`). Picking one attaches it to
 *      the next message without uploading it again (a `chat-copy` into this session). `View all` opens the
 *      Projects_Surface on the project.
 *   3. Web search — the Web_Search_Toggle registered in `composerSlots` (Req 3.4), beside a globe. Omitted when no
 *      toggle is registered.
 *   4. `Project` — the project radios and `Manage projects` (`ProjectPicker`): the one place in the chat a
 *      session's project is changed, with the open-time access read and the reset to Personal. Once the session has
 *      a turn (`projectFixed`) its project is fixed (asset-lineage D1): picking another project starts a NEW chat
 *      in it (`onStartNewChatInProject`, by default the host's `projects:new-chat` event) instead.
 *
 * Sections 2 and 4 need Projects: with `projects` null (Projects off, or the Active_Project not labelled yet) the
 * menu holds only `Attach files` and the toggle. In a locked session `Attach files` and the recent files are
 * `aria-disabled` with the reason `Session locked` (Req 3.23). There is deliberately no create, rename, invite, or
 * delete control; those live on the Projects_Surface.
 *
 * Keyboard (Req 3.24): one roving focus across every section via {@link useRovingMenu}; Escape closes and
 * refocuses `+`. Items inside the web-search slot take part through `[data-plus-menu-slot]`.
 */
import React, { useCallback, useId, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import PaperclipIcon from '../icons/PaperclipIcon';
import GlobeIcon from '../icons/GlobeIcon';
import { WebSearchToggleSlot, useWebSearchToggleSlot } from './composerSlots';
import { useRovingMenu } from '../ui/useRovingMenu';
import {
  ProjectPickerSection,
  useOpenTimeProjects,
  type ActiveProjectNotFound,
  type PickerListResult,
} from './ProjectPicker';
import { RecentAssetsSection, useAssetSearch, useRecentAssets, type RecentAssetsResult } from './RecentAssets';
import type { AssetView } from '../../projects/api';
import { requestNewChatInProject } from '../../projects/newChatInProject';
import { iconColumnStyle, itemNameStyle, metaStyle, rowStyle, separatorStyle } from './plusMenuStyles';

/** Everything the Recent and Project sections need. Null when Projects are off or not labelled yet. */
export interface PlusMenuProjects {
  /** The session's Active_Project. */
  activeProjectId: string;
  activeProjectName: string;
  /** Runs on every open; the list it returns decides `aria-checked` and the not-found check. */
  loadProjects: () => Promise<PickerListResult>;
  /** Select a Project as the session's Active_Project. The caller confirms access before recording it. */
  onSelectProject: (projectId: string) => void;
  /** The open-time read lacks the Active_Project: switch to Personal and show the notice. */
  onActiveProjectNotFound: (lost: ActiveProjectNotFound) => void;
  /** Open the Projects_Surface on a Project (`Manage projects`, `View all`). Called after the menu closes. */
  onManageProjects: (projectId: string) => void;
  /** The first page of the project's assets, newest first (`GET /projects/:pid/assets`). */
  loadRecentAssets: (projectId: string) => Promise<RecentAssetsResult>;
  /** Attach one of the project's assets to the next message. Called after the menu closes. */
  onAttachRecentAsset: (asset: AssetView, project: { id: string; name: string }) => void;
  /** Filename search of the project (`GET /projects/:pid/assets?q=`). Absent → no search box. */
  searchAssets?: (projectId: string, q: string) => Promise<RecentAssetsResult>;
  /** The session has a turn, so its project is fixed (D1): another project opens a new chat instead. */
  projectFixed?: boolean;
  /** Open a new chat in a project. Called after the menu closes. Defaults to {@link requestNewChatInProject}. */
  onStartNewChatInProject?: (projectId: string, projectName: string) => void;
}

export interface PlusMenuProps {
  /** Passed to the web-search slot so the toggle can read the session's value. */
  sessionId: string | null;
  locked: boolean;
  /** Open the chat's multi-file picker (Req 3.15). Called after the menu closes. */
  onAttachToChat: () => void;
  projects?: PlusMenuProjects | null;
}

export const SESSION_LOCKED_REASON = 'Session locked';

/** The `+` button's accessible name, and the menu's. */
export const PLUS_MENU_LABEL = 'Attach and tools';

const ITEM_SELECTOR = [
  '[role="menuitem"]',
  '[role="menuitemradio"]',
  '[role="menuitemcheckbox"]',
  '[data-plus-menu-slot] button',
  '[data-plus-menu-slot] input',
  '[data-plus-menu-slot] [tabindex]',
].join(', ');

export default function PlusMenu({ sessionId, locked, onAttachToChat, projects = null }: PlusMenuProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const recentHeadingId = `${menuId}-recent`;
  const projectHeadingId = `${menuId}-project`;
  const WebSearchSlot = useWebSearchToggleSlot();

  const picker = useOpenTimeProjects(open, projects);
  const activeId = projects?.activeProjectId || null;
  const recent = useRecentAssets(open, activeId, projects?.loadRecentAssets ?? null);
  const [assetQuery, setAssetQuery] = useState('');
  const found = useAssetSearch(open, activeId, assetQuery, projects?.searchAssets ?? null);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    setAssetQuery('');
    if (refocus) buttonRef.current?.focus();
  }, []);

  const { menuRef, highlight, onKeyDown, onFocusCapture, hoverFocus } = useRovingMenu({
    open,
    itemSelector: ITEM_SELECTOR,
    onClose: close,
    resetKey: picker.projects,
  });

  const attach = () => {
    if (locked) return;
    close(true);
    onAttachToChat();
  };
  const attachRecent = (asset: AssetView) => {
    if (locked || !projects) return;
    const project = { id: projects.activeProjectId, name: projects.activeProjectName };
    close(true);
    projects.onAttachRecentAsset(asset, project);
  };
  const openProject = () => {
    if (!projects) return;
    close(false);
    projects.onManageProjects(projects.activeProjectId);
  };
  const select = (projectId: string) => {
    if (!projects) return;
    close(true);
    if (projectId === projects.activeProjectId) return;
    if (projects.projectFixed) {
      const name = picker.projects?.find((p) => p.projectId === projectId)?.name ?? '';
      (projects.onStartNewChatInProject ?? requestNewChatInProject)(projectId, name);
      return;
    }
    projects.onSelectProject(projectId);
  };

  return (
    <div style={wrapperStyle}>
      <button
        ref={buttonRef}
        type="button"
        aria-label={PLUS_MENU_LABEL}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title={PLUS_MENU_LABEL}
        style={open ? { ...plusButtonStyle, ...plusButtonOpenStyle } : plusButtonStyle}
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        <span aria-hidden="true">+</span>
      </button>

      {open && (
        <>
          <div style={scrimStyle} aria-hidden="true" data-testid="plus-menu-scrim" onClick={() => close(false)} />
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label={PLUS_MENU_LABEL}
            style={menuStyle}
            onKeyDown={onKeyDown}
            onFocusCapture={onFocusCapture}
          >
            <button
              type="button"
              role="menuitem"
              aria-disabled={locked || undefined}
              data-menu-key="attach"
              title={locked ? SESSION_LOCKED_REASON : undefined}
              style={rowStyle(highlight, 'attach', locked)}
              onMouseEnter={hoverFocus}
              onClick={attach}
            >
              <span style={iconColumnStyle}><PaperclipIcon /></span>
              <span style={itemNameStyle}>Attach files</span>
              {locked && <span style={metaStyle}>{SESSION_LOCKED_REASON}</span>}
            </button>

            {projects && (
              <>
                <div role="separator" style={separatorStyle} />
                <RecentAssetsSection
                  headingId={recentHeadingId}
                  projectName={projects.activeProjectName}
                  state={recent}
                  locked={locked}
                  lockedReason={SESSION_LOCKED_REASON}
                  highlight={highlight}
                  onHover={hoverFocus}
                  onAttach={attachRecent}
                  onViewAll={openProject}
                  search={projects.searchAssets ? { query: assetQuery, onQuery: setAssetQuery, state: found } : null}
                />
              </>
            )}

            {WebSearchSlot && (
              <>
                {projects && <div role="separator" style={separatorStyle} />}
                <div style={slotRowStyle} data-plus-menu-slot="web-search">
                  <span style={iconColumnStyle}><GlobeIcon /></span>
                  <WebSearchToggleSlot sessionId={sessionId} locked={locked} />
                </div>
              </>
            )}

            {projects && (
              <>
                <div role="separator" style={separatorStyle} />
                <ProjectPickerSection
                  headingId={projectHeadingId}
                  activeProjectId={projects.activeProjectId}
                  fixed={projects.projectFixed === true}
                  activeProjectName={projects.activeProjectName}
                  loadState={picker.loadState}
                  projects={picker.projects}
                  highlight={highlight}
                  onHover={hoverFocus}
                  onSelect={select}
                  onManage={openProject}
                />
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ styles

const wrapperStyle: React.CSSProperties = {
  position: 'relative',
  flexShrink: 0,
};

/** Same 40px footprint as the composer's other controls; round. */
const plusButtonStyle: React.CSSProperties = {
  width: 40,
  height: 40,
  boxSizing: 'border-box',
  borderRadius: theme.radius.pill,
  // Longhands, not `border`: the open style overrides `borderColor`, and React warns when a
  // shorthand and its longhand change on the same element.
  borderWidth: 1,
  borderStyle: 'solid',
  borderColor: theme.colors.borderStrong,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  fontSize: 'var(--fs-xl)',
  lineHeight: 1,
  cursor: 'pointer',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontFamily: 'inherit',
  padding: 0,
};

const plusButtonOpenStyle: React.CSSProperties = {
  background: theme.colors.activeBg,
  borderColor: theme.colors.activeBorder,
  color: theme.colors.primary,
};

const scrimStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  zIndex: theme.zIndex.popover,
};

/** Opens upward from the composer; scrolls when the project list is long. */
const menuStyle: React.CSSProperties = {
  position: 'absolute',
  bottom: 'calc(100% + 8px)',
  left: 0,
  zIndex: theme.zIndex.popover + 1,
  width: 300,
  maxHeight: '60vh',
  overflowY: 'auto',
  boxSizing: 'border-box',
  background: theme.colors.surfaceRaised,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
  padding: 6,
  fontFamily: theme.typography.fontFamily,
  fontSize: theme.typography.sizes.body,
  color: theme.colors.textPrimary,
  whiteSpace: 'normal',
};

const slotRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '6px 8px',
};
