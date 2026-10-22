/**
 * The Plus_Menu's `Project` section (media-harness-projects-access; design Component 4; Req 2.7, 3.2, 3.3, 3.5,
 * 3.12) — the one place in the chat a session's project is changed. Moved here from the status-line Project chip,
 * with the same behaviour:
 *
 *   * Every open reads the project list (`loadProjects`, i.e. `GET /projects`). Personal comes first with a lock,
 *     then the rest by `orderProjects`; each row shows the Actor's role; the Active_Project is `aria-checked`.
 *   * Picking a row calls `onSelectProject` — the caller confirms access before recording it (Req 3.3). Picking
 *     the already-active row changes nothing.
 *   * When the open-time read lacks the Active_Project, `onActiveProjectNotFound` reports it with the
 *     Personal_Project from the same read, so the caller can switch the session to Personal (Req 3.12).
 *   * `Manage projects` opens the Projects_Surface on the Active_Project (Req 3.5).
 *   * Once the session has a turn its project is fixed (asset-lineage D1, R2): `fixed` turns every other row into
 *     `Start a new chat in <project>` (a `menuitem`, not a radio), under a note naming the project this chat keeps.
 *
 * {@link useOpenTimeProjects} holds the read; {@link ProjectPickerSection} draws it. The Plus_Menu owns both, so
 * its roving focus covers these rows with the rest of the menu.
 */
import { NameWithIndexGlyph } from '../projects/SearchIndexedGlyph';
import React, { useEffect, useRef, useState } from 'react';
import Badge from '../ui/Badge';
import { orderProjects } from '../../projects/orderProjects';
import FolderIcon from '../icons/FolderIcon';
import LockIcon from '../icons/LockIcon';
import CheckIcon from '../icons/CheckIcon';
import SettingsIcon from '../icons/SettingsIcon';
import { checkColumnStyle, iconColumnStyle, itemNameStyle, metaStyle, noteStyle, rowStyle, sectionHeadingStyle } from './plusMenuStyles';

/** One Project row, field-compatible with a `GET /projects` row. */
export interface PickerProject {
  projectId: string;
  name: string;
  role: 'owner' | 'member';
  personal: boolean;
  /** Search indexing ON (the `/projects` list carries it); drives the magnifier mark. */
  indexing?: boolean;
}

/** The open-time list read. Structurally compatible with the `/projects` client's result. */
export type PickerListResult =
  | { ok: true; value: readonly PickerProject[] }
  | { ok: false };

/** What the picker reports when the open-time read does not include the Active_Project (Req 3.12). */
export interface ActiveProjectNotFound {
  /** The Active_Project the Actor can no longer access. */
  projectId: string;
  projectName: string;
  /** The Actor's Personal_Project from the same read, when it was returned. */
  personal: PickerProject | null;
}

/** The notice of Req 3.11/3.12, for callers that show it after `onActiveProjectNotFound`. */
export function lostAccessNotice(projectName: string): string {
  const name = projectName.trim() || 'the previously selected project';
  return `You no longer have access to ${name}. This session has switched to your Personal project.`;
}

export type PickerLoadState = 'loading' | 'ready' | 'error';

export interface OpenTimeProjectsOptions {
  activeProjectId: string;
  activeProjectName: string;
  /** Runs on every open; the list it returns decides `aria-checked` and the not-found check. */
  loadProjects: () => Promise<PickerListResult>;
  /** The open-time read lacks the Active_Project: switch to Personal and show the notice. */
  onActiveProjectNotFound: (lost: ActiveProjectNotFound) => void;
}

/**
 * The open-time list read (Req 3.2, 3.12): re-run each time `open` turns true; an answer that arrives after the
 * menu closed (or reopened) is dropped. `options` null (Projects off) reads nothing.
 */
