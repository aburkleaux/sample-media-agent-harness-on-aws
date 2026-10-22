/**
 * Test helper: matches an emoji or a unicode symbol used as an icon in text — the glyphs the Projects UI
 * replaced with SVG icons (🔒 📎 ⚙ ✓ ✕ ⤒ and their kin).
 *
 *   * `\p{Extended_Pictographic}` — emoji, including 🔒 📎 ⚙.
 *   * U+2190–U+21FF Arrows, U+2700–U+27BF Dingbats (✓ ✕), U+2900–U+297F Supplemental Arrows-B (⤒).
 *
 * Ordinary punctuation the UI uses on purpose (`…`, `·`) is outside every range.
 */
export const EMOJI_OR_GLYPH = /[\p{Extended_Pictographic}\u2190-\u21FF\u2700-\u27BF\u2900-\u297F]/u;
