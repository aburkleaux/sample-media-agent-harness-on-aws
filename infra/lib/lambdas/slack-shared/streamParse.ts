// Main Agent SSE/NDJSON parser -> text + render manifest keys.
// Reqs 6.1–6.4, 7.4.

export interface ParsedHarnessResponse {
  text: string;
  manifestKeys: string[];
}

export function parseHarnessStream(raw: string): ParsedHarnessResponse {
  let text = "";
  const manifestKeys: string[] = [];
  const seen = new Set<string>();

  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const payload = trimmed.startsWith("data: ") ? trimmed.slice(6) : trimmed;

    let parsed: unknown;
    try {
      parsed = JSON.parse(payload);
    } catch {
      text += payload;
      continue;
    }

    if (typeof parsed === "string") {
      const inner = parsed;
      try {
        const reparsed = JSON.parse(inner) as unknown;
        if (reparsed && typeof reparsed === "object") {
          collectObject(reparsed as Record<string, unknown>, manifestKeys, seen);
          continue;
        }
      } catch {
        /* genuine text string */
      }
      text += inner;
      continue;
    }

    if (parsed && typeof parsed === "object") {
      collectObject(parsed as Record<string, unknown>, manifestKeys, seen);
    }
  }

  return { text: text.trim(), manifestKeys };
}

function collectObject(
  obj: Record<string, unknown>,
  manifestKeys: string[],
  seen: Set<string>,
): void {
  if (obj.type === "step") {
    const key = obj.render_manifest_key;
    if (typeof key === "string" && key && !seen.has(key)) {
      seen.add(key);
      manifestKeys.push(key);
    }
  }
  // execution_summary / unknown objects are not reply text — dropped.
}
