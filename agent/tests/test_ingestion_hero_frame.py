"""Sampled video hero image (ingestion/hero_frame.py, handlers/hero_frame.py, the Dispatcher fan-out)."""
from __future__ import annotations

import io
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import hero_frame  # noqa: E402
from ingestion.handlers import dispatcher as dispatcher_mod  # noqa: E402
from ingestion.handlers.hero_frame import HeroFrameService  # noqa: E402

PID = "p" + "a" * 32
VID = "video:11111111-1111-4111-8111-111111111111"


def solid(value: int, size=(64, 36)) -> Image.Image:
    return Image.new("RGB", size, (value, value, value))


def textured(size=(64, 36)) -> Image.Image:
    im = Image.new("RGB", size)
    im.putdata([((x * 37) % 256, (y * 53) % 256, ((x + y) * 11) % 256) for y in range(size[1]) for x in range(size[0])])
    return im


# ------------------------------------------------------------------------------------------------ pick
def test_pick_takes_first_usable_point():
    seen = []

    def sample(f):
        seen.append(f)
        return textured()

    _, frac = hero_frame.pick(sample)
    assert frac == 0.10 and seen == [0.10]


def test_pick_skips_black_and_flat_frames():
    frames = {0.10: solid(0), 0.25: solid(128), 0.50: textured()}
    _, frac = hero_frame.pick(frames.get)
    assert frac == 0.50


def test_pick_falls_back_to_liveliest_when_none_usable():
    frames = {0.10: solid(0), 0.25: solid(5), 0.50: None}
    image, frac = hero_frame.pick(frames.get)
    assert frac in (0.10, 0.25) and image is not None


def test_pick_raises_when_nothing_decodes():
    with pytest.raises(hero_frame.HeroFrameError):
        hero_frame.pick(lambda f: None)


def test_encode_never_upscales_and_caps_long_side():
    small, w, h = hero_frame.encode(textured((320, 180)))
    assert (w, h) == (320, 180) and small[:2] == b"\xff\xd8"
    _, w, h = hero_frame.encode(textured((1920, 1080)))
    assert (w, h) == (960, 540)


# ------------------------------------------------------------------------------------------- range reader
class FakeS3:
    def __init__(self, blobs: dict[tuple[str, str], bytes]):
        self.blobs, self.gets, self.puts = blobs, [], []

    def get_object(self, Bucket, Key, Range=None):
        data = self.blobs[(Bucket, Key)]
        start, end = (int(x) for x in Range[len("bytes="):].split("-"))
        self.gets.append((start, end))
        return {"Body": io.BytesIO(data[start:end + 1])}

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.blobs[(Bucket, Key)])}

    def put_object(self, **kw):
        self.puts.append(kw)


def test_range_reader_reads_seeks_and_caches_blocks():
    data = bytes(range(256)) * 20_000  # ~5 MB, spans several 1 MiB blocks
    s3 = FakeS3({("b", "k"): data})
    r = hero_frame.S3RangeReader(s3, "b", "k", len(data))
    assert r.read(10) == data[:10]
    r.seek(hero_frame.BLOCK - 5)
    assert r.read(10) == data[hero_frame.BLOCK - 5:hero_frame.BLOCK + 5]  # crosses a block boundary
    r.seek(-3, io.SEEK_END)
    assert r.read(100) == data[-3:]
    assert r.read(1) == b""
    gets = len(s3.gets)
    r.seek(0)
    r.read(10)
    assert len(s3.gets) == gets  # block 0 is cached
    assert r.bytes_fetched < len(data)


# --------------------------------------------------------------------------------------- real decode
def make_video(seconds: int = 4, fps: int = 10) -> bytes:
    """An MP4 whose first 20% is black and the rest a moving texture — so 10% is dark and 25% is not."""
    av = pytest.importorskip("av")
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mp4") as out:
        st = out.add_stream("libx264" if "libx264" in av.codecs_available else "mpeg4", rate=fps)
        st.width, st.height, st.pix_fmt = 160, 90, "yuv420p"
        st.options = {"g": str(fps)}  # a keyframe every second, so a seek lands on content
        total = seconds * fps
        for i in range(total):
            im = solid(0, (160, 90)) if i < total * 0.2 else textured((160, 90)).rotate(i % 360)
            for pkt in st.encode(av.VideoFrame.from_image(im)):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)
    return buf.getvalue()


def test_extract_from_real_video_skips_black_open():
    data = make_video()
    s3 = FakeS3({("b", "v.mp4"): data})
    frame = hero_frame.extract(hero_frame.S3RangeReader(s3, "b", "v.mp4", len(data)))
    assert frame.jpeg[:2] == b"\xff\xd8" and (frame.width, frame.height) == (160, 90)
    assert frame.at_s >= 0.9  # the 10% point (0.4 s) is black; 25% (1.0 s) is content


def test_extract_rejects_non_video():
    pytest.importorskip("av")
    s3 = FakeS3({("b", "x"): b"not a video at all" * 100})
    with pytest.raises(hero_frame.HeroFrameError):
        hero_frame.extract(hero_frame.S3RangeReader(s3, "b", "x", 1800))


