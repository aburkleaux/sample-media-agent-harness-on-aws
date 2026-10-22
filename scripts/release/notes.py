#!/usr/bin/env python3
"""Release notes for v<VERSION>: the commits since the previous release tag, grouped by conventional type.

    python3 scripts/release/notes.py [--version 0.9.0] [--since v0.8.0]

The previous release is the HIGHEST release tag (``vMAJOR.MINOR.PATCH``, no pre-release suffix) reachable from
HEAD that is LOWER than the version being released (none: every commit). Compared as numbers, so v0.14.2 precedes
v1.0.0 and v0.9.0 precedes v0.14.0; and never the release's own tag, so re-running the release job after the tag
exists still lists the release's commits instead of none. Merge commits are skipped; ``feat`` / ``fix`` /
``perf`` get their own sections, everything else is "Other". Output is the GitLab Release body (CI) and what to
paste into CHANGELOG.md.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECTIONS = (("feat", "Features"), ("fix", "Fixes"), ("perf", "Performance"))
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<subject>.+)$")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_version(v: str) -> tuple[int, int, int] | None:
    """``1.0.0`` / ``v1.0.0`` -> (1, 0, 0); anything else (a pre-release, a build suffix) -> None."""
    m = _RELEASE_TAG.match(v if v.startswith("v") else f"v{v}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def pick_previous(tags: list[str], version: str) -> str | None:
    """The highest release tag strictly below ``version`` (all release tags when it does not parse)."""
    current = parse_version(version)
    best: tuple[tuple[int, int, int], str] | None = None
    for tag in tags:
        v = parse_version(tag.strip())
        if v is None or (current is not None and v >= current):
            continue
        if best is None or v > best[0]:
            best = (v, tag.strip())
    return best[1] if best else None


def previous_tag(version: str) -> str | None:
    try:
        tags = _git("tag", "--merged", "HEAD", "--list", "v[0-9]*").splitlines()
    except subprocess.CalledProcessError:
        return None
    return pick_previous(tags, version)


def notes(version: str, since: str | None) -> str:
    rng = f"{since}..HEAD" if since else "HEAD"
    lines = _git("log", "--no-merges", "--format=%h%x09%s", rng).splitlines()
    groups: dict[str, list[str]] = {key: [] for key, _ in SECTIONS}
    other: list[str] = []
    for line in lines:
        sha, _, subject = line.partition("\t")
        m = _CONVENTIONAL.match(subject)
        if m and m.group("type") in groups:
            scope = f"**{m.group('scope')}**: " if m.group("scope") else ""
            groups[m.group("type")].append(f"- {scope}{m.group('subject')} ({sha})")
        else:
            other.append(f"- {subject} ({sha})")
    out = [f"## v{version}", ""]
    out.append(f"{len(lines)} commits since {since}." if since else f"{len(lines)} commits (first release).")
    for key, title in SECTIONS:
        if groups[key]:
            out += ["", f"### {title}", *groups[key]]
    if other:
        out += ["", "### Other", *other]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", default=(ROOT / "VERSION").read_text().strip())
    ap.add_argument("--since", default=None, help="previous release tag (default: newest v* tag before HEAD)")
    args = ap.parse_args(argv)
    sys.stdout.write(notes(args.version, args.since or previous_tag(args.version)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
