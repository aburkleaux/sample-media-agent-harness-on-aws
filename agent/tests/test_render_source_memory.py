"""Session-scoped source memory: an input seen as a source in ONE result must never be rendered
as an output by a LATER result that echoes it without the source role/key (the 60s-proxy job that
rendered its 300s original). Generic: keyed by normalized object key, no tool/type list."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import hooks.provider as p


def test_session_source_excluded_from_later_result():
    p.reset_dispatched_artifacts()
    s3_src = "s3://bkt/artifacts/1786453956902-nba.mp4"
    # 1) First result marks the original as a SOURCE (e.g. plan aggregate role=source).
    p.remember_source_urls([s3_src])
    # 2) A LATER result echoes the SAME object as a bare presigned https url (no source key).
    https_echo = "https://bkt.s3.amazonaws.com/artifacts/1786453956902-nba.mp4?AWSAccessKeyId=A&Signature=B&Expires=1"
    out = p.new_artifact_urls([https_echo], {https_echo: "video/mp4"}, source_urls=[])
    assert out == [], "a known session source must not render as an output on a later sighting"


def test_genuine_output_still_renders():
    p.reset_dispatched_artifacts()
    p.remember_source_urls(["s3://bkt/artifacts/orig.mp4"])
    proxy = "https://proxy.s3.amazonaws.com/mw/exec1/proxy/transcoded.mp4?X=1"
    out = p.new_artifact_urls([proxy], {proxy: "video/mp4"}, source_urls=[])
    assert out == [proxy], "a real output must still render"


def test_reset_clears_session_sources():
    p.remember_source_urls(["s3://bkt/x.mp4"])
    p.reset_dispatched_artifacts()
    u = "https://bkt.s3.amazonaws.com/x.mp4?s=1"
    assert p.new_artifact_urls([u], {u: "video/mp4"}) == [u]
