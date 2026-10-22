#!/usr/bin/env python3
"""Build a safe, self-contained Agent Olive review/comparison page.

Renders one <video> cell per manifest entry. Two uses:
- Per-SEGMENT review gallery (primary): one entry per packed segment, labelled
  with its text and its review status; flagged segments render a red badge.
- Model comparison (legacy): one entry per model.

Input manifest shape:
{
  "title": "Agent Olive segment review",
  "subtitle": "Flagged segments need regeneration",
  "columns": 3,
  "clips": [
    {"label": "Segment 1", "line": "Hi everybody! ...", "url": "https://...",
     "status": "passed"},
    {"label": "Segment 2", "line": "...", "url": "https://...",
     "status": "flagged", "gates": ["mouth_motion"]}
  ]
}
``model`` is accepted as an alias for ``label``; ``chunk`` for ``line``.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from urllib.parse import urlparse

_DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "comparison-page.html"
_PALETTE = ("#7dd3fc", "#c4b5fd", "#f9a8d4", "#86efac", "#fde68a", "#fca5a5")
_FLAG_ACCENT = "#fca5a5"
_FLAGGED = {"flagged", "failed", "regenerate", "reject", "rejected", "unverified"}
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_STYLES = """
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#09090b;color:#f4f4f5}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#18181b,#09090b 55%)}
main{max-width:1440px;margin:0 auto;padding:32px}h1{margin:0 0 8px;font-size:clamp(1.6rem,3vw,2.6rem)}
p{margin:0 0 24px;color:#a1a1aa}.grid{display:grid;grid-template-columns:repeat(var(--columns),minmax(0,1fr));gap:18px}
.cell{overflow:hidden;border:1px solid color-mix(in srgb,var(--accent) 55%,#27272a);border-top:4px solid var(--accent);border-radius:14px;background:#18181b;box-shadow:0 12px 35px #0008}
.label{padding:12px 14px;font-weight:700}.line{display:block;margin-top:4px;color:#d4d4d8;font-size:.9rem;font-weight:400;white-space:pre-wrap}
.badge{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:999px;background:var(--accent);color:#09090b;font-size:.72rem;font-weight:700;vertical-align:middle}
video{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#000}@media(max-width:850px){.grid{grid-template-columns:1fr!important}main{padding:18px}}
""".strip()


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"clips": data}
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        raise ValueError("manifest must be an object with a clips array")
    if not data["clips"]:
        raise ValueError("clips array must not be empty")
    return data


def safe_https_url(raw: object) -> str:
    value = str(raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"clip URL must be an HTTPS URL without embedded credentials: {value!r}")
    return value


def render_cell(clip: dict, index: int) -> str:
    if not isinstance(clip, dict):
        raise ValueError(f"clip {index} must be an object")
    label = html.escape(str(clip.get("label") or clip.get("model") or f"Segment {index + 1}"))
    line = html.escape(str(clip.get("line") or clip.get("chunk") or ""))
    url = html.escape(safe_https_url(clip.get("url")), quote=True)
    status_raw = str(clip.get("status") or "").strip()
    flagged = status_raw.lower() in _FLAGGED
    accent_raw = str(clip.get("accent") or "")
    if flagged:
        accent = _FLAG_ACCENT
    elif _HEX_COLOR.fullmatch(accent_raw):
        accent = accent_raw
    else:
        accent = _PALETTE[index % len(_PALETTE)]
    badge = ""
    if status_raw:
        gates = clip.get("gates")
        detail = ""
        if flagged and isinstance(gates, list) and gates:
            detail = ": " + ", ".join(html.escape(str(g)) for g in gates)
        badge = f'<span class="badge">{html.escape(status_raw)}{detail}</span>'
    line_html = f'<span class="line">{line}</span>' if line else ""
    return (
        f'<article class="cell" style="--accent:{accent}">'
        f'<div class="label">{label}{badge}{line_html}</div>'
        f'<video controls preload="metadata" playsinline src="{url}"></video>'
        "</article>"
    )


def render_page(manifest: dict, template: str) -> str:
    title = html.escape(str(manifest.get("title") or "Agent Olive review"))
    subtitle = html.escape(str(manifest.get("subtitle") or "Review each segment; flagged segments need regeneration."))
    clips = manifest["clips"]
    override = manifest.get("columns")
    if isinstance(override, int) and 1 <= override <= 4:
        columns = override
    else:
        columns = max(1, min(3, len(clips)))
    cells = "\n".join(render_cell(clip, index) for index, clip in enumerate(clips))
    replacements = {
        "{{TITLE}}": title,
        "{{SUBTITLE}}": subtitle,
        "{{GRID_COLUMNS}}": str(columns),
        "{{STYLES}}": _STYLES,
        "{{CELLS}}": cells,
    }
    page = template
    for token, value in replacements.items():
        page = page.replace(token, value)
    leftovers = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", page)))
    if leftovers:
        raise ValueError(f"template contains unresolved placeholders: {leftovers}")
    return page


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--template", type=Path, default=_DEFAULT_TEMPLATE)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        manifest = load_manifest(args.manifest)
        template = args.template.read_text(encoding="utf-8")
        page = render_page(manifest, template)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(page, encoding="utf-8")
        print(json.dumps({"output": str(args.output), "clips": len(manifest["clips"]), "bytes": len(page.encode("utf-8"))}, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