# ---------------------------------------------------------------------------------------------- handler
class FakeTable:
    def __init__(self, item=None, fail_condition=False):
        self.item, self.fail_condition, self.updates = item, fail_condition, []

    def get_item(self, Key, ConsistentRead=False):
        return {"Item": self.item} if self.item else {}

    def update_item(self, **kw):
        if self.fail_condition:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.updates.append(kw)


def _row(**extra):
    return {"PK": f"PROJECT#{PID}", "SK": f"ASSET#{VID}", "assetId": VID, "kind": "video",
            "sourceUri": "s3://src/v.mp4", **extra}


def _stub_extract(monkeypatch):
    monkeypatch.setattr(hero_frame, "extract",
                        lambda r: hero_frame.HeroFrame(jpeg=b"\xff\xd8jpeg", width=960, height=540, at_s=1.5))


def test_handler_writes_rendition_conditionally(monkeypatch):
    _stub_extract(monkeypatch)
    s3, table = FakeS3({("src", "v.mp4"): b"x" * 10}), FakeTable(_row())
    out = HeroFrameService(table, s3, "ingest").handle({"pid": PID, "assetId": VID})
    key = f"projects/{PID}/{VID}/rendition.jpg"
    assert out["results"][0]["key"] == key
    assert s3.puts[0]["Bucket"] == "ingest" and s3.puts[0]["Key"] == key
    upd = table.updates[0]
    assert "attribute_not_exists(rendition)" in upd["ConditionExpression"]
    assert upd["ExpressionAttributeValues"][":r"] == {"key": key, "width": 960, "height": 540, "bytes": 6,
                                                      "sampledAtMs": 1500}


def test_handler_skips_existing_rendition_and_non_video(monkeypatch):
    _stub_extract(monkeypatch)
    svc = HeroFrameService(FakeTable(_row(rendition={"key": "k"})), FakeS3({}), "ingest")
    assert svc.one(PID, VID)["skipped"] == "already has a rendition"
    assert svc.one(PID, "image:11111111-1111-4111-8111-111111111111")["skipped"] == "not a video"


def test_handler_lost_race_is_a_skip(monkeypatch):
    _stub_extract(monkeypatch)
    svc = HeroFrameService(FakeTable(_row(), fail_condition=True), FakeS3({("src", "v.mp4"): b"x"}), "ingest")
    assert svc.one(PID, VID)["skipped"] == "rendition appeared meanwhile"


def test_handler_reads_the_objects_current_size_not_the_stale_row(monkeypatch):
    seen = {}

    def fake_extract(reader):
        seen["size"] = reader._size
        return hero_frame.HeroFrame(jpeg=b"\xff\xd8x", width=1, height=1, at_s=0)
    monkeypatch.setattr(hero_frame, "extract", fake_extract)
    s3 = FakeS3({("src", "v.mp4"): b"x" * 2000})  # rewritten in place: now 2000 bytes
    HeroFrameService(FakeTable(_row(sourceSize=900)), s3, "ingest").one(PID, VID)
    assert seen["size"] == 2000


def test_handler_failure_is_returned_not_raised(monkeypatch):
    def boom(r):
        raise hero_frame.HeroFrameError("the file has no video stream")
    monkeypatch.setattr(hero_frame, "extract", boom)
    s3, table = FakeS3({("src", "v.mp4"): b"x"}), FakeTable(_row())
    out = HeroFrameService(table, s3, "ingest").one(PID, VID)
    assert out["failed"] == "the file has no video stream" and not s3.puts and not table.updates


# ------------------------------------------------------------------------------------------- dispatcher
def _insert(sk, **img):
    return {"eventName": "INSERT", "dynamodb": {"NewImage": {"PK": {"S": f"PROJECT#{PID}"}, "SK": {"S": sk}, **img}}}


def test_new_videos_selects_video_asset_inserts_without_rendition():
    recs = [_insert(f"ASSET#{VID}"), _insert("ASSET#image:22222222-2222-4222-8222-222222222222"),
            _insert("QUEUE#1#x"), _insert(f"ASSET#{VID}", rendition={"M": {}}),
            {"eventName": "MODIFY", "dynamodb": {"NewImage": _insert(f"ASSET#{VID}")["dynamodb"]["NewImage"]}}]
    assert dispatcher_mod.new_videos(recs) == [(PID, VID)]


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


class NullStore:
    def get_meta(self, pid):
        return None


def test_dispatcher_async_invokes_hero_frame_per_project():
    lam = FakeLambda()
    d = dispatcher_mod.Dispatcher(NullStore(), None, "arn", lambda_client=lam, hero_frame_fn="hero-fn")
    out = d.handle({"Records": [_insert(f"ASSET#{VID}")]})
    assert out["heroFrames"] == 1 and out["started"] == 0
    call = lam.calls[0]
    assert call["FunctionName"] == "hero-fn" and call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"pid": PID, "assetIds": [VID]}


def test_dispatcher_without_hero_fn_is_unchanged():
    d = dispatcher_mod.Dispatcher(NullStore(), None, "arn")
    assert d.handle({"Records": [_insert(f"ASSET#{VID}")]}) == {"projects": 0, "started": 0}
