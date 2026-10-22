/**
 * The Selected_Project's own header on the Projects_Surface (media-harness-projects-access; Req 1.2, 1.8,
 * 1.15, 1.22, 10.8, 10.9).
 *
 * Sits one level under the `Projects` surface header, with the same metrics as the Agents and Skills
 * detail headers (`surfaceNestedHeaderStyle`): the Project's name, a lock for Personal, the caller's role,
 * then the actions —
 *
 *   * `Upload` / `S3 location` — only when project ingestion has registered that slot (`tabSlots`).
 *   * `Chat in project` — on every Project: opens a NEW chat session working in this Project. It never changes
 *     the Active_Project of a chat already open (that is the Plus_Menu's job).
 *   * An overflow menu, by role:
 *       Owner of a non-Personal Project → `Rename` (inline field) and `Delete project` (typed-name
 *       {@link ConfirmDialog}, Req 1.22); Member → `Leave project` ({@link LeaveProjectDialog});
 *       Personal → no menu at all, since nothing about it can change (Req 1.8).
 *
 * Every denial is the uniform not-found, which triggers {@link ProjectHeaderProps.onNotFound}.
 */
import SearchIndexedGlyph from './SearchIndexedGlyph';
import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import ConfirmDialog from '../ConfirmDialog';
import { surfaceNestedHeaderStyle } from '../SurfaceHeader';
import { useRovingMenu } from '../ui/useRovingMenu';
import FolderIcon from '../icons/FolderIcon';
import LockIcon from '../icons/LockIcon';
import MoreIcon from '../icons/MoreIcon';
import UploadIcon from '../icons/UploadIcon';
import { LeaveProjectDialog } from './MembersTab';
import { deleteProject, leaveProject, renameProject, type ProjectsResult } from '../../projects/api';

export const PERSONAL_NOTICE = 'The Personal project is private and cannot be shared, renamed, deleted, or left.';
export const MEMBER_NOTICE = 'Only the owner can rename or delete this project.';
export const OWNER_NOTICE = 'You own this project.';

/** The overflow menu's trigger name. */
export const PROJECT_ACTIONS_LABEL = 'Project actions';

export type ProjectPanelKey = 'upload' | 's3-location';

export interface ProjectHeaderProps {
  project: { id: string; name: string; personal: boolean; activeAssetCount?: number; indexing?: boolean };
  role: 'owner' | 'member';
  /** Start a new chat session in this Project. Absent → no `New chat` button. */
  onNewChat?(): void;
  /** Registered ingestion slots, in toolbar order. An absent key renders no button. */
  panels: ProjectPanelKey[];
  activePanel: ProjectPanelKey | null;
  onTogglePanel(key: ProjectPanelKey): void;
  onNotFound(): void;
  /** After a rename (with the new name), a delete, or a leave. */
  onChanged(): void;
  /** Report a non-not-found failure to show in the body. */
  onError(message: string | null): void;
  /** The Settings page is showing (the button reads pressed). */
  settingsOpen?: boolean;
  /** Show or hide the Settings page. Absent → no Settings button. */
  onToggleSettings?(): void;
}

const PANEL_LABEL: Record<ProjectPanelKey, string> = { upload: 'Upload', 's3-location': 'S3 location' };

type MenuAction = 'rename' | 'delete' | 'leave';

