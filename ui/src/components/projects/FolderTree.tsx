/**
 * The Assets surface's folder tree: All files, Top level (the files in no folder), the user's folders, then
 * `Chats` (where every chat's outputs are filed) last and muted. Clicking a folder shows the files filed directly
 * in it; a folder's count is its whole subtree. Each row is a drop target for asset cards dragged from the grid
 * (`ASSET_DRAG_TYPE`, never a file drag — those still go to upload), Top level included.
 *
 * A folder's ⋯ (on hover, always on the open one) holds New folder inside · Rename · Delete folder. New folder at
 * the bottom makes one WHERE YOU ARE (inside the open folder, else at the top level) and says so. Names are checked
 * as you type (`folderNameProblem`), so a bad one is explained, never sent. Rename is in place (Enter saves, Escape
 * cancels; double-click works too). Chat folders can be opened, not renamed or deleted.
 */
import React, { useEffect, useId, useRef, useState } from 'react';
import { cleanFolder, folderName, folderNameProblem, isWithin, parentFolder, type FolderNode } from '../../projects/folderPaths';
import { theme } from '../../styles/tokens';
import ChevronDownIcon from '../icons/ChevronDownIcon';
import FolderIcon from '../icons/FolderIcon';
import { draggedAssetIds, isAssetDrag } from './assetDrag';
import { isSessionFolder } from './assetSelection';

export interface FolderTreeProps {
  tree: readonly FolderNode[];
  /** The selected folder path, `allValue` for All files, or `topValue` for Top level. */
  selected: string;
  allValue: string;
  /** Files in the whole project (the All files count). */
  totalCount?: number;
  /** Present → a Top level row (the files in no folder) with this value; `rootCount` is its count. */
  topValue?: string;
  rootCount?: number;
  onSelect: (folder: string) => void;
  onDropAssets: (folder: string, assetIds: string[]) => void;
  onCreate: (folder: string) => void;
  onRename: (from: string, to: string) => void;
  /** Delete a folder (the section confirms and says where its files go). Absent → not offered. */
  onDelete?: (folder: string) => void;
  /** Nothing that changes the project (no drop, create, rename). */
  readOnly?: boolean;
  /** In a popover (a narrow pane): no side border, not sticky. */
  compact?: boolean;
  /** Docked: hide the list (the Folder button then opens it). */
  onCollapse?: () => void;
  /** In the popover with room to dock: keep the list open beside the files again. */
  onPin?: () => void;
}

