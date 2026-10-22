/**
 * The outcome of one Render Agent stream (`POST /render`), reduced to the four things a caller can act on.
 *
 * - `manifest`: a clean `output_ref` (no `error` flag). The LAST clean one wins.
 * - `no_view`: the agent declined deliberately (`{"type":"no_view"}`, e.g. data-only JSON). Terminal and not a
 *   failure: the client drops the placeholder tab instead of showing "Composing…" forever.
 * - `error`: only error-flagged output_refs (a failure record under `render/_errors/`). Never surfaced as a
 *   manifest — session a2d759c2 filled a tab with a VALIDATION_FAILED record.
 * - `none`: nothing usable.
 */
export type RenderOutcome =
  | { kind: "manifest"; s3Key: string }
  | { kind: "no_view"; reason: string }
  | { kind: "error"; code: string }
  | { kind: "none" };

export function parseRenderOutcome(sse: string): RenderOutcome {
  let manifest: string | null = null;
  let errorCode: string | null = null;
  let noView: string | null = null;
  for (const line of sse.split("\n")) {
    if (!line.startsWith("data: ")) continue;
    let v: any;
    try {
      v = JSON.parse(line.slice(6));
      if (typeof v === "string") { try { v = JSON.parse(v); } catch { /* keep the string */ } }
    } catch { continue; }
    if (!v || typeof v !== "object") continue;
    if (v.type === "output_ref" && typeof v.s3_key === "string" && v.s3_key) {
      if (v.error || v.s3_key.includes("/render/_errors/")) errorCode = String(v.error_code ?? "error");
      else manifest = v.s3_key;
    } else if (v.type === "no_view") {
      noView = String(v.reason ?? "no_view");
    }
  }
  if (manifest) return { kind: "manifest", s3Key: manifest };
  if (noView) return { kind: "no_view", reason: noView };
  if (errorCode) return { kind: "error", code: errorCode };
  return { kind: "none" };
}
