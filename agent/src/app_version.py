"""The release this process runs: ``APP_VERSION`` (stamped by CDK from ``-c appVersion=``, which CI computes
from the repo's VERSION file with ``scripts/release/version.sh``), else ``dev``. On an agent runtime it arrives
through RuntimeConfigSecret; on the Capability API it is task env."""
from __future__ import annotations

import os


def app_version() -> str:
    return (os.environ.get("APP_VERSION") or "").strip() or "dev"