export function FolderTree(props: FolderTreeProps) {
  const { tree, selected, allValue, totalCount, onSelect, readOnly, topValue } = props;
  // Where a new folder is being typed: the parent path (`''` = the top level), or null.
  const [creating, setCreating] = useState<string | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [hoverRow, setHoverRow] = useState<string | null>(null);
  const [menuUp, setMenuUp] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  // Folders start open except `Chats/…`; the selected folder's ancestors always open.
  const [toggled, setToggled] = useState<ReadonlySet<string>>(() => new Set());
  const isOpen = (path: string) => {
    if (selected !== allValue && selected !== path && isWithin(selected, path)) return true;
    return isSessionFolder(path) ? toggled.has(path) : !toggled.has(path);
  };
  const toggle = (path: string) => setToggled((cur) => {
    const next = new Set(cur);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
  const userRoots = tree.filter((n) => !isSessionFolder(n.path));
  // New folder makes one where you are: inside the open folder (not a chat folder), else at the top level.
  const here = selected !== allValue && selected !== topValue && !isSessionFolder(selected) ? selected : '';
  const startCreate = (parent: string) => {
    setMenuFor(null);
    setCreating(parent);
    if (parent) setToggled((cur) => { const next = new Set(cur); next.delete(parent); return next; });
  };
  const createInput = (parent: string, depth: number) => (
    <li key="__new"><NameInput initial="" label={parent ? `New folder in ${folderName(parent)}` : 'New folder name'} indent={6 + depth * 14}
      check={(v) => folderNameProblem(v, parent)}
      onDone={(name) => {
        setCreating(null);
        const clean = cleanFolder(name);
        if (clean && !folderNameProblem(clean, parent)) props.onCreate(parent ? `${parent}/${clean}` : clean);
      }} /></li>
  );
  const chatRoots = tree.filter((n) => isSessionFolder(n.path));

  const dropProps = (path: string) => (readOnly ? {} : {
    onDragOver: (e: React.DragEvent) => {
      if (!isAssetDrag(e.dataTransfer)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (dropTarget !== path) setDropTarget(path);
    },
    onDragLeave: () => setDropTarget((cur) => (cur === path ? null : cur)),
    onDrop: (e: React.DragEvent) => {
      const ids = draggedAssetIds(e.dataTransfer);
      setDropTarget(null);
      if (!ids) return;
      e.preventDefault();
      e.stopPropagation();
      if (ids.length) props.onDropAssets(path, ids);
    },
  });

  const row = (n: FolderNode, depth: number): React.ReactNode => {
    const open = isOpen(n.path);
    const on = selected === n.path;
    const style: React.CSSProperties = {
      ...rowStyle,
      paddingLeft: 6 + depth * 14,
      ...(on ? rowOnStyle : null),
      ...(dropTarget === n.path ? rowDropStyle : null),
      ...(isSessionFolder(n.path) && !on ? mutedStyle : null),
    };
    return (
      <li key={n.path} role="treeitem" aria-selected={on} aria-expanded={n.children.length ? open : undefined}
        aria-label={`${n.name}, ${n.total} file${n.total === 1 ? '' : 's'}`} data-testid="folder-tree-node" data-folder={n.path}>
        {renaming === n.path ? (
          <NameInput initial={n.name} label={`Rename ${n.name}`} indent={6 + depth * 14}
            check={(v) => folderNameProblem(v, parentFolder(n.path))}
            onDone={(name) => {
              setRenaming(null);
              const clean = cleanFolder(name);
              if (!clean || folderNameProblem(clean, parentFolder(n.path))) return;
              const parent = parentFolder(n.path);
              const to = parent ? `${parent}/${clean}` : clean;
              if (to !== n.path) props.onRename(n.path, to);
            }} />
        ) : (
          <div style={style} {...dropProps(n.path)} data-folder-row=""
            onMouseEnter={() => setHoverRow(n.path)} onMouseLeave={() => setHoverRow((c) => (c === n.path ? null : c))}
            onFocus={() => setHoverRow(n.path)}>
            <button type="button" style={chevronBtnStyle} tabIndex={-1} aria-hidden="true"
              onClick={() => toggle(n.path)} disabled={!n.children.length}>
              {n.children.length > 0 && (
                <span style={open ? chevronStyle : { ...chevronStyle, transform: 'rotate(-90deg)' }}><ChevronDownIcon size={11} /></span>
              )}
            </button>
            <button type="button" style={nameBtnStyle} onClick={() => onSelect(n.path)}
              onDoubleClick={() => { if (!readOnly && !isSessionFolder(n.path)) setRenaming(n.path); }}
              onKeyDown={(e) => {
                // Shortcuts for a focused folder, nothing on screen: F2 renames, Delete asks to delete it.
                if (readOnly || isSessionFolder(n.path)) return;
                if (e.key === 'F2') { e.preventDefault(); setRenaming(n.path); }
                else if ((e.key === 'Delete' || e.key === 'Backspace') && props.onDelete) { e.preventDefault(); props.onDelete(n.path); }
              }}
              title={readOnly || isSessionFolder(n.path) ? n.path : `${n.path} — double-click or F2 to rename`}>
              <FolderIcon size={14} />
              <span style={nameStyle}>{n.name}</span>
              {n.total > 0 && <span style={countStyle}>{n.total.toLocaleString('en-US')}</span>}
            </button>
            {(readOnly || isSessionFolder(n.path)) && <span style={moreSlotStyle} aria-hidden="true" />}
            {!readOnly && !isSessionFolder(n.path) && (
              <span style={{ position: 'relative', display: 'inline-flex' }}>
                <button type="button" style={on || menuFor === n.path || hoverRow === n.path ? moreBtnStyle : { ...moreBtnStyle, opacity: 0 }}
                  data-folder-more="" aria-label={`Actions for folder ${n.name}`} aria-haspopup="menu"
                  aria-expanded={menuFor === n.path} title="Folder actions"
                  onClick={(e) => {
                    // Near the bottom of the tree, the menu opens upward so the tree's scroll box never clips it.
                    const nav = (e.currentTarget.closest('nav') as HTMLElement | null)?.getBoundingClientRect();
                    setMenuUp(!!nav && e.currentTarget.getBoundingClientRect().bottom + 120 > nav.bottom);
                    setMenuFor((cur) => (cur === n.path ? null : n.path));
                  }}>
                  <Dots />
                </button>
                {menuFor === n.path && (
                  <>
                    <span style={scrimStyle} aria-hidden="true" onMouseDown={() => setMenuFor(null)} />
                    <span role="menu" aria-label={`Folder ${n.name}`}
                      style={menuUp ? { ...menuStyle, top: 'auto', bottom: 'calc(100% + 2px)' } : menuStyle}
                      onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); setMenuFor(null); } }}>
                      <button type="button" role="menuitem" style={menuItemStyle} onClick={() => startCreate(n.path)}>New folder inside</button>
                      <button type="button" role="menuitem" style={menuItemStyle}
                        onClick={() => { setMenuFor(null); setRenaming(n.path); }}>Rename</button>
                      {props.onDelete && (
                        <button type="button" role="menuitem" style={{ ...menuItemStyle, color: theme.colors.error }}
                          onClick={() => { setMenuFor(null); props.onDelete!(n.path); }}>Delete folder…</button>
                      )}
                    </span>
                  </>
                )}
              </span>
            )}
          </div>
        )}
        {open && (n.children.length > 0 || creating === n.path) && (
          <ul role="group" style={groupStyle}>
            {creating === n.path && createInput(n.path, depth + 1)}
            {n.children.map((c) => row(c, depth + 1))}
          </ul>
        )}
      </li>
    );
  };

  return (
    <nav aria-label="Folders" style={props.compact ? compactNavStyle : navStyle} data-testid="folder-tree">
      {(props.onCollapse || props.onPin) && (
        <div style={treeHeadStyle}>
          <span>Folders</span>
          <button type="button" style={collapseBtnStyle} data-testid={props.onCollapse ? 'folder-tree-collapse' : 'folder-tree-pin'}
            onClick={() => (props.onCollapse ?? props.onPin)!()}
            aria-label={props.onCollapse ? 'Hide the folder list' : 'Keep the folder list open'}
            title={props.onCollapse ? 'Hide the folder list (the Folder button opens it)' : 'Keep the folder list open beside the files'}>
            {props.onCollapse ? '«' : '»'}
          </button>
        </div>
      )}
      <ul role="tree" aria-label="Folders" style={groupStyle}>
        <li role="treeitem" aria-selected={selected === allValue} data-testid="folder-tree-all">
          <div style={selected === allValue ? { ...rowStyle, ...rowOnStyle } : rowStyle}>
            <span style={chevronBtnStyle} />
            <button type="button" style={nameBtnStyle} onClick={() => onSelect(allValue)}>
              <FolderIcon size={14} />
              <span style={nameStyle}>All files</span>
              {typeof totalCount === 'number' && <span style={countStyle}>{totalCount.toLocaleString('en-US')}</span>}
            </button>
            {!readOnly && <span style={moreSlotStyle} aria-hidden="true" />}
          </div>
        </li>
        {topValue !== undefined && (
          <li role="treeitem" aria-selected={selected === topValue} data-testid="folder-tree-top"
            aria-label={`Top level, ${props.rootCount ?? 0} file${props.rootCount === 1 ? '' : 's'}`}>
            <div style={{ ...(selected === topValue ? { ...rowStyle, ...rowOnStyle } : rowStyle), ...(dropTarget === '' ? rowDropStyle : null) }}
              {...dropProps('')}>
              <span style={chevronBtnStyle} />
              <button type="button" style={nameBtnStyle} onClick={() => onSelect(topValue)}
                title="Files that are not in any folder">
                <FolderIcon size={14} />
                <span style={nameStyle}>Top level</span>
                {!!props.rootCount && <span style={countStyle}>{props.rootCount.toLocaleString('en-US')}</span>}
              </button>
              {!readOnly && <span style={moreSlotStyle} aria-hidden="true" />}
            </div>
          </li>
        )}
        {creating === '' && createInput('', 0)}
        {userRoots.map((n) => row(n, 0))}
        {chatRoots.length > 0 && <li aria-hidden="true" style={dividerStyle} />}
        {chatRoots.map((n) => row(n, 0))}
      </ul>
      {!readOnly && creating === null && (
        <button type="button" style={newBtnStyle} onClick={() => startCreate(here)} data-testid="folder-tree-new"
          title={here ? `A new folder inside ${here}` : 'A new folder at the top level'}>
          <span aria-hidden="true">+</span><span style={newInStyle}>{here ? `New folder in ${folderName(here)}` : 'New folder'}</span>
        </button>
      )}
    </nav>
  );
}

