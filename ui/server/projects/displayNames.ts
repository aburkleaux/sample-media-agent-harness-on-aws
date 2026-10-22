// Display_Name derivation, server side: the TS twin of `agent/src/ingestion/names.py::derive_display_name`
// (Req 3.4). The registry derives a file's Display_Name in Python at registration; the proxy re-derives
// the same base to answer `POST /projects/:pid/assets/duplicates` without asking the Asset_Service. Both
// sides run the shared fixture `agent/src/ingestion/fixtures/folder_cases.json` (`displayName`).
import type { AssetKind } from "./schema";

export const DISPLAY_NAME_MAX = 255;

const FORBIDDEN = /[[\]@#]/gu;
const OTHER = /\p{C}/gu; // control, format, surrogate, private-use, unassigned (Python category "C*")
const EXT_RE = /\.[A-Za-z0-9]{1,10}$/;
const SUFFIX_RE = / \((\d+)\)$/u;

const UNTITLED: Record<AssetKind, string> = {
  video: "Untitled video",
  image: "Untitled image",
  audio: "Untitled audio",
  document: "Untitled document",
};

/** Forbidden punctuation and "Other" characters removed, whitespace runs collapsed, ends trimmed. */
export function displayBase(name: unknown): string {
  const s = typeof name === "string" ? name : "";
  return s.replace(FORBIDDEN, "").replace(OTHER, "").replace(/\s+/gu, " ").trim();
}

function truncate(s: string): string {
  const cps = [...s];
  if (cps.length <= DISPLAY_NAME_MAX) return s;
  const m = EXT_RE.exec(s);
  const ext = m ? m[0] : "";
  if (ext && ext.length < DISPLAY_NAME_MAX) {
    const stem = [...s.slice(0, s.length - ext.length)];
    return stem.slice(0, DISPLAY_NAME_MAX - ext.length).join("").trimEnd() + ext;
  }
  return cps.slice(0, DISPLAY_NAME_MAX).join("").trimEnd();
}

/** The base Display_Name the registry gives `name` (before any ` (n)`), `Untitled <kind>` when empty. */
export function deriveDisplayName(name: unknown, kind: AssetKind): string {
  const out = truncate(displayBase(name));
  return out || UNTITLED[kind] || "Untitled";
}

/** A Display_Name without the registry's trailing ` (n)` copy suffix. */
export function withoutCopySuffix(displayName: string): string {
  return displayName.replace(SUFFIX_RE, "");
}
