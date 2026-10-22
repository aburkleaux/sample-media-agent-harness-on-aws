"""The hook names the render tab after the work that produced it.

The stream event that reaches the UI carries only a manifest key, so before
this the UI had no choice but to title every hook-driven tab "Render Output".
The job identity was available in the hook's event context all along — it just
was not forwarded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import actions  # noqa: E402


class TestRenderLabel:
    def test_federated_tool_name_titleizes_to_the_target(self):
        """`<target>___<tool>` is AgentCore's gateway naming convention."""
        assert actions._render_label(
            {"tool": {"name": "shot-segmentation___get_job_status"}}
        ) == "Shot Segmentation"

    def test_matches_the_shape_of_the_other_media_targets(self):
        cases = {
            "speech-diarization___diarize_speech": "Speech Diarization",
            "audio-stem___separate_audio_stems": "Audio Stem",
            "face-recognition___recognize_faces": "Face Recognition",
            "media-processing___process_media": "Media Processing",
        }
        for tool_name, expected in cases.items():
            assert actions._render_label({"tool": {"name": tool_name}}) == expected

    def test_non_federated_tool_uses_its_own_name(self):
        assert actions._render_label({"tool": {"name": "artifact"}}) == "Artifact"

    def test_underscored_name_is_split(self):
        assert actions._render_label(
            {"tool": {"name": "code_interpreter"}}) == "Code Interpreter"

    def test_missing_context_yields_empty_so_the_ui_can_number_it(self):
        assert actions._render_label({}) == ""
        assert actions._render_label({"tool": {}}) == ""
        assert actions._render_label({"tool": None}) == ""
        assert actions._render_label({"tool": {"name": ""}}) == ""


class TestRenderActionForwardsLabel:
    def _ctx(self, tool_name="shot-segmentation___get_job_status"):
        return {
            "tool": {"name": tool_name, "role": "status", "use_id": "u1"},
            "result": {"urls": ["https://x/shots.json"], "source_urls": []},
        }

    def test_label_accompanies_the_manifest_key(self, monkeypatch):
        seen = {}

        def fake_dispatch(urls, source_urls=None, label=None):
            seen["urls"] = urls
            seen["label"] = label
            return "artifacts/s/render/abc.json"

        monkeypatch.setattr("render.artifacts.dispatch_render", fake_dispatch)
        res = actions.render_action({}, self._ctx())
        assert seen["label"] == "Shot Segmentation"
        assert res.extra["render_manifest_key"] == "artifacts/s/render/abc.json"
        assert res.extra["render_label"] == "Shot Segmentation"

    def test_no_label_key_when_nothing_was_rendered(self, monkeypatch):
        monkeypatch.setattr("render.artifacts.dispatch_render",
                            lambda urls, source_urls=None, label=None: None)
        res = actions.render_action({}, self._ctx())
        assert res.status == "skipped"
        assert "render_label" not in res.extra
        assert "render_manifest_key" not in res.extra

    def test_unnameable_render_still_produces_a_manifest(self, monkeypatch):
        """An empty label must not block the render — the UI numbers it."""
        monkeypatch.setattr("render.artifacts.dispatch_render",
                            lambda urls, source_urls=None, label=None: "artifacts/s/render/x.json")
        res = actions.render_action({}, {"result": {"urls": ["https://x/a.json"]}})
        assert res.extra["render_manifest_key"] == "artifacts/s/render/x.json"
        assert "render_label" not in res.extra