function NameInput({ initial, label, indent, onDone, check }: {
  initial: string; label: string; indent: number; onDone: (v: string) => void; check?: (v: string) => string | null;
}) {
  const [value, setValue] = useState(initial);
  const [shown, setShown] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  const errId = useId();
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, []);
  const finish = (v: string) => { if (!done.current) { done.current = true; onDone(v); } };
  // A problem shows as you type; Enter on one keeps the field open; leaving (blur, Escape) cancels.
  const problem = value.trim() && check ? check(value) : null;
  return (
    <span style={{ display: 'block', marginLeft: indent, marginRight: 12 }}>
      <input ref={ref} value={value} aria-label={label} style={problem ? { ...nameInputStyle, borderColor: theme.colors.error } : nameInputStyle}
        aria-invalid={problem ? true : undefined} aria-describedby={problem ? errId : undefined}
        onChange={(e) => { setValue(e.target.value); setShown(null); }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (problem) { setShown(problem); return; }
            finish(value);
          } else if (e.key === 'Escape') { e.preventDefault(); finish(initial); }
        }}
        onBlur={() => finish(problem ? initial : value)} placeholder="Folder name" data-testid="folder-tree-name-input" />
      {(problem || shown) && <span id={errId} role="alert" style={nameErrorStyle} data-testid="folder-name-error">{problem || shown}</span>}
    </span>
  );
}

