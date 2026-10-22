/** The few line icons the chat's asset cards and preview use (24px grid, currentColor strokes). */
import React from 'react';

export type AssetIconKind = 'expand' | 'download' | 'panel' | 'close' | 'clock' | 'page' | 'audio' | 'image' | 'video' | 'folder' | 'cut' | 'similar' | 'trash';

const PATHS: Record<AssetIconKind, React.ReactNode> = {
  expand: <path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7" />,
  download: <path d="M12 4v11M7 10l5 5 5-5M5 20h14" />,
  panel: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M14 4v16" /></>,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  page: <><path d="M7 3h7l5 5v13H7z" /><path d="M14 3v5h5" /></>,
  audio: <path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2" />,
  image: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 16l5-5 4 4 3-3 6 6" /></>,
  video: <><rect x="3" y="6" width="13" height="12" rx="2" /><path d="M16 10l5-3v10l-5-3z" /></>,
  folder: <><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="M10 13h6M13 10l3 3-3 3" /></>,
  cut: <><circle cx="6" cy="7" r="3" /><circle cx="6" cy="17" r="3" /><path d="M8.5 8.5L20 19M8.5 15.5L20 5" /></>,
  similar: <><circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" /></>,
  trash: <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />,
};

export function AssetIcon({ kind, size = 17 }: { kind: AssetIconKind; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {PATHS[kind]}
    </svg>
  );
}
