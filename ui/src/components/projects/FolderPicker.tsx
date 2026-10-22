/**
 * The ONE "where should this go?" popover: the Assets selection bar's Move to…, a chat preview's Move to folder,
 * and a Renderer tab's Save to folder all open it, so filing anything looks and works the same everywhere.
 *
 * Type to find a folder or name a new one ("Create “Boards/Board 07” and move"); a path with `/` creates every
 * level at once. Chat folders show only once you type. Arrow keys move through the options, Enter picks, Escape or a click outside closes. The caller
 * positions it (it is `position: absolute`, so the caller's wrapper is `position: relative`).
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { cleanFolder } from '../../projects/folderPaths';
import { theme } from '../../styles/tokens';
import FolderIcon from '../icons/FolderIcon';
import { isSessionFolder } from './assetSelection';

export interface FolderPickerProps {
  /** Every existing folder path, in display order. */
  folders: readonly string[];
  /** "Move 3 files to", "Save 3 items to". */
  title: string;
  /** The verb of the create option: `Create “X” and ${verb}`. */
  verb?: string;
  /** Hide a folder that is not a valid destination (the items' current folder). */
  current?: string;
  /** Where the item is now, shown as "From …" so you know what you are moving it out of (`""` = top level). */
  from?: string;
  /** One quiet line under the options ("1 moment is saved as a bookmark."). */
  note?: string;
  onPick: (folder: string) => void;
  onClose: () => void;
  /** Where the popover opens relative to its wrapper. */
  align?: 'left' | 'right';
}

interface Option {
  key: string;
  folder: string;
  label: string;
  create?: boolean;
}

const ROOT_LABEL = 'Top level';

export function FolderPicker({ folders, title, verb = 'move', current, from, note, onPick, onClose, align = 'left' }: FolderPickerProps) {
  const [text, setText] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);

  const options = useMemo<Option[]>(() => {
    const typed = cleanFolder(text);
    const needle = typed.toLocaleLowerCase();
    const out: Option[] = [];
    if (!needle && current !== '' && current !== undefined) out.push({ key: '\u0000root', folder: '', label: ROOT_LABEL });
    for (const f of folders) {
      if (f === current) continue;
      // Chat folders (one per chat, many) wait until you type: filing INTO one is rare, so the list starts short.
      if (!needle && isSessionFolder(f)) continue;
      if (needle && !f.toLocaleLowerCase().includes(needle)) continue;
      out.push({ key: f, folder: f, label: f.split('/').join(' / ') });
      if (out.length >= 60) break; // the list scrolls; a type-ahead narrows it
    }
    if (typed && !folders.some((f) => f.toLocaleLowerCase() === needle)) {
      out.push({ key: '\u0000create', folder: typed, label: `Create “${typed}” and ${verb}`, create: true });
    }
    return out;
  }, [text, folders, current, verb]);
  useEffect(() => { setActive(0); }, [text]);

  const pick = (o: Option | undefined) => { if (o) onPick(o.folder); };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setActive((i) => Math.min(i + 1, options.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); pick(options[active]); }
  };

  return (
    <>
      <span style={scrimStyle} aria-hidden="true" onMouseDown={onClose} />
      <div role="dialog" aria-label={title} style={{ ...panelStyle, ...(align === 'right' ? { right: 0 } : { left: 0 }) }}
        onKeyDown={onKeyDown} data-testid="folder-picker">
        <div style={titleStyle}>{title}</div>
        {from !== undefined && (
          <div style={fromStyle} data-testid="folder-picker-from">
            From <b style={{ color: 'inherit' }}>{from ? from.split('/').join(' / ') : ROOT_LABEL}</b>
          </div>
        )}
        <input ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} style={inputStyle}
          placeholder="Find or create a folder…" aria-label="Find or create a folder" role="combobox"
          aria-expanded="true" aria-controls="folder-picker-options"
          aria-activedescendant={options[active] ? `folder-opt-${active}` : undefined} />
        <ul id="folder-picker-options" role="listbox" aria-label="Folders" style={listStyle}>
          {options.map((o, i) => (
            <li key={o.key} id={`folder-opt-${i}`} role="option" aria-selected={i === active}
              style={i === active ? { ...optionStyle, ...optionActiveStyle } : optionStyle}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); pick(o); }}
              data-testid={o.create ? 'folder-picker-create' : 'folder-picker-option'}>
              {o.create ? <span style={plusStyle} aria-hidden="true">+</span> : <FolderIcon size={14} />}
              <span style={labelStyle} title={o.folder || ROOT_LABEL}>{o.label}</span>
            </li>
          ))}
          {options.length === 0 && <li style={emptyStyle}>No folders yet. Type a name to create one.</li>}
        </ul>
        {note && <div style={noteStyle} data-testid="folder-picker-note">{note}</div>}
      </div>
    </>
  );
}

const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const panelStyle: React.CSSProperties = {
  position: 'absolute', top: 'calc(100% + 6px)', zIndex: theme.zIndex.popover + 1, width: 300, boxSizing: 'border-box',
  background: theme.colors.surfaceRaised, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated, padding: 6, color: theme.colors.textPrimary, textAlign: 'left',
};
const titleStyle: React.CSSProperties = {
  padding: '4px 6px 6px', fontSize: 'var(--fs-xs)', fontWeight: 600, color: theme.colors.textTertiary,
};
const fromStyle: React.CSSProperties = {
  margin: '0 6px 8px', padding: '6px 8px', borderRadius: theme.radius.button, background: theme.colors.surfaceSubtle,
  fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
};
const inputStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', height: 30, padding: '0 8px', marginBottom: 4, font: 'inherit',
  fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary, background: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button,
};
const listStyle: React.CSSProperties = { listStyle: 'none', margin: 0, padding: 0, maxHeight: 260, overflowY: 'auto' };
const optionStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderRadius: theme.radius.button,
  fontSize: 'var(--fs-sm)', cursor: 'pointer', color: theme.colors.textPrimary,
};
const optionActiveStyle: React.CSSProperties = { background: theme.colors.activeBg, color: theme.colors.primary };
const labelStyle: React.CSSProperties = { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };
const plusStyle: React.CSSProperties = { width: 14, textAlign: 'center', fontWeight: 600 };
const emptyStyle: React.CSSProperties = { padding: '6px 8px', fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
const noteStyle: React.CSSProperties = {
  padding: '6px 8px 2px', fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary,
  borderTop: `1px solid ${theme.colors.border}`, marginTop: 4,
};
