"""The release a process runs (APP_VERSION, else ``dev``), reported on every turn's execution_summary."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app_version import app_version  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"


def test_reads_app_version_else_dev(monkeypatch):
    monkeypatch.setenv("APP_VERSION", " 0.9.0 ")
    assert app_version() == "0.9.0"
    monkeypatch.delenv("APP_VERSION")
    assert app_version() == "dev"


def test_every_turn_summary_names_the_release():
    src = (SRC / "main.py").read_text()
    summary = src[src.index('"type": "execution_summary"'):]
    assert '"app_version": app_version()' in summary[:1500]


def test_the_capability_image_ships_the_module():
    assert "COPY src/app_version.py ./src/app_version.py" in (SRC.parent / "capability.Dockerfile").read_text()
