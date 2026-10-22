/**
 * "Move to folder" for one project file shown outside the Assets grid — the chat preview's header and a Renderer
 * tab. It files the asset in another folder of its project with the same `FolderPicker` the grid uses; a move
 * is metadata only (no bytes move, nothing is re-indexed).
 *
 * The caller knows the asset by id (an `asset:` reference, a citation) or only by the URL the chat shows (an
 * artifact link): a URL is resolved to its asset when the button is first used, and the button disappears when
 * it names no asset of this project. A hidden "this chat only" upload is not offered either: filing it in a
 * project folder would have to make it visible to the whole project, which this does not do.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { getAsset, listFolders, moveAssets, resolveLocations } from '../../projects/api';
import { buildFolderTree, flattenFolders } from '../../projects/folderPaths';
import { readPendingFolders } from '../../projects/pendingFolders';
import { FolderPicker } from '../projects/FolderPicker';
import { AssetIcon } from './AssetIcons';

export interface MoveTarget {
  projectId: string;
  assetId?: string | null;
  /** The URL the chat shows, when there is no asset id. */
  location?: string;
}

interface Resolved {
  assetId: string;
  folder: string;
}

/**
 * The Move to folder state machine for one file: resolve it (a URL → its asset), list the folders, move, undo.
 * `available` is false until the file is known to be a movable asset of the project. `ui` is the picker and the
 * status line, rendered inside a `position: relative` wrapper by whoever shows the trigger (a button, a ⋯ item).
 */
export function useMoveToFolder(target: MoveTarget | undefined, name: string, light = false) {
  const none: MoveTarget = { projectId: '' };
  const t = target ?? none;
  const [resolved, setResolved] = useState<Resolved | null | undefined>(undefined); // undefined = not looked up
  const [open, setOpen] = useState(false);
  const [folders, setFolders] = useState<string[]>([]);
  const [status, setStatus] = useState<{ text: string; undo?: () => void } | null>(null);

  const resolve = useCallback(async (): Promise<Resolved | null> => {
    if (!t.projectId) return null; // no project (or no target): nothing to move
    let id = t.assetId ?? null;
    if (!id && t.location) {
      const r = await resolveLocations(t.projectId, [t.location]);
      id = r.ok ? (r.value[t.location] ?? null) : null;
    }
    if (!id) return null;
    const a = await getAsset(t.projectId, id);
    if (!a?.ok || a.value.status === 'removed' || a.value.visibility === 'session') return null;
    return { assetId: id, folder: typeof a.value.folder === 'string' ? a.value.folder : '' };
  }, [t.projectId, t.assetId, t.location]);

  // Look the asset up once, up front, so a link to a non-project file never shows the button at all.
  useEffect(() => {
    let live = true;
    void resolve().then((r) => { if (live) setResolved(r); });
    return () => { live = false; };
  }, [resolve]);

  const openPicker = async () => {
    setStatus(null);
    const r = await listFolders(t.projectId);
    // Include folders made in the project that hold nothing yet (they exist only as pending folders).
    setFolders(flattenFolders(buildFolderTree(r.ok ? r.value.folders : [], readPendingFolders(t.projectId))));
    setOpen(true);
  };

  const move = async (folder: string) => {
    setOpen(false);
    const cur = resolved;
    if (!cur) return;
    const r = await moveAssets(t.projectId, [cur.assetId], folder);
    if (!r.ok || r.value.moved.length === 0) {
      setStatus({ text: `Couldn't move ${name}` });
      return;
    }
    const from = cur.folder;
    setResolved({ ...cur, folder });
    setStatus({
      text: `Moved to ${folder || 'Top level'}`,
      undo: () => {
        void moveAssets(t.projectId, [cur.assetId], from).then((u) => {
          if (u.ok) { setResolved({ ...cur, folder: from }); setStatus({ text: `Back in ${from || 'Top level'}` }); }
        });
      },
    });
  };

  const available = !!target && !!resolved;
  const ui = (
    <>
      {open && resolved && (
        <FolderPicker folders={folders} current={resolved.folder} from={resolved.folder} title={`Move ${name} to`} align="right"
          onPick={(f) => void move(f)} onClose={() => setOpen(false)} />
      )}
      {status && (
        <span role="status" className={light ? 'cap-move-status cap-move-status--light' : 'cap-move-status'} data-testid="move-to-folder-status">
          {status.text}
          {status.undo && <button type="button" onClick={() => { const u = status.undo; setStatus(null); u?.(); }}>Undo</button>}
        </span>
      )}
    </>
  );
  return { available, open, openPicker: () => void openPicker(), close: () => setOpen(false), ui,
    /** Where the file is filed, once resolved (`""` = top level). */
    folder: resolved ? resolved.folder : undefined, assetId: resolved?.assetId };
}

export function MoveToFolderButton({ target, name, light = false, label = 'Move to folder' }: {
  target: MoveTarget; name: string;
  /** On a light surface (a Renderer tab's toolbar) rather than the preview's dark stage. */
  light?: boolean;
  label?: string;
}) {
  const m = useMoveToFolder(target, name, light);
  if (!m.available) return null;
  return (
    <span style={wrapStyle}>
      <button type="button" className={light ? 'cap-btn-light' : 'cap-btn'} onClick={() => (m.open ? m.close() : m.openPicker())}
        aria-haspopup="dialog" aria-expanded={m.open} data-testid="move-to-folder">
        <AssetIcon kind="folder" size={15} /> {label}
      </button>
      {m.ui}
    </span>
  );
}

const wrapStyle: React.CSSProperties = { position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 10 };
