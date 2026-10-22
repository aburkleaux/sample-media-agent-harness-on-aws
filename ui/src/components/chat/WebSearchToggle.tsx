/**
 * The + menu's Web_Search_Toggle (media-harness-web-search-toggle; registered into `composerSlots` by
 * `projects/registerWebSearchUi.ts`).
 *
 * A per-chat switch, off by default and remembered per chat (the session row's `webSearch`, through
 * `sessionProjectStore.setWebSearch`). It only offers itself where Web Search is deployed
 * (`/config.webSearchAvailable`); elsewhere it shows disabled with the reason, and the proxy and the runtime
 * both run every turn without it regardless of the value. Turning it on lets THIS chat's next turns search the
 * web; it never changes another chat.
 */
import React, { useState } from 'react';

import { peekRuntimeConfig } from '../../config/runtimeConfig';
import { setWebSearch, useSessionProject } from '../../projects/sessionProjectStore';
import type { WebSearchToggleSlotProps } from './composerSlots';

export const WEB_SEARCH_UNAVAILABLE = 'Web search is not available in this deployment';

/** Whether this deployment has Web Search (`/config.webSearchAvailable`); false until `/config` has loaded. */
export function webSearchAvailable(): boolean {
  return peekRuntimeConfig()?.webSearchAvailable === true;
}

export default function WebSearchToggle({ sessionId, locked }: WebSearchToggleSlotProps): React.ReactElement {
  const project = useSessionProject(sessionId);
  const [busy, setBusy] = useState(false);
  const available = webSearchAvailable();
  const on = available && project?.webSearch === true;
  const disabled = !available || !sessionId || locked || busy;
  const reason = !available ? WEB_SEARCH_UNAVAILABLE : locked ? 'This chat is locked' : undefined;

  const toggle = async () => {
    if (disabled || !sessionId) return;
    setBusy(true);
    try {
      await setWebSearch(sessionId, !on);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      role="menuitemcheckbox"
      aria-checked={on}
      aria-disabled={disabled}
      disabled={disabled}
      title={reason}
      data-testid="web-search-toggle"
      onClick={() => { void toggle(); }}
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, width: '100%',
        background: 'transparent', border: 'none', padding: 0, font: 'inherit', color: 'inherit',
        cursor: disabled ? 'default' : 'pointer', opacity: available ? 1 : 0.55, textAlign: 'left',
      }}
    >
      <span style={{ display: 'flex', flexDirection: 'column' }}>
        <span>Web search</span>
        {!available && <span style={{ fontSize: 11, opacity: 0.8 }}>{WEB_SEARCH_UNAVAILABLE}</span>}
      </span>
      <span
        aria-hidden="true"
        data-testid="web-search-toggle-state"
        style={{
          minWidth: 30, height: 16, borderRadius: 8, position: 'relative', flexShrink: 0,
          background: on ? 'var(--accent, #3b82f6)' : 'var(--border, #94a3b8)', transition: 'background 120ms',
        }}
      >
        <span style={{
          position: 'absolute', top: 2, left: on ? 16 : 2, width: 12, height: 12, borderRadius: 6,
          background: '#fff', transition: 'left 120ms',
        }} />
      </span>
    </button>
  );
}
