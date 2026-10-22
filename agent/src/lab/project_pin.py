"""Project pin scope validator helpers.

Enforces the Requirement 12 invariant that project pins reference only
Published_Records with `scope ∈ {"group", "org"}`. The Project_Runtime_Role
is statically scoped to `groups/<g>/published/*` and `org/<orgId>/published/*`
prefixes (Requirement 10.5), so a pin against a `private`-scoped record would
be unresolvable at runtime by construction.

This module is a schema-level helper only. The Project asset type, triggers,
and run history are out of scope for this sprint and live in a follow-on spec
(see design.md "Out of scope"). The two functions exposed here are the
single chokepoint that pin write paths and resolve-time loaders MUST call:

- `validate_pin_target(record)` — invoked at pin-create / pin-update time
  to refuse a pin against a `private` record up front (Requirement 12.1, 12.2).
- `assert_pin_resolvable_for_project_runtime(record)` — invoked at
  resolve time before the Project_Runtime loads a pinned version, in case
  the pin record predates a later mutation that left the target unreachable
  (Requirement 12.4).

Both functions enforce the same invariant. They are kept distinct so call
sites read clearly: a write-time guard versus a runtime-resolution guard.

Requirements: 12.1, 12.2, 12.3, 12.4
Properties: 10 (Project pin non-private invariant)
"""

from __future__ import annotations

from .errors import PrivateScopePinError
from .models import AssetCommon


def validate_pin_target(record: AssetCommon) -> None:
    """Reject a project pin against a `private`-scoped Published_Record.

    Called at pin-create and pin-update time. Returns None on success.
    Raises `PrivateScopePinError` carrying the offending `asset_id` and
    `version` when `record.scope == "private"`.

    Requirement 12.3 is preserved structurally elsewhere: `Share` rejects
    any narrowing transition before this helper is ever consulted, so a
    Published_Record once visible to a project remains visible.
    """
    _enforce_non_private(record)


def assert_pin_resolvable_for_project_runtime(record: AssetCommon) -> None:
    """Refuse to resolve a pinned version with `scope == "private"`.

    Called at resolve time by the Project_Runtime before loading a pinned
    Published_Record (Requirement 12.4). The body is identical to
    `validate_pin_target`; the separate name documents that this is a
    runtime-side guard, not a write-side guard, so a regression in either
    path is independently traceable.

    Returns None on success. Raises `PrivateScopePinError` otherwise.
    """
    _enforce_non_private(record)


def _enforce_non_private(record: AssetCommon) -> None:
    """Shared body for both helpers.

    The pin invariant has exactly one rule today, but isolating it here
    keeps the two public entry points free of branching and makes any
    future extension (e.g. additional structural checks) visible in one
    place.
    """
    if record.scope == "private":
        # `version` is Optional on the dataclass because drafts have no
        # version; in practice project pins always target Published_Records
        # so `version` is set. Default to 0 in the unexpected None case so
        # the error still produces a parseable `@v<version>` suffix.
        version = record.version if record.version is not None else 0
        raise PrivateScopePinError(record.asset_id, version)
