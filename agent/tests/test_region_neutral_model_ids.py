"""Runtime model defaults are region-neutral (``global.`` inference profiles), never geography-locked.

The customer install deploys in any of the toolkit's regions (eu-west-1, us-east-1, us-west-2,
ap-southeast-2, ap-northeast-1). A ``us.`` / ``eu.`` / ``apac.`` / ``jp.`` / ``au.`` cross-region
profile only exists from its own geography, so such a default fails with a ValidationException in the
others (the render agent's ``us.anthropic.claude-haiku-4-5`` default would break every render in Tokyo).
Comments may still name them, e.g. to say ``us.X`` and ``global.X`` are the same model.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO_MODEL = re.compile(r"""["'](?:us|eu|apac|jp|au)\.(?:anthropic|amazon|meta|mistral|deepseek|cohere|openai)\.[\w.:-]+["']""")
SCANNED = ("agent/src", "infra/lib", "ui/server")
SUFFIXES = {".py", ".ts", ".json", ".yaml", ".yml"}


def _code_lines(path: pathlib.Path):
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*")):
            continue
        yield n, line


def test_no_geography_locked_model_ids_in_runtime_code():
    hits = []
    for top in SCANNED:
        for path in (ROOT / top).rglob("*"):
            if path.suffix not in SUFFIXES or "test" in path.name or "node_modules" in path.parts:
                continue
            for n, line in _code_lines(path):
                if GEO_MODEL.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert not hits, "use a global. inference profile instead:\n" + "\n".join(hits)