export function useOpenTimeProjects(open: boolean, options: OpenTimeProjectsOptions | null) {
  const [loadState, setLoadState] = useState<PickerLoadState>('loading');
  const [projects, setProjects] = useState<PickerProject[] | null>(null);
  const requestRef = useRef(0);
  // Read at call time, so an inline `loadProjects` or a not-found handler that re-renders the parent cannot
  // re-trigger the read.
  const latest = useRef(options);
  latest.current = options;
  const enabled = !!options;

  useEffect(() => {
    if (!open || !enabled) return;
    const req = ++requestRef.current;
    setProjects(null);
    setLoadState('loading');
    let pending: Promise<PickerListResult>;
    try {
      pending = (latest.current as OpenTimeProjectsOptions).loadProjects();
    } catch {
      setLoadState('error');
      return;
    }
    pending
      .then((res) => {
        if (req !== requestRef.current) return;
        if (!res.ok) {
          setLoadState('error');
          return;
        }
        const ordered = orderProjects(res.value);
        setProjects(ordered);
        setLoadState('ready');
        const opts = latest.current;
        if (!opts) return;
        const { activeProjectId: active, activeProjectName: activeName, onActiveProjectNotFound: lost } = opts;
        if (active && !ordered.some((p) => p.projectId === active)) {
          lost({ projectId: active, projectName: activeName, personal: ordered.find((p) => p.personal) ?? null });
        }
      })
      .catch(() => {
        if (req === requestRef.current) setLoadState('error');
      });
    return () => {
      requestRef.current += 1;
    };
  }, [open, enabled]);

  return { loadState, projects };
}

/** The accessible name (and label) of a row that opens a new chat in `projectName`. */
export function newChatInProjectLabel(projectName: string): string {
  return `Start a new chat in ${projectName}`;
}

/** The note shown over the rows when the session's project is fixed. */
export function projectFixedNote(activeProjectName: string): string {
  return `This chat stays in ${activeProjectName || 'its project'}. Pick another project to start a new chat in it.`;
}

export interface ProjectPickerSectionProps {
  headingId: string;
  activeProjectId: string;
  /** The session has a turn: its project is fixed, and other rows start a new chat instead (D1). */
  fixed?: boolean;
  /** For the fixed note. */
  activeProjectName?: string;
  loadState: PickerLoadState;
  projects: readonly PickerProject[] | null;
  /** The `data-menu-key` of the row holding focus. */
  highlight: string | null;
  onHover: (e: React.MouseEvent<HTMLElement>) => void;
  onSelect: (projectId: string) => void;
  onManage: () => void;
}

export function ProjectPickerSection({
  headingId,
  activeProjectId,
  fixed = false,
  activeProjectName = '',
  loadState,
  projects,
  highlight,
  onHover,
  onSelect,
  onManage,
}: ProjectPickerSectionProps) {
  return (
    <div role="group" aria-labelledby={headingId} data-plus-menu-section="project">
      <div id={headingId} style={sectionHeadingStyle}>Project</div>
      {fixed && <div style={noteStyle} data-testid="project-fixed-note">{projectFixedNote(activeProjectName)}</div>}
      {loadState === 'loading' && <div style={noteStyle} role="status">Loading projects…</div>}
      {loadState === 'error' && <div style={noteStyle} role="status">Projects could not be loaded.</div>}
      {projects?.map((p) => {
        const key = `project:${p.projectId}`;
        const checked = p.projectId === activeProjectId;
        if (fixed && !checked) {
          return (
            <button
              key={p.projectId}
              type="button"
              role="menuitem"
              aria-label={newChatInProjectLabel(p.name)}
              data-menu-key={key}
              style={rowStyle(highlight, key)}
              onMouseEnter={onHover}
              onClick={() => onSelect(p.projectId)}
            >
              <span style={checkColumnStyle} />
              <span style={iconColumnStyle}>{p.personal ? <LockIcon size={14} /> : <FolderIcon size={14} />}</span>
              <NameWithIndexGlyph name={p.name} indexing={p.indexing} style={itemNameStyle} />
              <span style={metaStyle}>New chat</span>
            </button>
          );
        }
        return (
          <button
            key={p.projectId}
            type="button"
            role="menuitemradio"
            aria-checked={checked}
            aria-label={`${p.name}${p.personal ? ', private' : ''}, ${p.role}`}
            data-menu-key={key}
            style={rowStyle(highlight, key)}
            onMouseEnter={onHover}
            onClick={() => onSelect(p.projectId)}
          >
            <span style={checkColumnStyle}>{checked && <CheckIcon size={14} />}</span>
            <span style={iconColumnStyle}>{p.personal ? <LockIcon size={14} /> : <FolderIcon size={14} />}</span>
            <NameWithIndexGlyph name={p.name} indexing={p.indexing} style={itemNameStyle} />
            <Badge>{p.role}</Badge>
          </button>
        );
      })}
      <button
        type="button"
        role="menuitem"
        data-menu-key="manage"
        style={rowStyle(highlight, 'manage')}
        onMouseEnter={onHover}
        onClick={onManage}
      >
        <span style={checkColumnStyle} />
        <span style={iconColumnStyle}><SettingsIcon size={14} /></span>
        <span style={itemNameStyle}>Manage projects</span>
      </button>
    </div>
  );
}
