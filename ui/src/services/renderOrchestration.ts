/**
 * renderOrchestration — pure decision helpers for the renderer/animation
 * lifecycle. Extracted so the robustness fixes (Phase 1 of the eval-suite
 * plan) are unit-testable in isolation from React state.
 *
 * Each helper guards one observed UX bug:
 *  - computeInspectorSelection  → T4: leftover "Select an item…" placeholder
 *  - shouldRenderManifest       → T5: duplicate renders of the same artifact
 *  - isRenderPendingStep        → T6: inconsistent "Working…"/compose animation
 *  - shouldRevertSpuriousOpen   → T3: renderer drawer left open with no artifact
 */

import type { ManifestClip, CaptionCue } from '../components/nle/manifestParser';

export type InspectorSelection =
  | { type: 'clip'; clip: ManifestClip }
  | { type: 'cue'; cue: CaptionCue }
  | null;

/**
 * T4 — default the inspector to the first selectable item (clip, else first
 * cue) when the user hasn't explicitly selected anything, so the inspector
 * shows real metadata. The "Select an item on the timeline…" placeholder then
 * only appears for a genuinely empty manifest (no clips AND no captions).
 */
export function computeInspectorSelection(
  clips: ManifestClip[],
  captions: CaptionCue[] | undefined,
  selectedClip: ManifestClip | null,
  selectedCue: CaptionCue | null,
): InspectorSelection {
  if (selectedClip) return { type: 'clip', clip: selectedClip };
  if (selectedCue) return { type: 'cue', cue: selectedCue };
  if (clips && clips.length > 0) return { type: 'clip', clip: clips[0] };
  if (captions && captions.length > 0) return { type: 'cue', cue: captions[0] };
  return null;
}

/**
 * T5 — turn-scoped manifest single-flight. Returns true if this manifest key
 * should render (first time seen this turn), false if it's a duplicate emitted
 * by another path (e.g. hook + relay). Mutates `seen`. A modified artifact has
 * a NEW key (versioned outputs), so legitimate re-renders still pass.
 */
export function shouldRenderManifest(seen: Set<string>, key: string): boolean {
  if (!key) return false;
  if (seen.has(key)) return false;
  seen.add(key);
  return true;
}

/**
 * T6 — whether a step indicates Render Agent activity (→ show the composing
 * animation). Deterministic via the `agent === 'render'` marker present on
 * every forwarded Render Agent step (hook + dispatch paths); falls back to a
 * label match for older/other producers.
 */
export function isRenderPendingStep(step: { agent?: string; label?: string } | null | undefined): boolean {
  if (!step) return false;
  if (step.agent === 'render') return true;
  return /render/i.test(step.label || '');
}

/**
 * T3 — whether a resolved "render pending" episode should revert a spurious
 * drawer open: we auto-opened the renderer on render-agent activity, but no
 * render block ever arrived (the turn produced nothing renderable).
 */
export function shouldRevertSpuriousOpen(autoOpened: boolean, gotBlock: boolean): boolean {
  return autoOpened && !gotBlock;
}

/**
 * Double-render guard — a render-agent step should only (re)enter the
 * "Rendering…" pending state when NO manifest has rendered yet this turn. Once
 * a render has arrived, a trailing render-agent step must not flip the renderer
 * panel back to the loading animation (the reported render-twice flicker).
 */
export function shouldEnterRenderPending(isRenderStep: boolean, renderArrivedThisTurn: boolean): boolean {
  return isRenderStep && !renderArrivedThisTurn;
}

/**
 * renderer-visibility invariant — the Session Renderer panel is shown only when
 * the user hasn't collapsed it AND there is something to show (a render in
 * history or one composing). This prevents the empty "No rendered content yet"
 * drawer from appearing on sessions that produced no artifact.
 */
export function shouldShowRenderer(collapsed: boolean, historyLength: number, pending: boolean): boolean {
  return !collapsed && (historyLength > 0 || pending);
}


/**
 * Auto-follow (tab selection) rule for a NEW composing tab, from the agreed
 * situations table. Selection jumps to the new tab ONLY when it is the render
 * of the turn the user just initiated, it is the first such tab of that turn,
 * and the user has not manually pinned another tab during the turn. Everything
 * else (out-of-turn async completions, later tabs of a multi-render turn, a
 * turn where the user clicked away) keeps the current selection and just badges
 * the new tab "composing".
 *
 * The one exception is the very first entry in an empty panel: there is nothing
 * to yank the user away from, so always select it.
 *
 *  - firstEntry          → panel was empty (situation 1)
 *  - turnActive          → a user turn is streaming right now; false means an
 *                          out-of-turn async completion (situation 5 → badge only)
 *  - autoFollowConsumed  → already jumped to this turn's first composing tab
 *                          (situation 3 → later tabs badge only)
 *  - userPinned          → user manually selected a tab this turn
 *                          (situation 4 → do not yank)
 */
export interface AutoFollowState {
  firstEntry: boolean;
  turnActive: boolean;
  autoFollowConsumed: boolean;
  userPinned: boolean;
}

export function shouldAutoFollow(s: AutoFollowState): boolean {
  if (s.firstEntry) return true;
  return s.turnActive && !s.autoFollowConsumed && !s.userPinned;
}

/**
 * A composing placeholder that never received a manifest by turn-end is spurious
 * (the turn produced nothing renderable for it) and must be removed rather than
 * left as a stuck "Composing…" tab. Per-tab form of shouldRevertSpuriousOpen.
 */
export function shouldDropComposingEntry(
  entry: { status?: string; blocks?: unknown[] },
): boolean {
  return entry.status === 'composing' && (entry.blocks?.length ?? 0) === 0;
}
