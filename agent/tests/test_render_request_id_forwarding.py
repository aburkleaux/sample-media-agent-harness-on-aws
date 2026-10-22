"""Root-cause regression for the "composing tab stuck beside a duplicate" bug.

A render dispatched inside the task agent pushes two channel events: a leading
marker (render_request_id, no manifest) and a manifest (render_manifest_key +
render_request_id). The task agent re-emits the manifest as an ``output_ref`` line
for the orchestrator. It used to keep only ``s3_key``/``label`` and DROP
``render_request_id`` — so the manifest reached the UI id-less, opened a second
tab, and left the marker's composing placeholder spinning forever. The marker,
being a plain step, kept its id — which is why only the manifest side lost it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orchestration.progress_channel import ProgressChannel  # noqa: E402
from task_agent import _drain_hook_lines  # noqa: E402


def test_output_ref_carries_render_request_id():
    ch = ProgressChannel()
    ch.push("running", "\u21b3 Composing render\u2026", agent="render",
            extra={"render_request_id": "R1", "render_label": "Run Model"})
    ch.push("done", "\u21b3 Rendered artifacts", agent="render",
            extra={"render_manifest_key": "artifacts/s/render/m.json",
                   "render_request_id": "R1", "render_label": "Run Model"})
    lines = [json.loads(l) for l in _drain_hook_lines(ch)]

    marker = next(l for l in lines if l.get("type") == "step" and "Composing" in (l.get("label") or ""))
    manifest = next(l for l in lines if l.get("type") == "output_ref")

    # The marker (a step) always kept its id.
    assert marker.get("render_request_id") == "R1"
    # The manifest (re-emitted as output_ref) now keeps its id — the fix. Same id
    # as the marker, so the UI fills the composing tab instead of appending.
    assert manifest.get("s3_key", "").endswith("m.json")
    assert manifest.get("render_request_id") == "R1"
    assert manifest.get("label") == "Run Model"
