"""The render agent loads skills through the SAME SkillsManager path as every other agent, but
scoped to `render-` so it exposes ONLY render skills (the consolidated render-media bundle),
never skill-creator/video-processing/etc. Pinned so a change to create_plugin that drops the
name_prefix scoping fails here."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from skills_manager import SkillsManager

SKILLS = pathlib.Path(__file__).resolve().parents[1] / "skills"


def test_render_scoping_yields_only_render_media():
    mgr = SkillsManager(skills_dir=SKILLS)
    mgr.create_plugin(name_prefix="render-")
    names = sorted(getattr(s, "name", "") for s in (mgr._shared_skills or []))
    assert names == ["render-media"], names  # consolidated to ONE render skill


def test_unfiltered_still_loads_all_skills():
    mgr = SkillsManager(skills_dir=SKILLS)
    mgr.create_plugin()  # main-agent behavior: no prefix -> everything
    names = {getattr(s, "name", "") for s in (mgr._shared_skills or [])}
    assert "render-media" in names
    assert len(names) > 1  # non-render skills present too


def test_render_media_is_the_only_render_skill_folder():
    render_dirs = sorted(p.name for p in SKILLS.iterdir()
                         if p.is_dir() and p.name.startswith("render-"))
    assert render_dirs == ["render-media"]
    refs = sorted(p.stem for p in (SKILLS / "render-media" / "references").glob("*.md"))
    assert set(refs) >= {"shot-segmentation", "face-recognition", "face-people",
                         "speaker-diarization", "audio", "video", "document"}
