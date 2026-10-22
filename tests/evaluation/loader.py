"""Dataset loader + schema validation (Requirement 1).

Validates scenario dicts against the design's JSON dataset schema without a hard
jsonschema dependency (kept lightweight so unit tests run in the agent venv).
Invalid scenarios are skipped with collected errors (Requirement 1.7 / 19.3).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.evaluation.scenario_builder import VALID_CATEGORIES

REQUIRED_FIELDS = ["scenario_id", "category", "description", "turns", "expected_trajectory", "assertions"]
VALID_MODES = {"fixed", "simulated"}
_ID_RE = re.compile(r"^[a-z0-9-]+$")


def validate_scenario(sc: dict) -> list[str]:
    """Return a list of validation errors ([] if valid)."""
    errors: list[str] = []
    if not isinstance(sc, dict):
        return ["scenario is not an object"]
    for f in REQUIRED_FIELDS:
        if f not in sc:
            errors.append(f"missing required field: {f}")
    sid = sc.get("scenario_id")
    if isinstance(sid, str) and not _ID_RE.match(sid):
        errors.append(f"scenario_id {sid!r} does not match ^[a-z0-9-]+$")
    cat = sc.get("category")
    if cat is not None and cat not in VALID_CATEGORIES:
        errors.append(f"invalid category {cat!r}; valid: {VALID_CATEGORIES}")
    mode = sc.get("mode", "fixed")
    if mode not in VALID_MODES:
        errors.append(f"invalid mode {mode!r}; valid: {sorted(VALID_MODES)}")
    turns = sc.get("turns")
    if isinstance(turns, list):
        if len(turns) == 0:
            errors.append("turns must contain at least one turn")
        else:
            for i, t in enumerate(turns):
                if not isinstance(t, dict) or not t.get("input"):
                    errors.append(f"turn {i} missing required 'input'")
    elif turns is not None:
        errors.append("turns must be an array")
    return errors


def load_dataset(path: str) -> tuple[list[dict], list[dict]]:
    """Load + validate a dataset file. Returns (valid_scenarios, errors).

    ``errors`` is a list of {scenario_id, errors} for skipped scenarios.
    """
    data = json.loads(Path(path).read_text())
    scenarios = data.get("scenarios", []) if isinstance(data, dict) else []
    valid: list[dict] = []
    errs: list[dict] = []
    for i, sc in enumerate(scenarios):
        e = validate_scenario(sc)
        if e:
            errs.append({"scenario_id": sc.get("scenario_id", f"index-{i}"), "errors": e})
        else:
            valid.append(sc)
    return valid, errs


def filter_scenarios(scenarios: list[dict], category: str | None = None,
                     scenario_id: str | None = None, mode: str | None = None) -> list[dict]:
    out = scenarios
    if category:
        out = [s for s in out if s.get("category") == category]
    if scenario_id:
        out = [s for s in out if s.get("scenario_id") == scenario_id]
    if mode:
        out = [s for s in out if s.get("mode", "fixed") == mode]
    return out