export default function ProjectHeader({
  project,
  role,
  onNewChat,
  panels,
  activePanel,
  onTogglePanel,
  onNotFound,
  onChanged,
  onError,
  settingsOpen,
  onToggleSettings,
}: ProjectHeaderProps) {
  const isOwner = role === 'owner';
  const actions: MenuAction[] = project.personal ? [] : isOwner ? ['rename', 'delete'] : ['leave'];

  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draftName, setDraftName] = useState(project.name);
  const [confirm, setConfirm] = useState<'delete' | 'leave' | null>(null);
  const [busy, setBusy] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  // A different Project, or a rename that landed, resets the inline field.
  useEffect(() => {
    setRenaming(false);
    setDraftName(project.name);
    setConfirm(null);
    setMenuOpen(false);
  }, [project.id, project.name]);

  const closeMenu = useCallback((refocus: boolean) => {
    setMenuOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);
  const { menuRef, highlight, onKeyDown, onFocusCapture, hoverFocus } = useRovingMenu({
    open: menuOpen,
    itemSelector: '[role="menuitem"]',
    onClose: closeMenu,
  });

  const settle = useCallback(
    <T,>(r: ProjectsResult<T>): T | null => {
      if (r.ok) {
        onError(null);
        return r.value;
      }
      if (r.kind === 'not_found') onNotFound();
      else if (r.kind === 'error') onError(r.message);
      return null;
    },
    [onNotFound, onError],
  );

  const choose = (action: MenuAction) => {
    closeMenu(false);
    if (action === 'rename') {
      setDraftName(project.name);
      setRenaming(true);
    } else {
      setConfirm(action);
    }
  };

  const submitRename = async () => {
    const next = draftName.trim();
    if (!next || busy) return;
    if (next === project.name) {
      setRenaming(false);
      return;
    }
    setBusy(true);
    const r = await renameProject(project.id, next);
    setBusy(false);
    if (settle(r)) {
      setRenaming(false);
      onChanged();
    }
  };

  const onDelete = async () => {
    setBusy(true);
    const r = await deleteProject(project.id, project.name);
    setBusy(false);
    setConfirm(null);
    if (settle(r)) onChanged();
  };

  const onLeave = async () => {
    setBusy(true);
    const r = await leaveProject(project.id);
    setBusy(false);
    setConfirm(null);
    if (r.ok) {
      // The Member no longer has access: the surface drops it and falls back to Personal.
      onChanged();
      onNotFound();
    } else if (r.kind === 'not_found') {
      onNotFound();
    } else if (r.kind === 'error') {
      onError(r.message);
    }
  };

  const description = project.personal ? PERSONAL_NOTICE : isOwner ? OWNER_NOTICE : MEMBER_NOTICE;
  const count = project.activeAssetCount;
  const countText =
    typeof count === 'number' ? ` It currently has ${count} active asset${count === 1 ? '' : 's'}.` : '';

  return (
    <div style={surfaceNestedHeaderStyle} data-testid="project-header">
      <div style={{ minWidth: 0, flex: 1 }}>
        {renaming ? (
          <form
            style={renameRowStyle}
            onSubmit={(e) => { e.preventDefault(); void submitRename(); }}
          >
            <input
              style={renameInputStyle}
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') setRenaming(false); }}
              aria-label="Project name"
              autoFocus
              disabled={busy}
            />
            <Button type="submit" variant="primary" size="md" disabled={busy || !draftName.trim()}>Save</Button>
            <Button type="button" variant="ghost" size="md" onClick={() => setRenaming(false)} disabled={busy}>
              Cancel
            </Button>
          </form>
        ) : (
          <div style={titleRowStyle}>
            <span style={titleIconStyle}>
              {project.personal ? <LockIcon size={18} /> : <FolderIcon size={18} />}
            </span>
            <h2 style={titleStyle} title={project.name}>{project.name}</h2>
            <SearchIndexedGlyph indexing={project.indexing} size={16} color={theme.colors.textSecondary} />
            {project.personal && <Badge title={description}>private</Badge>}
            {/* What the role allows lives in the badge's tooltip, not in a sentence under every title. */}
            {!project.personal && !isOwner && <Badge title={description}>{role}</Badge>}
          </div>
        )}
      </div>

      <div style={actionsStyle}>
        {panels.map((key) => (
          <Button
            key={key}
            variant="secondary"
            size="md"
            aria-pressed={activePanel === key}
            onClick={() => onTogglePanel(key)}
          >
            {key === 'upload' && <UploadIcon size={14} style={{ marginRight: 6 }} />}
            {PANEL_LABEL[key]}
          </Button>
        ))}
        {onToggleSettings && (
          <Button variant="secondary" size="md" aria-pressed={!!settingsOpen} onClick={onToggleSettings}
            data-testid="project-settings-button">
            Settings
          </Button>
        )}
        {onNewChat && (
          <Button variant="primary" size="md" onClick={onNewChat} aria-label={`Chat in ${project.name}`}
            title="A new chat that works with this project's files and files what it makes here"
            data-testid="project-new-chat">
            Chat in project
          </Button>
        )}
        {actions.length > 0 && (
          <span style={{ position: 'relative', display: 'inline-flex' }}>
            <button
              ref={triggerRef}
              type="button"
              aria-label={PROJECT_ACTIONS_LABEL}
              title={PROJECT_ACTIONS_LABEL}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-controls={menuOpen ? menuId : undefined}
              style={menuOpen ? { ...moreBtnStyle, ...moreBtnOpenStyle } : moreBtnStyle}
              onClick={() => (menuOpen ? closeMenu(false) : setMenuOpen(true))}
              disabled={busy}
            >
              <MoreIcon />
            </button>
            {menuOpen && (
              <>
                <span style={scrimStyle} aria-hidden="true" onClick={() => closeMenu(false)} />
                <div
                  ref={menuRef}
                  id={menuId}
                  role="menu"
                  aria-label={PROJECT_ACTIONS_LABEL}
                  style={menuStyle}
                  onKeyDown={onKeyDown}
                  onFocusCapture={onFocusCapture}
                >
                  {actions.map((a) => (
                    <button
                      key={a}
                      type="button"
                      role="menuitem"
                      data-menu-key={a}
                      style={{
                        ...menuItemStyle,
                        ...(a !== 'rename' ? { color: theme.colors.error } : null),
                        ...(highlight === a ? { background: theme.colors.activeBg } : null),
                      }}
                      onMouseEnter={hoverFocus}
                      onClick={() => choose(a)}
                    >
                      {a === 'rename' ? 'Rename' : a === 'delete' ? 'Delete project' : 'Leave project'}
                    </button>
                  ))}
                </div>
              </>
            )}
          </span>
        )}
      </div>

      <ConfirmDialog
        open={confirm === 'delete'}
        title="Delete project"
        message={`Delete "${project.name}"? This cannot be undone.${countText}`}
        confirmLabel="Delete project"
        requiredPhrase={project.name}
        loading={busy}
        onConfirm={onDelete}
        onCancel={() => setConfirm(null)}
      />
      <LeaveProjectDialog
        open={confirm === 'leave'}
        projectName={project.name}
        loading={busy}
        onConfirm={onLeave}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ styles

const titleRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 };
const titleIconStyle: React.CSSProperties = { display: 'inline-flex', color: theme.colors.textSecondary };
// Matches the Agents published-detail title (`caDetailTitleStyle`).
const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-xl)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
  // One line: a long name ellipsizes (its full text is the tooltip) instead of wrapping beside the actions.
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  minWidth: 0,
};
const actionsStyle: React.CSSProperties = { display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 };
const renameRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, maxWidth: 560 };
const renameInputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  padding: '6px 8px',
  fontSize: 'var(--fs-md)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  fontFamily: 'inherit',
};
const moreBtnStyle: React.CSSProperties = {
  width: 32,
  height: 32,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 0,
  borderWidth: 1,
  borderStyle: 'solid',
  borderColor: theme.colors.border,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.textSecondary,
  cursor: 'pointer',
};
const moreBtnOpenStyle: React.CSSProperties = {
  background: theme.colors.activeBg,
  borderColor: theme.colors.activeBorder,
  color: theme.colors.primary,
};
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const menuStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 4px)',
  right: 0,
  zIndex: theme.zIndex.popover + 1,
  minWidth: 180,
  boxSizing: 'border-box',
  background: theme.colors.surfaceRaised,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
  padding: 6,
  fontFamily: theme.typography.fontFamily,
  fontSize: theme.typography.sizes.body,
};
const menuItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  width: '100%',
  padding: '6px 8px',
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  textAlign: 'left',
  cursor: 'pointer',
  color: theme.colors.textPrimary,
  fontFamily: 'inherit',
  fontSize: 'inherit',
};
