/**
 * activeSession — the session id currently on screen.
 *
 * Set by WorkspaceView whenever the active session changes; read by the
 * implicit auto-render (autoRenderOnComplete) to decide HOW to surface a
 * completed job:
 *   - if the job's session IS the one on screen → drive the live chat stream
 *     (user bubble + "Working…" animation + steps + renderer drawer), matching
 *     a normal typed turn;
 *   - otherwise → persist quietly into that session's cache in the background.
 */

let _activeSessionId = '';

export function setActiveSessionId(id: string): void {
  _activeSessionId = id || '';
}

export function getActiveSessionId(): string {
  return _activeSessionId;
}
