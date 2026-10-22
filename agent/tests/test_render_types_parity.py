"""The polling-worker lambda is a separate deploy unit, so render_types.py is
vendored there as a byte-identical copy. This test fails the build if the two
drift — the drift that once let .html render in one layer but not another."""
from pathlib import Path


def test_vendored_render_types_is_byte_identical():
    root = Path(__file__).resolve().parents[2]
    canonical = root / "agent" / "src" / "render" / "types.py"
    vendored = root / "infra" / "lib" / "lambdas" / "polling-worker" / "render_types.py"
    assert canonical.exists() and vendored.exists()
    assert canonical.read_text() == vendored.read_text(), (
        "polling-worker/render_types.py drifted from agent/src/render_types.py — "
        "copy the canonical file over after editing the registry."
    )
