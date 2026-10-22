/**
 * WorkspaceView's half of a Moment card's Clip auto-open (see services/momentClipAutoOpen): when the
 * click's job succeeds, open its notification exactly as a bell click does — but only if that session is
 * the one on screen. A user who has moved to another session is not yanked back; the normal bell
 * notification is still there for them.
 */
import { useEffect, useRef } from 'react';

import { MOMENT_CLIP_READY_EVENT, type MomentClipReadyDetail } from '../services/momentClipAutoOpen';
import { notificationsStore, type JobNotification } from '../services/notifications';

export function useMomentClipAutoOpen(
  activeSessionId: string | null | undefined,
  open: (n: JobNotification) => void,
): void {
  const activeRef = useRef(activeSessionId);
  activeRef.current = activeSessionId;
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    const onReady = (e: Event) => {
      const n = (e as CustomEvent<MomentClipReadyDetail>).detail?.notification;
      if (!n || !n.sessionId || n.sessionId !== activeRef.current) return;
      // Opened for the user: the bell item is handled, and its banner would announce a tab already shown.
      notificationsStore.markSeen(n.jobId);
      notificationsStore.markBannered(n.jobId);
      openRef.current(n);
    };
    window.addEventListener(MOMENT_CLIP_READY_EVENT, onReady);
    return () => window.removeEventListener(MOMENT_CLIP_READY_EVENT, onReady);
  }, []);
}
