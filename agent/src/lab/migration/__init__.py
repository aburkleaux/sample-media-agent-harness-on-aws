"""Legacy-skill migration package — one-shot driver.

This package contains the one-time migration of legacy
``s3://<bucket>/skills/<name>/`` artifacts into the unified
``org/<adminOrgId>/published/skills/<publishedId>/v1/`` tree, plus the
bootstrap of the org-scoped ``Core_Task_Agent`` published specialty
agent and the rewrite of workflow ``agent:{kind:'core',
ref:'task-agent'}`` references to the new ``Asset_Ref`` form.

The package is designed to be invoked as a module from the command
line::

    python -m lab.migration --dry-run --bucket <b> --table <t> \\
        --admin-user <u> --admin-org <o>

Public API:

- :func:`drive`  — orchestrates discovery → bootstrap → copy → rewrite
  → report under one driver function. The driver accepts injected
  dependencies (S3 client, :class:`AssetIndex`) so it is unit-testable
  without AWS credentials.
- :func:`main`   — argparse entrypoint. Builds default boto3
  dependencies and delegates to :func:`drive`.
- :func:`build_parser` — exposes the argparse ``ArgumentParser`` so
  tests can introspect the documented flags.

This module is the skeleton produced by spec task 8.1; sub-modules
(:mod:`discovery`, :mod:`bootstrap`, :mod:`copy`, :mod:`rewrite`,
:mod:`report`) are stubs that will be filled in by tasks 8.2 – 8.7.

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7
"""

from __future__ import annotations

from .cli import build_parser, drive, main

__all__ = ["build_parser", "drive", "main"]
