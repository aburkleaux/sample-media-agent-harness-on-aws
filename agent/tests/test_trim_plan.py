import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills" / "video-processing" / "scripts" / "trim_plan.py"
)
_spec = importlib.util.spec_from_file_location("trim_plan", _SCRIPT)
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


def test_pairs_by_position():
    plan = tp.build_trim_plan(["c1", "c2", "c3"], [5.17, 4.97, 6.61])
    assert [(p["clip"], p["target"]) for p in plan] == [("c1", 5.17), ("c2", 4.97), ("c3", 6.61)]
    assert [p["index"] for p in plan] == [0, 1, 2]


def test_count_mismatch_raises():
    with pytest.raises(ValueError, match="count mismatch"):
        tp.build_trim_plan(["c1", "c2"], [5.0])  # the off-by-one / misassignment class


def test_nonpositive_or_nonfinite_target_raises():
    with pytest.raises(ValueError):
        tp.build_trim_plan(["c1"], [0])
    with pytest.raises(ValueError):
        tp.build_trim_plan(["c1"], [float("inf")])


def test_verify_trim_passes_within_tol():
    tp.verify_trim(7, 6.61, 6.60)  # within default tol — no raise


def test_verify_trim_catches_overtrim():
    # shot 8: target 6.61 but trimmed to 5.17 (got shot 1's length) -> must raise, not ship short
    with pytest.raises(ValueError, match="trim mismatch"):
        tp.verify_trim(7, 6.61, 5.17)
