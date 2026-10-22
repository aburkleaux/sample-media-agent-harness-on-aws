/**
 * Strip internal, model-only prompt augmentation from a user turn before it is shown
 * (or compared) as the user's transcript. The agent/UI decorate a turn with context
 * the model needs on replay but the user never typed:
 *   - a trailing `\n\n[Current date: YYYY-MM-DD]` block (agent/src/main.py);
 *   - a trailing `[source: <url>]` / `[sources: …]` block a Renderer action appends so
 *     the agent can target the right clip (ui/src/views/WorkspaceView.tsx);
 *   - leading `[File attached: … S3 bucket/key/URI/URL …]` blocks (one per attached file)
 *     the upload path prepends for the model, whose UI-visible counterpart is one
 *     `📎 name (size)` chip line per file (ui/src/components/ChatCanvas.tsx).
 *
 * These live correctly in AgentCore Memory (the model needs them on replay), but the
 * user's transcript should show only what they typed. Used to render Memory-sourced
 * (`mem-`) user turns clean in the raw-Memory debug view, and by the proxy's
 * server-side mirror (transcriptStore.normalizeUserPrompt) when it persists a user turn
 * without a client-supplied clean display_text. Idempotent, and a no-op (trim only) when
 * there is no augmentation.
 *
 * Lives in its own module (not lab-api) so it isn't part of lab-api's mockable
 * surface — components can import it without every `vi.mock('../lab-api')` needing
 * to re-declare it.
 */
export function normalizeUserTurnContent(text: string): string {
  if (typeof text !== 'string') return '';
  return text
    // Leading [File attached: …] blocks the upload path prepends for the model, one per attached file.
    .replace(/^(?:\s*\[File attached:[\s\S]*?\]\s*)+/i, '')
    // Leading `📎 name (size)` attachment chip lines the UI prepends for display, one per attached file.
    .replace(/^(?:\s*📎[^\n]*\n?)+/u, '')
    // Trailing [source: …] / [sources: …] block a Renderer action appends.
    .replace(/\s*\[sources?:[^\]]*\]\s*$/i, '')
    // Trailing [Current date: YYYY-MM-DD] block (agent/src/main.py).
    .replace(/\s*\n+\[Current date:\s*\d{4}-\d{2}-\d{2}\]\s*$/i, '')
    .trim();
}
