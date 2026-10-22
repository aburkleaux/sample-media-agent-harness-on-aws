/**
 * Which models accept the reasoning-effort cap (`output_config.effort`).
 *
 * This is a SECOND copy of the matrix in `agent/src/model/factory.py`
 * (`_REASONING_EFFORT_CAPABLE`). It lives here because the proxy `/models` route
 * computes its model list from `ListInferenceProfiles` independently of the
 * runtime, so it must compute `supportsEffort` itself to surface it to the UI.
 *
 * Support is per-GENERATION, verified live via Bedrock Converse (us-west-2,
 * 2026-10): generation >= 4.6 or >= 5 across opus/sonnet/haiku accepts the field;
 * everything <= 4.5 (incl. opus-4-5 / sonnet-4-5 / haiku-4-5), 4.1 and 3.x
 * REJECTS it with a hard 400. The UI uses this only to show the slider; the
 * backend `supports_reasoning_effort` gate is authoritative and fail-safe.
 *
 * Entries are id SUBSTRINGS so the region prefix (`us.`/`global.`) and the date
 * suffix are irrelevant. 'claude-opus-5' covers opus-5 AND opus-5-5;
 * 'claude-sonnet-5' covers sonnet-5 AND sonnet-5-5. Keep in step with the Python
 * copy — `reasoningEffort.test.ts` pins the same ids so the two cannot drift.
 */
export const REASONING_EFFORT_CAPABLE: readonly string[] = [
  "claude-opus-4-6",
  "claude-opus-4-7",
  "claude-opus-4-8",
  "claude-opus-5", // opus-5 and opus-5-5
  "claude-sonnet-4-6",
  "claude-sonnet-5", // sonnet-5 and sonnet-5-5
  "claude-haiku-5-5",
];

/** Whether `modelId` accepts output_config.effort (substring match, case-insensitive). */
export function supportsEffort(modelId: string | undefined | null): boolean {
  const mid = (modelId ?? "").toLowerCase();
  return REASONING_EFFORT_CAPABLE.some((fam) => mid.includes(fam));
}
