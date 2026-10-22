"""No caller sends a pre-rename M2C parameter (bundle alpha.20261014a renamed them; every env runs it).

The harness no longer translates between spellings, so an old name in a skill, a prompt or an argument builder
is a call the gateway rejects. The only places allowed to name an old spelling are the ones that refuse it.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
OLD = re.compile(r"\b(asset_s3_uri|transcript_s3_uri|documents_s3_uri|manifest_s3_uri|search_image_uri)\b")
SCANNED = ("agent/src", "agent/skills", "agent/scripts", "ui/src", "ui/server", "infra/lib")
SUFFIXES = {".py", ".md", ".ts", ".tsx", ".json", ".txt", ".mjs", ".js"}
# SearchTargetSync refuses an upstream that still declares the old image input; the policy builder says why.
ALLOWED = {"infra/lib/lambdas/search-target-sync/sync.py", "infra/lib/lambdas/search-target-sync/test_sync.py",
           "infra/lib/projects/search-policy.ts"}
SKIP_DIRS = {"node_modules", "botocore_models", "__pycache__", ".venv", "cdk.out", "dist"}
# video-processing references carry the M2C authors' prompt VERBATIM below a harness-owned header (pinned by
# test_video_processing_skill_package); those bodies predate the rename, so only the header is scanned, and a
# header above a body with old names must carry the "Parameter names" note that maps them.
UPSTREAM_BODY = re.compile(r"(?m)^# .+ System Prompt$")


def test_no_pre_rename_parameter_names():
    hits = []
    for top in SCANNED:
        for path in (ROOT / top).rglob("*"):
            if path.suffix not in SUFFIXES or not path.is_file() or SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            body = UPSTREAM_BODY.search(text) if "/references/" in rel else None
            if body:
                if OLD.search(text[body.start():]) and "**Parameter names:**" not in text[:body.start()]:
                    hits.append(f"{rel}: upstream body names an old parameter but the header has no Parameter names note")
                text = text[:body.start()]
            for n, line in enumerate(text.splitlines(), 1):
                if OLD.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, "pre-rename M2C parameter names (use the *_s3uri spelling):\n" + "\n".join(hits)
