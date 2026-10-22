"""A video's sampled hero image: one frame grabbed at a fixed point, for a video that indexing never reached.

The indexing pipeline's poster (:mod:`ingestion.poster`) is the better frame, but it exists only after the whole
M2C chain has run — so a ``stored`` video (indexing off, a session upload, an M2C intermediate) never had one, and
a queued video showed a play icon for hours. This samples a frame instead: seek to 10% of the duration and take the
keyframe there; if it is near-black or flat (a fade, a title card), try 25% then 50%, else keep the liveliest.

The source is read through ranged S3 GETs (:class:`S3RangeReader`), so a 1 GB video costs a few MB of reads — the
container index plus the GOPs at the sample points — and no presigned URL or CDN hop is involved. Decoding is
PyAV (FFmpeg as a wheel), which only the HeroFrame Lambda's own package carries.

It writes the same ``projects/<pid>/<assetId>/rendition.jpg`` the poster uses, so the Asset_URL_Route signs it
unchanged; a later index run's poster simply replaces it.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

SAMPLE_POINTS: Sequence[float] = (0.10, 0.25, 0.50)
MAX_SIDE = 960
JPEG_QUALITY = 82
# A frame darker than this mean luma, or flatter than this luma spread, is a fade / black / title card.
DARK_MEAN = 18.0
FLAT_STDDEV = 10.0
BLOCK = 1 << 20  # 1 MiB ranged reads
MAX_BLOCKS = 48


class HeroFrameError(Exception):
    """No frame could be taken; the message is the reason."""


@dataclass(frozen=True)
class HeroFrame:
    jpeg: bytes
    width: int
    height: int
    at_s: float


class S3RangeReader(io.RawIOBase):
    """A seekable, read-only file over one S3 object, fetched in 1 MiB ranged GETs with a small block cache."""

    def __init__(self, s3: Any, bucket: str, key: str, size: int):
        super().__init__()
        self._s3, self._bucket, self._key, self._size = s3, bucket, key, int(size)
        self._pos = 0
        self._blocks: dict[int, bytes] = {}
        self.bytes_fetched = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + int(offset))
        return self._pos

    def _block(self, idx: int) -> bytes:
        blk = self._blocks.get(idx)
        if blk is None:
            start = idx * BLOCK
            end = min(self._size, start + BLOCK) - 1
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._key, Range=f"bytes={start}-{end}")
            blk = resp["Body"].read()
            self.bytes_fetched += len(blk)
            if len(self._blocks) >= MAX_BLOCKS:
                self._blocks.pop(next(iter(self._blocks)))
            self._blocks[idx] = blk
        return blk

    def readinto(self, buf: Any) -> int:
        n = min(len(buf), self._size - self._pos)
        if n <= 0:
            return 0
        out = memoryview(buf)
        done = 0
        while done < n:
            idx, off = divmod(self._pos, BLOCK)
            blk = self._block(idx)
            take = min(n - done, len(blk) - off)
            if take <= 0:
                break
            out[done:done + take] = blk[off:off + take]
            done += take
            self._pos += take
        return done


def frame_quality(image: Any) -> tuple[float, float]:
    """``(mean, stddev)`` of the frame's luma — a PIL image in, two numbers out."""
    from PIL import ImageStat

    stat = ImageStat.Stat(image.convert("L"))
    return float(stat.mean[0]), float(stat.stddev[0])


def usable(mean: float, stddev: float) -> bool:
    return mean >= DARK_MEAN and stddev >= FLAT_STDDEV


def encode(image: Any) -> tuple[bytes, int, int]:
    """Scale (never up) so the longer side is ≤ :data:`MAX_SIDE`; baseline JPEG."""
    from PIL import Image

    im = image.convert("RGB")
    w, h = im.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), im.size[0], im.size[1]


def pick(sample: Callable[[float], Optional[Any]], points: Sequence[float] = SAMPLE_POINTS) -> tuple[Any, float]:
    """The first usable frame over ``points`` (fractions of the duration), else the liveliest one seen.

    ``sample(fraction)`` returns a PIL image or ``None`` (nothing decodable there).
    """
    best: Optional[tuple[float, Any, float]] = None
    for frac in points:
        image = sample(frac)
        if image is None:
            continue
        mean, stddev = frame_quality(image)
        if usable(mean, stddev):
            return image, frac
        if best is None or stddev > best[0]:
            best = (stddev, image, frac)
    if best is None:
        raise HeroFrameError("no frame could be decoded")
    return best[1], best[2]


def _rotated(image: Any, frame: Any) -> Any:
    """Apply the stream's display rotation (phone video) when PyAV reports one."""
    rot = int(getattr(frame, "rotation", 0) or 0) % 360
    return image.rotate(rot, expand=True) if rot else image


def extract(fileobj: Any) -> HeroFrame:
    """Sample a hero frame from an open, seekable video file object."""
    import av

    try:
        container = av.open(fileobj, mode="r")
    except Exception as exc:  # noqa: BLE001 — every open failure is the same outcome
        raise HeroFrameError(f"the video cannot be opened ({type(exc).__name__})") from exc
    with container:
        if not container.streams.video:
            raise HeroFrameError("the file has no video stream")
        stream = container.streams.video[0]
        duration_s = 0.0
        if container.duration:
            duration_s = container.duration / av.time_base
        elif stream.duration and stream.time_base:
            duration_s = float(stream.duration * stream.time_base)

        def sample(frac: float) -> Optional[Any]:
            at = duration_s * frac if duration_s > 0 else 0.0
            try:
                if at > 0 and stream.time_base:
                    container.seek(int(at / stream.time_base), stream=stream, backward=True, any_frame=False)
                for frame in container.decode(stream):
                    return _rotated(frame.to_image(), frame)
            except Exception:  # noqa: BLE001 — a bad GOP at one point; try the next point
                return None
            return None

        image, frac = pick(sample)
        jpeg, w, h = encode(image)
        return HeroFrame(jpeg=jpeg, width=w, height=h, at_s=round(duration_s * frac, 3))
