"""Direct timeline actions — Frame / Clip / Crop with NO model turn (design 2026-10-18 §1, re-assessed).

The user already said exactly what to do (times, ranges, a box), so a model turn adds 5–15 s and a failure mode
(guessing an ``s3://`` URI from a presigned URL) and nothing else. The proxy's ``POST /media/actions`` resolves
the source to a project asset of the chat's project, presigns it, and invokes the MAIN runtime with
``{"__media_action": {...}}`` under the chat's runtime session (so it lands in the warm microVM). This module
then runs ffmpeg deterministically in the code-interpreter SANDBOX — the same substrate ``tools/crop_video``
uses: isolated from the agent process, holding per-(project, session) scoped S3 credentials, and with the
bundled crop script as the single definition of the crop math.

What makes it fast, beyond skipping the model:
  * ONE warm sandbox per chat session, reused across actions (``_SANDBOXES``), recreated when it died;
  * ffmpeg provisioned once per sandbox — the bundled script's ``_ensure_ffmpeg_ffprobe`` finds the binary it
    already unpacked under the sandbox temp dir before downloading anything;
  * the source downloaded once per sandbox (cached by its ``s3://`` identity), so a Frame then a Clip of the same
    video reads it from local disk.

This path NEVER builds a Strands Agent or calls a model (a test pins it). Outputs go to the session OUTPUT prefix;
the proxy then sends the GeneratedMedia turn message that registers them as project assets with ``derivedFrom``
= the source asset.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

RESULT_TYPE = "media_action_result"
OPS = ("frame", "clip", "crop")
# Longer than this goes back to the agent / an M2C job (the UI falls back to the chat path).
MAX_RANGE_S = 120.0
MAX_POINTS = 20
MAX_RANGES = 10
# A warm sandbox lives this long; the per-call timeout below bounds one action.
SANDBOX_SESSION_TIMEOUT_S = 1800
ACTION_TIMEOUT_S = 240.0
# Scoped credentials are served with >= 45 min left; re-inject well before that runs out.
CREDENTIAL_REFRESH_S = 30 * 60

_SENTINEL = "MEDIA_ACTION_JSON:"


class MediaActionError(ValueError):
    """A request this path refuses (the UI falls back to the chat path)."""


# --------------------------------------------------------------------------------------------- request parsing

@dataclass
class MediaAction:
    action_id: str
    op: str
    source_url: str          # presigned GET the sandbox downloads
    source_uri: str          # canonical s3://bucket/key (cache identity + lineage)
    source_name: str
    asset_id: str = ""
    points_ms: list[int] = field(default_factory=list)
    ranges_ms: list[tuple[int, int]] = field(default_factory=list)
    box: Optional[tuple[float, float, float, float]] = None


_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def parse_action(raw: Any) -> MediaAction:
    """Validate the proxy's ``__media_action`` object. Raises :class:`MediaActionError` with a reason."""
    if not isinstance(raw, dict):
        raise MediaActionError("__media_action must be an object")
    op = raw.get("op")
    if op not in OPS:
        raise MediaActionError(f"op must be one of {', '.join(OPS)}")
    action_id = str(raw.get("action_id") or "")
    if not _ACTION_ID_RE.match(action_id):
        raise MediaActionError("action_id is required")
    src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    url, uri = str(src.get("url") or ""), str(src.get("s3uri") or "")
    if not url.startswith("https://") or not uri.startswith("s3://"):
        raise MediaActionError("source.url (https) and source.s3uri are required")
    act = MediaAction(action_id=action_id, op=op, source_url=url, source_uri=uri,
                      source_name=str(src.get("name") or uri.rsplit("/", 1)[-1]),
                      asset_id=str(src.get("asset_id") or ""))
    if op == "frame":
        pts = raw.get("points")
        if not isinstance(pts, list) or not pts:
            raise MediaActionError("frame needs points (ms)")
        if len(pts) > MAX_POINTS:
            raise MediaActionError(f"at most {MAX_POINTS} frames per action")
        act.points_ms = [_ms(p, "points") for p in pts]
    else:
        rng = raw.get("ranges") or []
        if not isinstance(rng, list) or len(rng) > MAX_RANGES:
            raise MediaActionError(f"at most {MAX_RANGES} ranges per action")
        for r in rng:
            if not isinstance(r, dict):
                raise MediaActionError("each range is {inMs, outMs}")
            a, b = _ms(r.get("inMs"), "inMs"), _ms(r.get("outMs"), "outMs")
            if b <= a:
                raise MediaActionError("a range must end after it starts")
            # Only a CROP re-encodes; a clip is a stream copy (~0.1 s at any length), so it has no length cap.
            if op == "crop" and (b - a) / 1000.0 > MAX_RANGE_S:
                raise MediaActionError(f"crops longer than {int(MAX_RANGE_S)} s go through the agent")
            act.ranges_ms.append((a, b))
        if op == "clip" and not act.ranges_ms:
            raise MediaActionError("clip needs at least one range")
        if op == "crop":
            box = raw.get("box")
            if not isinstance(box, dict):
                raise MediaActionError("crop needs box {x,y,w,h} as 0..1 fractions")
            try:
                vals = tuple(float(box[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError) as exc:
                raise MediaActionError("crop box needs numeric x, y, w, h") from exc
            if not all(0.0 <= v <= 1.0 for v in vals) or vals[2] <= 0 or vals[3] <= 0:
                raise MediaActionError("crop box values are fractions of the frame (0..1), w and h > 0")
            act.box = vals  # type: ignore[assignment]
            if len(act.ranges_ms) > 1:
                raise MediaActionError("crop takes at most one range")
    return act


def _ms(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise MediaActionError(f"{name} must be a non-negative number of milliseconds")
    return int(round(v))


# --------------------------------------------------------------------------------------------- output naming

def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    base = base.rsplit(".", 1)[0] if "." in base else base
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    return (safe or "media")[:60]


def _tc(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}{s % 60:02d}"


def plan_outputs(act: MediaAction, prefix: str) -> list[dict]:
    """The deterministic jobs the sandbox runs, one per output, with their keys under ``prefix``."""
    stem, tag = _stem(act.source_name), act.action_id[-6:]
    jobs: list[dict] = []
    if act.op == "frame":
        for i, p in enumerate(act.points_ms):
            jobs.append({"kind": "image", "at": p / 1000.0,
                         "key": f"{prefix}{stem}_frame_{_tc(p)}_{p % 1000:03d}_{tag}{i}.jpg"})
    elif act.op == "clip":
        for i, (a, b) in enumerate(act.ranges_ms):
            jobs.append({"kind": "video", "in": a / 1000.0, "out": b / 1000.0,
                         "key": f"{prefix}{stem}_clip_{_tc(a)}-{_tc(b)}_{tag}{i}.mp4"})
    else:
        trim = act.ranges_ms[0] if act.ranges_ms else None
        jobs.append({"kind": "video", "box": list(act.box or ()),
                     "in": trim[0] / 1000.0 if trim else None, "out": trim[1] / 1000.0 if trim else None,
                     "key": f"{prefix}{stem}_crop_{tag}.mp4"})
    return jobs


# --------------------------------------------------------------------------------------------- sandbox program

# Appended after the bundled crop script (its helpers: _ensure_ffmpeg_ffprobe, probe_dimensions, probe_has_audio,
# resolve_crop). Every parameter arrives as a JSON literal. The source is cached per sandbox by its s3 identity.
_RUNNER = r'''
import os, json, hashlib, subprocess, tempfile, urllib.request

# The staged ffmpeg is read with the sandbox's IAM role (tools/ffmpeg/* grant), never via a public link.
if _BUCKET and not os.environ.get("FFMPEG_S3_URI") and not os.environ.get("FFMPEG_STATIC_URL"):
    os.environ["FFMPEG_S3_URI"] = "s3://" + _BUCKET + "/tools/ffmpeg"
if _REGION:
    os.environ.setdefault("AWS_REGION", _REGION)

def _cached_source():
    root = os.path.join(tempfile.gettempdir(), "media-src")
    os.makedirs(root, exist_ok=True)
    ext = os.path.splitext(_SRC_URI)[1][:6] or ".mp4"
    path = os.path.join(root, hashlib.sha256(_SRC_URI.encode()).hexdigest()[:32] + ext)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        tmp = path + ".part"
        urllib.request.urlretrieve(_SRC_URL, tmp)  # public egress; ffmpeg then reads LOCAL
        os.replace(tmp, path)
        return path, False
    return path, True

def _copyable(ffprobe, path):
    """Whether a clip can be a stream copy into MP4: H.264/HEVC video, and AAC/MP3 audio or none."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", path],
                           capture_output=True, text=True, timeout=30)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except Exception:  # noqa: BLE001 - unknown: re-encode, the safe path
        return False
    v = [x.get("codec_name") for x in streams if x.get("codec_type") == "video"]
    a = [x.get("codec_name") for x in streams if x.get("codec_type") == "audio"]
    return bool(v) and v[0] in ("h264", "hevc") and (not a or a[0] in ("aac", "mp3"))

_out = {"error": "media action did not run"}
try:
    import boto3
    _s3 = boto3.client("s3", region_name=_REGION)  # per cell: credentials can be re-injected between cells
    _ffmpeg, _ffprobe = _ensure_ffmpeg_ffprobe()
    _local, _hit = _cached_source()
    _outs = []
    with tempfile.TemporaryDirectory() as _td:
        for _i, _j in enumerate(_JOBS):
            _dst = os.path.join(_td, "out%d%s" % (_i, ".jpg" if _j["kind"] == "image" else ".mp4"))
            if _j["kind"] == "image":
                _cmd = [_ffmpeg, "-y", "-ss", str(_j["at"]), "-i", _local, "-frames:v", "1", "-q:v", "2", _dst]
                _ct = "image/jpeg"
            elif not _j.get("box") and _copyable(_ffprobe, _local):
                # A plain clip is a STREAM COPY: no re-encode, ~0.1 s for any length. The input seek lands on the
                # keyframe before `in`, and the MP4 edit list ffmpeg writes hides that pre-roll, so playback starts
                # on the exact marked frame (measured: the first shown frame is the source frame at `in`).
                _cmd = [_ffmpeg, "-y", "-ss", str(_j["in"]), "-i", _local, "-t", str(_j["out"] - _j["in"]),
                        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", _dst]
                _ct = "video/mp4"
            else:
                _cmd = [_ffmpeg, "-y"]
                if _j.get("in") is not None:
                    _cmd += ["-ss", str(_j["in"])]
                _cmd += ["-i", _local]
                if _j.get("in") is not None and _j.get("out") is not None:
                    _cmd += ["-t", str(_j["out"] - _j["in"])]
                if _j.get("box"):
                    _w, _h = probe_dimensions(_ffprobe, _local)
                    _c = resolve_crop(tuple(_j["box"]), "normalized", _w, _h)
                    _cmd += ["-vf", "crop=%d:%d:%d:%d" % (_c["w"], _c["h"], _c["x"], _c["y"])]
                # Crop (and a source that cannot be copied) re-encodes; superfast keeps a 10 s crop to a few seconds.
                _cmd += ["-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "0"]
                _cmd += ["-c:a", "aac"] if probe_has_audio(_ffprobe, _local) else ["-an"]
                _cmd += ["-movflags", "+faststart", _dst]
                _ct = "video/mp4"
            _r = subprocess.run(_cmd, capture_output=True, text=True, timeout=_TIMEOUT)
            if _r.returncode != 0 or not os.path.isfile(_dst):
                raise RuntimeError("ffmpeg failed: " + (_r.stderr or "")[-600:].strip())
            with open(_dst, "rb") as _fh:
                _s3.put_object(Bucket=_BUCKET, Key=_j["key"], Body=_fh.read(), ContentType=_ct)
            _outs.append({"key": _j["key"], "kind": _j["kind"]})
    _out = {"ok": True, "outputs": _outs, "sourceCached": _hit}
except Exception as _e:  # noqa: BLE001 - the real cause goes back to the caller
    _out = {"error": str(_e)[:800] or repr(_e)}

print(_SENTINEL_TOKEN + json.dumps(_out))
'''


def build_sandbox_code(script_src: str, act: MediaAction, jobs: list[dict], *, bucket: str, domain: str,
                       region: str, timeout: float = ACTION_TIMEOUT_S) -> str:
    """The bundled crop script (as a library) + the injected params + the runner."""
    from tools.crop_video import _as_library

    params = {
        "_SRC_URL": act.source_url, "_SRC_URI": act.source_uri, "_JOBS": jobs, "_BUCKET": bucket,
        "_DOMAIN": domain, "_REGION": region, "_TIMEOUT": float(timeout), "_SENTINEL_TOKEN": _SENTINEL,
    }
    # Each value as json.loads of a JSON string literal: JSON is NOT Python (`null` / `true` raised NameError in the
    # sandbox for a crop with no time range, whose jobs carry "in": null), and a quoted JSON string always is.
    header = "import json as _json\n" + "\n".join(f"{k} = _json.loads({json.dumps(json.dumps(v))})" for k, v in params.items())
    return f"{_as_library(script_src)}\n\n# --- injected params + runner ---\n{header}\n{_RUNNER}"


# --------------------------------------------------------------------------------------------- warm sandboxes

@dataclass
class _Sandbox:
    ci: Any
    started_at: float
    creds_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_SANDBOXES: dict[str, _Sandbox] = {}
_REGISTRY_LOCK = threading.Lock()


def _new_code_interpreter():  # seam for tests
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    ci = CodeInterpreter(os.environ.get("AWS_REGION", "us-west-2"))
    ci.start(identifier=os.environ.get("CODE_INTERPRETER_ID") or "aws.codeinterpreter.v1",
             session_timeout_seconds=SANDBOX_SESSION_TIMEOUT_S)
    return ci


def _inject_credentials(ci, project_id: Optional[str], session_id: str) -> None:
    """Scoped per-(project, session) S3 credentials into the sandbox, out of band (Phase 3 (b)).

    FAILS CLOSED: raises ``SandboxCredentialsError`` where the scoped role is configured and they could
    not be installed, so the action never runs on the sandbox's own role (no artifact S3 since B2)."""
    import sandbox_credentials as sc

    if not sc.required():
        return
    sc.install(ci.invoke, project_id, session_id)


def _sandbox_for(session_id: str) -> _Sandbox:
    with _REGISTRY_LOCK:
        box = _SANDBOXES.get(session_id)
        if box is not None and time.time() - box.started_at < SANDBOX_SESSION_TIMEOUT_S - ACTION_TIMEOUT_S - 30:
            return box
        if box is not None:
            _stop_quietly(box.ci)
        box = _Sandbox(ci=_new_code_interpreter(), started_at=time.time())
        _SANDBOXES[session_id] = box
        return box


def _discard(session_id: str, box: _Sandbox) -> None:
    with _REGISTRY_LOCK:
        if _SANDBOXES.get(session_id) is box:
            del _SANDBOXES[session_id]
    _stop_quietly(box.ci)


def _stop_quietly(ci) -> None:
    try:
        ci.stop()
    except Exception:  # noqa: BLE001 - best effort
        pass


def run_in_warm_sandbox(session_id: str, project_id: Optional[str], code: str) -> str:
    """Run ``code`` in this chat's warm sandbox (one retry on a fresh one if the warm one died)."""
    from tools.crop_video import _extract_stdout

    for attempt in (0, 1):
        box = _sandbox_for(session_id)
        try:
            with box.lock:
                if time.time() - box.creds_at > CREDENTIAL_REFRESH_S:
                    _inject_credentials(box.ci, project_id, session_id)
                    box.creds_at = time.time()
                return _extract_stdout(box.ci.execute_code(code))
        except Exception:  # noqa: BLE001 - an expired / dead sandbox: replace it once
            _discard(session_id, box)
            if attempt:
                raise
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------------------------- the action

def _output_prefix(session_id: str, project_id: Optional[str]) -> str:
    from projects.storage_layout import session_output_prefix

    safe = session_id.replace("/", "_").strip()
    return session_output_prefix(safe, project_id, artifacts_prefix=os.environ.get("ARTIFACTS_PREFIX", "artifacts/"))


def _script_source() -> str:
    from tools.crop_video import _script_source as src

    return src()


def run_action(act: MediaAction, *, session_id: str, project_id: Optional[str]) -> dict:
    """Run one action and return the ``media_action_result`` frame (never raises)."""
    started = time.monotonic()
    bucket = os.environ.get("SKILLS_S3_BUCKET", "")
    domain = os.environ.get("ARTIFACTS_DOMAIN", "")
    region = os.environ.get("AWS_REGION", "us-west-2")
    try:
        if not bucket:
            raise MediaActionError("SKILLS_S3_BUCKET is not configured")
        jobs = plan_outputs(act, _output_prefix(session_id, project_id))
        code = build_sandbox_code(_script_source(), act, jobs, bucket=bucket, domain=domain, region=region)
        raw = run_in_warm_sandbox(session_id, project_id, code)
        line = next((ln for ln in raw.splitlines() if ln.startswith(_SENTINEL)), None)
        if line is None:
            raise RuntimeError(f"the sandbox produced no result: {raw[-300:].strip()}")
        res = json.loads(line[len(_SENTINEL):])
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "the sandbox reported a failure")
        outputs = []
        for o in res.get("outputs") or []:
            key = o["key"]
            outputs.append({"kind": o["kind"], "name": key.rsplit("/", 1)[-1], "key": key,
                            "s3uri": f"s3://{bucket}/{key}",
                            "url": f"https://{domain}/{key}" if domain else key})
        return {"type": RESULT_TYPE, "ok": True, "action_id": act.action_id, "op": act.op, "outputs": outputs,
                "source": {"s3uri": act.source_uri, "asset_id": act.asset_id, "name": act.source_name},
                "sourceCached": bool(res.get("sourceCached")),
                "elapsedMs": int((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - one JSON frame either way
        logger.warning("media action %s (%s) failed: %s", act.action_id, act.op, exc)
        return {"type": RESULT_TYPE, "ok": False, "action_id": act.action_id, "op": act.op,
                "error": str(exc)[:500] or type(exc).__name__,
                "elapsedMs": int((time.monotonic() - started) * 1000)}


async def handle(payload: dict):
    """The ``__media_action`` entrypoint branch: one JSON frame, no model, no Agent."""
    import asyncio

    from jobs import context as jobs_context
    from projects.ids import is_project_id

    try:
        act = parse_action(payload.get("__media_action"))
    except MediaActionError as exc:
        yield json.dumps({"type": RESULT_TYPE, "ok": False, "error": str(exc)})
        return
    session_id = str(payload.get("session_id") or "")
    pid = payload.get("active_project_id")
    project_id = pid if isinstance(pid, str) and is_project_id(pid) else None
    if not session_id or project_id is None:
        yield json.dumps({"type": RESULT_TYPE, "ok": False, "action_id": act.action_id,
                          "error": "a session with an active project is required"})
        return
    # The proxy verified the actor's access to this project (it resolved the source as one of its assets).
    tokens = jobs_context.set_request_context(actor_id=str(payload.get("__actor_id") or ""), session_id=session_id)
    ptoken = jobs_context.set_producing_project_id(project_id)
    try:
        frame = await asyncio.to_thread(run_action, act, session_id=session_id, project_id=project_id)
    finally:
        jobs_context.reset_producing_project_id(ptoken)
        jobs_context.reset_request_context(tokens)
    yield json.dumps(frame)


def _reset_for_tests() -> None:
    with _REGISTRY_LOCK:
        _SANDBOXES.clear()


# Exposed for the warm-cache digest tests.
def source_cache_name(uri: str) -> str:
    return hashlib.sha256(uri.encode()).hexdigest()[:32]