function Dots() {
  return (
    <svg width={14} height={14} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.7" /><circle cx="12" cy="12" r="1.7" /><circle cx="19" cy="12" r="1.7" />
    </svg>
  );
}

// Pinned beside the grid while it scrolls (a drop target must stay reachable), scrolling on its own when long.
const navStyle: React.CSSProperties = {
  width: 220, flexShrink: 0, boxSizing: 'border-box', padding: '4px 8px 12px 0', borderRight: `1px solid ${theme.colors.border}`,
  overflowY: 'auto', position: 'sticky', top: 0, alignSelf: 'flex-start', maxHeight: 'calc(100vh - 160px)',
};
const treeHeadStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 4px 4px 8px',
  fontSize: 'var(--fs-xs)', fontWeight: 600, color: theme.colors.textTertiary, textTransform: 'uppercase', letterSpacing: '0.04em',
};
const collapseBtnStyle: React.CSSProperties = {
  width: 22, height: 22, display: 'grid', placeItems: 'center', padding: 0, border: 'none', background: 'none',
  borderRadius: theme.radius.button, color: theme.colors.textSecondary, cursor: 'pointer', fontSize: 'var(--fs-sm)',
};
const compactNavStyle: React.CSSProperties = {
  boxSizing: 'border-box', padding: '0 8px 0 0', maxHeight: '60vh', overflowY: 'auto',
};
const groupStyle: React.CSSProperties = { listStyle: 'none', margin: 0, padding: 0 };
const rowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 2, minHeight: 30, paddingRight: 4, borderRadius: theme.radius.button,
  color: theme.colors.textSecondary,
};
const rowOnStyle: React.CSSProperties = { background: theme.colors.activeBg, color: theme.colors.primary, fontWeight: 600 };
const rowDropStyle: React.CSSProperties = { outline: `2px dashed ${theme.colors.primary}`, outlineOffset: -2, background: theme.colors.activeBg };
const mutedStyle: React.CSSProperties = { color: theme.colors.textTertiary };
const chevronBtnStyle: React.CSSProperties = {
  width: 16, height: 16, flexShrink: 0, display: 'inline-grid', placeItems: 'center', padding: 0, border: 'none',
  background: 'none', color: 'inherit', cursor: 'pointer',
};
const chevronStyle: React.CSSProperties = { display: 'inline-flex', transition: 'transform 0.12s ease' };
const nameBtnStyle: React.CSSProperties = {
  flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 7, padding: '5px 4px', border: 'none',
  background: 'none', color: 'inherit', font: 'inherit', fontSize: 'var(--fs-sm)', textAlign: 'left', cursor: 'pointer',
};
const nameStyle: React.CSSProperties = { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };
const countStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary, fontWeight: 400 };
const moreBtnStyle: React.CSSProperties = {
  width: 22, height: 22, display: 'grid', placeItems: 'center', padding: 0, border: 'none', background: 'none',
  borderRadius: theme.radius.button, color: theme.colors.textSecondary, cursor: 'pointer',
};
const moreSlotStyle: React.CSSProperties = { width: 22, flexShrink: 0 };
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const menuStyle: React.CSSProperties = {
  position: 'absolute', right: 0, top: 'calc(100% + 2px)', zIndex: theme.zIndex.popover + 1, minWidth: 170, padding: 4,
  display: 'block', background: theme.colors.surfaceRaised, border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel, boxShadow: theme.elevation.elevated,
};
const menuItemStyle: React.CSSProperties = {
  display: 'block', width: '100%', padding: '6px 9px', border: 'none', background: 'none', textAlign: 'left',
  borderRadius: theme.radius.button, font: 'inherit', fontSize: 'var(--fs-sm)', fontWeight: 400,
  color: theme.colors.textPrimary, cursor: 'pointer',
};
const nameErrorStyle: React.CSSProperties = {
  display: 'block', marginTop: 3, fontSize: 'var(--fs-xs)', color: theme.colors.error, fontWeight: 400,
};
const newInStyle: React.CSSProperties = { maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };
const dividerStyle: React.CSSProperties = { height: 1, background: theme.colors.border, margin: '8px 4px' };
const newBtnStyle: React.CSSProperties = {
  marginTop: 8, width: '100%', height: 30, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button, background: theme.colors.surface,
  color: theme.colors.textPrimary, font: 'inherit', fontSize: 'var(--fs-sm)', fontWeight: 600, cursor: 'pointer',
};
const nameInputStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', height: 28, padding: '0 8px', font: 'inherit', fontSize: 'var(--fs-sm)',
  border: `1px solid ${theme.colors.primary}`, borderRadius: theme.radius.button, background: theme.colors.surface,
  color: theme.colors.textPrimary,
};
