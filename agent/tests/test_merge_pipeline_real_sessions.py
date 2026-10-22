"""End-to-end LOCAL guard for the render-merge contract, keyed on the ACTUAL session shapes we
debugged on staging (so we validate here, not by burning CI). Each case asserts what sourceKey
`derive_source_key` stamps, and therefore which renders the UI (mergeServerRenders/groupBySourceKey)
will fold into ONE composite vs leave solo.

The single rule: every render ABOUT the same source media must produce the SAME sourceKey, whether
it is the source's own video, a plain deliverable, or an analysis — regardless of which storage
root (durable artifacts vs ephemeral micro-workflow) or which copy it ran on.
"""
import render_agent as ra
from render.review import _norm_url


def sk(source_video, media, *, analysis):
    return ra.derive_source_key(source_video, media, [], has_analysis=analysis)


def test_cf3c0ffe_trim_clip_video_and_shots_share_one_key():
    ex = "0da9b6c6-1756-5c88-bae6-4d6cea04bf10"
    clip = sk([], [f"https://cdn/artifacts/cf3c0ffe-46d9-4e96-a619-041c3d024382/output/{ex}/meridian_trim.mp4"], analysis=False)
    shots = sk([f"https://cdn/micro-workflow/media-processing/{ex}/trim_clip/meridian_trim.mp4"], ["https://cdn/shots.json"], analysis=True)
    assert clip == shots, (clip, shots)  # -> ONE composite tab (video + shots lane)


def test_adc293de_clip_analyses_all_share_the_clip_key():
    # stems (audio, no markers), shots (markers), faces (annotations), diarization (audio+captions)
    ex = "aaaaaaaa-1111-2222-3333-444444444444"
    clip = sk([], [f"https://cdn/artifacts/s/output/{ex}/transcoded.mp4"], analysis=False)
    stems = sk([f"https://cdn/micro-workflow/media-processing/{ex}/proxy/transcoded.mp4"], ["https://cdn/audio_speech.wav"], analysis=False)
    shots = sk([f"https://cdn/micro-workflow/media-processing/{ex}/proxy/transcoded.mp4"], ["https://cdn/segment_0000.mp4"], analysis=True)
    faces = sk([f"https://cdn/micro-workflow/media-processing/{ex}/proxy/transcoded.mp4"], ["https://cdn/faces.json"], analysis=True)
    assert clip == stems == shots == faces, (clip, stems, shots, faces)


def test_6dffd3ec_extract_audio_keys_to_the_original_not_a_new_tab():
    """The failure the user saw: extract-audio on the ORIGINAL must key to the original video, so
    IF the original is rendered it folds together. (The remaining gap is that the uploaded original
    must ALSO be a server render — asserted separately below.)"""
    sid = "6dffd3ec-52bf-4557-b021-873ca7d5a0ce"
    original_video = sk([], [f"https://cdn/artifacts/{sid}/input/1787848608768-Netflix_Open_Content_Meridian.mp4"], analysis=False)
    extracted_audio = sk([f"https://cdn/artifacts/{sid}/input/1787848608768-Netflix_Open_Content_Meridian.mp4"], ["https://cdn/meridian_audio.wav"], analysis=False)
    assert original_video == extracted_audio, (original_video, extracted_audio)


def test_original_and_a_derived_clip_are_DIFFERENT_sources():
    sid = "6dffd3ec-52bf-4557-b021-873ca7d5a0ce"
    ex = "41f86216-608d-5057-991a-365a29d0b5f7"
    original = sk([], [f"https://cdn/artifacts/{sid}/input/x-meridian.mp4"], analysis=False)
    clip = sk([], [f"https://cdn/artifacts/{sid}/output/{ex}/meridian_trim.mp4"], analysis=False)
    assert original != clip  # clip is its own tab; analyses of each land on the right one


def test_metadata_probe_key_is_not_a_video_tab():
    # a lone probe json has no video/source -> keys to itself, never a source-video tab
    k = sk([], ["https://cdn/micro-workflow/media-processing/eeeeeeee-1111-2222-3333-444444444444/probe/metadata.json"], analysis=False)
    assert "metadata.json" in k
