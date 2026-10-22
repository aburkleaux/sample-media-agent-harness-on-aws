"""A render tab must never be titled with a raw job/request id. A fal submit_job node carried only
its request_id and no tool name, so the tab read "019feeea F88c 71b3 927e 13e752299e46". Normalize
an empty/id-like label to the media kind ("Video"/"Image"/"Audio")."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import render.artifacts as ra


def test_request_id_forms_are_detected_as_ids():
    assert ra._looks_like_raw_id("019feeea-f88c-71b3-927e-13e752299e46")
    assert ra._looks_like_raw_id("019feeea F88c 71b3 927e 13e752299e46")  # titleized
    assert ra._looks_like_raw_id("arn:aws:states:us-west-2:1:execution:sm:exec")
    assert ra._looks_like_raw_id("")
    assert ra._looks_like_raw_id(None)


def test_real_labels_are_kept():
    assert not ra._looks_like_raw_id("Shot Segmentation")
    assert not ra._looks_like_raw_id("Recognize Faces")
    assert not ra._looks_like_raw_id("Video")


def test_media_kind_label_from_url():
    assert ra._media_kind_label(["https://v3b.fal.media/x/y_video.mp4?X-Amz=z"]) == "Video"
    assert ra._media_kind_label(["https://cdn/cat.png"]) == "Image"
    assert ra._media_kind_label(["https://cdn/track.wav"]) == "Audio"
    assert ra._media_kind_label(["https://cdn/report.pdf"]) == "Document"
    assert ra._media_kind_label(["https://cdn/thing.bin"]) == "Result"
