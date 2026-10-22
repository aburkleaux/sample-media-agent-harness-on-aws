"""The repo ships to customers: no third-party ffmpeg binaries or download sources, anywhere.

The sandbox's ffmpeg is the harness's own LGPL build (infra/lib/distribution/ffmpeg/Dockerfile),
staged by the deploy. A GPL static build host creeping back in (as a download default, a doc
pointer, or a fallback) is a licensing problem, so it fails here.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FORBIDDEN = ("johnvansickle", "evermeet.cx", "BtbN/FFmpeg-Builds", "ffmpeg.zeranoe")


# `.cache`: CI's pip / uv / npm caches (.gitlab-ci.yml) sit under the project dir — third-party wheels, not ours.
_SKIP_DIRS = {".git", "node_modules", ".venv", "dist", "cdk.out", "__pycache__", ".pytest_cache", ".cache"}


def _tracked_files() -> list[str]:
    """`git ls-files`, or (CI's Python image has no git) every file under the repo minus build/vendor dirs."""
    try:
        return subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                              check=True).stdout.split()
    except (FileNotFoundError, subprocess.CalledProcessError):
        out = []
        for path in REPO.rglob("*"):
            rel = path.relative_to(REPO)
            if path.is_file() and not _SKIP_DIRS.intersection(rel.parts):
                out.append(str(rel))
        return out


def test_no_third_party_ffmpeg_sources_in_the_repo():
    tracked = _tracked_files()
    hits = []
    for rel in tracked:
        if rel.endswith(("test_no_third_party_ffmpeg.py",)) or not (REPO / rel).is_file():
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for needle in FORBIDDEN:
            if needle.lower() in text.lower():
                hits.append(f"{rel}: {needle}")
    assert not hits, "third-party ffmpeg source referenced:\n" + "\n".join(hits)
