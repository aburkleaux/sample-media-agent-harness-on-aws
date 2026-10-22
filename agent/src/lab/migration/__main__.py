"""Module entrypoint for ``python -m lab.migration``.

This file exists solely so the migration package can be invoked as
``python -m lab.migration`` per the task 8.1 contract. All argument
parsing and orchestration live in :mod:`lab.migration.cli`; this
module simply forwards ``sys.argv[1:]`` to :func:`cli.main` and exits
with the returned status code.

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7
"""

from __future__ import annotations

import sys

from .cli import main


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main(sys.argv[1:]))
