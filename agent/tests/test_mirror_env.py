"""The mirror reads the env names main.py reads -- not main.py's variable names.

main.py does:

    S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", ...)
    S3_PREFIX = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")

_mirror_external_media originally read "S3_BUCKET" and "S3_PREFIX" -- the variable
names. Neither exists in the environment, so the bucket was always "" and the
function returned on its first guard every time it was called. The mirror never ran
once in production, and the guard was silent, so nothing in the logs said so.

These tests fail if the names drift apart again.
"""
import os
import sys
import re
import pathlib
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def test_mirror_reads_the_same_env_names_main_reads():
    main_txt = (SRC / "main.py").read_text(encoding="utf-8")
    actions_txt = (SRC / "hooks" / "actions.py").read_text(encoding="utf-8")

    def env_names(txt: str) -> set[str]:
        return set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', txt))

    main_names = env_names(main_txt)
    assert "SKILLS_S3_BUCKET" in main_names
    assert "ARTIFACTS_PREFIX" in main_names

    mirror_block = actions_txt[actions_txt.index("def _mirror_external_media"):]
    mirror_block = mirror_block[: mirror_block.index("\ndef ", 10)]
    used = env_names(mirror_block)
    assert "SKILLS_S3_BUCKET" in used, "must read the bucket name main.py reads"
    assert "ARTIFACTS_PREFIX" in used, "must read the prefix name main.py reads"


def test_missing_bucket_is_logged_not_silent(monkeypatch, caplog):
    """A silent return is indistinguishable from 'nothing to mirror'."""
    from hooks import actions

    monkeypatch.delenv("SKILLS_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    urls = ["https://v3b.fal.media/files/b/x/y.jpg"]

    with caplog.at_level(logging.WARNING):
        out = actions._mirror_external_media(urls)

    assert out == urls, "urls pass through unchanged when unconfigured"
    assert any("no bucket configured" in r.message or "no bucket configured" in r.getMessage()
               for r in caplog.records), "the skip must be visible in the logs"
