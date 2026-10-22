"""First-class ``crop_video`` tool — crop a video to a bounding box, deterministically.

Why this is a TOOL, not a code_interpreter cell the model authors
-----------------------------------------------------------------
A crop is a deterministic operation, but routing it through model-authored code let the model
hand-roll ffmpeg AND reinterpret the box. In session efa3569d a 9:16 selection came back as a
540x540 SQUARE because the model substituted the aspect RATIO (9/16 = 0.5625) for the width
FRACTION and ran its own ffmpeg. Trim never had this class of bug because it is a typed tool call
(``process_media`` with ``mark_in_out=[a,b]`` — an M2C Step Functions job). This tool puts crop on
the same footing: the caller passes the normalized box VERBATIM and the deterministic math + ffmpeg
run without anything for the model to recompute.

Execution substrate: ffmpeg runs in the CODE INTERPRETER SANDBOX (not the agent runtime)
----------------------------------------------------------------------------------------
``process_media`` (M2C) cannot express an arbitrary ``(x,y,w,h)`` box (its crop is a top-left
offset + scale only), so crop is the one clip operation that must run ffmpeg outside M2C — until
M2C grows arbitrary-crop support. We run that ffmpeg in the AgentCore **code interpreter sandbox**,
NOT in the agent runtime, so we never provision a static ffmpeg into (or run subprocess load on)
the shared agent process. The sandbox is disposable, has public egress, and holds its own
``s3:PutObject`` grant on the skills bucket (``skillsBucket.grantReadWrite(codeInterpreter)``), so
it downloads the source, self-provisions ffmpeg, crops, and writes the result to the session
OUTPUT prefix itself (``projects.storage_layout.session_output_prefix``: ``artifacts/<session>/output/``,
or ``projects/<pid>/sessions/<session>/output/`` with the project layout on). The tool stays deterministic: it builds the exact sandbox
program (box passed VERBATIM, calling the bundled ``resolve_crop``) — the model never sees or
recomputes the geometry.

Why the source is downloaded first (inside the sandbox)
------------------------------------------------------
ffmpeg/ffprobe reading a remote HTTPS URL directly fails in these environments (the self-provisioned
static build cannot resolve/TLS a CloudFront URL — observed in sessions 54a9b4a7 / d1a7f019). Python
CAN fetch it, so the sandbox program downloads the source to a local file first, then ffprobe/ffmpeg
only ever touch a LOCAL path.

One implementation of the crop math
------------------------------------
The crop math + ffmpeg provisioning live in the bundled ``agent/skills/video-processing/scripts/
crop_video.py`` script, which is self-contained (it also runs in the code_interpreter sandbox). The
sandbox is a separate microVM and does NOT have ``agent/skills`` mounted, so this tool reads that
script's SOURCE and injects it into the sandbox program — ``resolve_crop`` therefore has exactly ONE
definition and can never silently diverge.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from strands import tool

logger = logging.getLogger(__name__)

# The bundled script, loaded lazily + cached. Named distinctly from this module.
_SCRIPT: Any = None
_SCRIPT_PATH: Optional[Path] = None

_SCRIPT_REL = "skills/video-processing/scripts/crop_video.py"

# Sentinel prefixing the sandbox program's single JSON result line, so we can pluck it out of any
# ffmpeg/urllib chatter on stdout.
_SENTINEL = "CROP_RESULT_JSON:"


def _script_path() -> Path:
    """Resolve the bundled crop_video.py script path in either layout.

    Repo: this module is ``agent/src/tools/crop_video.py`` → script at ``agent/skills/...``.
    Deployed: modules are flat (``<app>/tools/crop_video.py``) and skills land at ``<app>/skills/...``.
    """
    global _SCRIPT_PATH
    if _SCRIPT_PATH is not None:
        return _SCRIPT_PATH
    here = Path(__file__).resolve().parent  # .../tools
    candidates = (
        here.parent / _SCRIPT_REL,          # deployed: <app>/tools -> <app>/skills
        here.parent.parent / _SCRIPT_REL,   # repo: agent/src/tools -> agent/skills
    )
    path: Optional[Path] = next((c for c in candidates if c.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            f"crop_video.py script not found in any layout: {[str(c) for c in candidates]}"
        )
    _SCRIPT_PATH = path
    return path


def _load_script() -> Any:
    """Import the bundled crop_video.py script (helpers only) — used for in-process validation
    (``parse_box``/``parse_trim``) and by tests to reach the shared ``resolve_crop``."""
    global _SCRIPT
    if _SCRIPT is not None:
        return _SCRIPT
    script_path = _script_path()
    spec = importlib.util.spec_from_file_location("crop_video_script", script_path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    _SCRIPT = module
    return _SCRIPT


def _script_source() -> str:
    """The bundled script's SOURCE text, to inject into the sandbox program (the sandbox microVM
    does not have ``agent/skills`` mounted)."""
    return _script_path().read_text(encoding="utf-8")


def _output_prefix() -> str:
    """Session-scoped output prefix (mirrors main._artifact_output_prefix): generated crops land
    under the session OUTPUT prefix so the render hook treats them as deliverables — the project
    root when the env opts in and the turn has a trusted project (asset-lineage Phase 3 (a))."""
    from projects.storage_layout import session_output_prefix

    prefix = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")
    try:
        from jobs import context as jobs_context
        sid = (jobs_context.current_session_id() or "").strip()
        pid = jobs_context.trusted_project_id()
    except Exception:  # noqa: BLE001 - context may be unbound outside a request
        sid, pid = "", None
    safe = sid.replace("/", "_").strip() if sid else ""
    return session_output_prefix(safe, pid, artifacts_prefix=prefix)


# --- sandbox program + driver (the seams tests patch: _build_sandbox_code / _run_in_sandbox) -----

# The bundled script ends with `if __name__ == "__main__": raise SystemExit(main())`. The code
# interpreter executes a cell in an IPython kernel where `__name__` IS `"__main__"`, so that CLI ran
# on the KERNEL's argv and killed the program before the runner — `error: the following arguments are
# required: out`, surfaced as "sandbox produced no crop result" (staging 2026-10-08: every crop and
# Clip failed this way, and the model fell back to a 220 s M2C round trip). We cannot simply assign
# `__name__` first: the script opens with `from __future__ import annotations`, which must be the
# first statement. So the guard CONDITION is disabled in the injected copy instead, which keeps the
# script's own CLI contract intact for its standalone use.
_MAIN_GUARD_RE = re.compile(r'^if\s+__name__\s*==\s*(["\'])__main__\1\s*:', re.M)
_MAIN_GUARD_OFF = 'if False:  # sandbox: the script is a LIBRARY here (a cell runs as __main__)'


def _as_library(script_src: str) -> str:
    """The bundled script with its ``__main__`` CLI guard disabled (see above). Never changes the file."""
    return _MAIN_GUARD_RE.sub(_MAIN_GUARD_OFF, script_src)


# The runner appended after the injected bundled script. It uses ONLY the bundled script's helpers
# (single definition of the crop math) plus stdlib + boto3 (present in the sandbox, which also holds
# the S3 write grant). Every parameter is injected as a JSON literal by _build_sandbox_code, so the
# box reaches resolve_crop VERBATIM.
_RUNNER = '''
import os, sys, json, tempfile, subprocess, urllib.request

# The staged ffmpeg is read with the sandbox's IAM role (tools/ffmpeg/* grant), never via a public link.
if _BUCKET and not os.environ.get("FFMPEG_S3_URI") and not os.environ.get("FFMPEG_STATIC_URL"):
    os.environ["FFMPEG_S3_URI"] = "s3://" + _BUCKET + "/tools/ffmpeg"
if _REGION:
    os.environ.setdefault("AWS_REGION", _REGION)

_out = {"error": "crop did not run"}
try:
    with tempfile.TemporaryDirectory() as _td:
        _local = os.path.join(_td, "src.mp4")
        urllib.request.urlretrieve(_SRC, _local)  # sandbox has public egress; ffmpeg then reads LOCAL
        _ffmpeg, _ffprobe = _ensure_ffmpeg_ffprobe()
        _w, _h = probe_dimensions(_ffprobe, _local)
        _audio = probe_has_audio(_ffprobe, _local)
        if _MODE == "aspect":
            _crop = resolve_crop(aspect_box(_BOX, _w, _h), "normalized", _w, _h)
        else:
            _crop = resolve_crop(parse_box(_BOX), _MODE, _w, _h)
        _trim = parse_trim(_TRIM) if _TRIM else None
        _dst = os.path.join(_td, "out.mp4")
        # Encoder args for what the provisioned binary actually has (the harness build: libopenh264,
        # bitrate-driven from the source); raises a clear error when it has no H.264 encoder.
        _vargs = video_codec_args(h264_encoder(_ffmpeg), probe_video_bitrate(_ffprobe, _local))
        _cmd = build_ffmpeg_cmd(_ffmpeg, _local, _dst, _crop, trim=_trim, has_audio=_audio,
                                video_args=_vargs)
        _r = subprocess.run(_cmd, capture_output=True, text=True, timeout=_TIMEOUT)
        if _r.returncode != 0:
            _out = {"error": "ffmpeg failed: " + (_r.stderr or "")[-800:].strip()}
        else:
            import boto3
            with open(_dst, "rb") as _fh:
                boto3.client("s3", region_name=_REGION).put_object(
                    Bucket=_BUCKET, Key=_KEY, Body=_fh.read(), ContentType="video/mp4")
            _url = ("https://" + _DOMAIN + "/" + _KEY) if _DOMAIN else _KEY
            _out = {"status": "cropped", "url": _url, "key": _KEY, "crop": _crop,
                    "sourceDims": [_w, _h], "trim": list(_trim) if _trim else None, "audio": _audio}
except Exception as _e:  # noqa: BLE001 - surface the real cause to the tool
    _out = {"error": "sandbox crop failed: " + repr(_e)}

print(_SENTINEL_TOKEN + json.dumps(_out))
'''


def _build_sandbox_code(script_src: str, *, src: str, box: str, box_px: str, mode: str, aspect: str = "",
                        trim: str, bucket: str, key: str, domain: str, region: str,
                        timeout: float) -> str:
    """Compose the deterministic sandbox program: the bundled script SOURCE + injected params +
    the runner. Params are JSON literals so the box reaches ``resolve_crop`` verbatim.

    The script is injected as a LIBRARY (``_as_library`` disables its ``__main__`` CLI guard) because
    a code-interpreter cell runs as ``__main__`` — see ``_MAIN_GUARD_RE``.
    """
    params = {
        "_SRC": src,
        "_BOX": box or box_px or aspect,
        "_MODE": mode,
        "_TRIM": trim or "",
        "_BUCKET": bucket,
        "_KEY": key,
        "_DOMAIN": domain,
        "_REGION": region,
        "_TIMEOUT": float(timeout),
        "_SENTINEL_TOKEN": _SENTINEL,
    }
    header = "\n".join(f"{k} = {json.dumps(v)}" for k, v in params.items())
    return (f"{_as_library(script_src)}\n\n"
            f"# --- injected params + runner ---\n{header}\n{_RUNNER}")


def _extract_stdout(resp: Any) -> str:
    """Pull stdout text out of an invoke_code_interpreter streaming response."""
    stream = resp.get("stream") if isinstance(resp, dict) else None
    if stream is None:
        return ""
    chunks: list[str] = []
    for event in stream:
        result = event.get("result") if isinstance(event, dict) else None
        if not isinstance(result, dict):
            continue
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and sc.get("stdout"):
            chunks.append(str(sc["stdout"]))
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                chunks.append(str(block["text"]))
    return "\n".join(chunks)


def _inject_scoped_credentials(ci) -> None:
    """Install per-(project, session) S3 credentials in this fresh sandbox before the crop program runs
    (asset-lineage Phase 3 (b), :mod:`sandbox_credentials`). Harness-side, never via model input.

    FAILS CLOSED where the scoped role is configured: raises ``SandboxCredentialsError`` and the crop
    never runs (the sandbox's own role has no artifact S3 access since B2). A no-op only where no role is
    configured at all (unit tests, local runs)."""
    import sandbox_credentials as sc
    from jobs import context as jobs_context

    if not sc.required():
        return
    try:
        sid = jobs_context.current_session_id()
    except Exception:  # noqa: BLE001 - no session bound
        sid = None
    sc.install(ci.invoke, jobs_context.trusted_project_id(), sid)


def _run_in_sandbox(code: str, timeout: float) -> str:
    """Run ``code`` in a fresh code_interpreter sandbox session and return its stdout.

    Isolated by design: ffmpeg never runs in the agent runtime. The agent-runtime role already
    holds Start/Invoke code-interpreter permissions (the model-facing tool uses them)."""
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    region = os.environ.get("AWS_REGION", "us-west-2")
    ci_id = os.environ.get("CODE_INTERPRETER_ID") or "aws.codeinterpreter.v1"
    ci = CodeInterpreter(region)
    ci.start(identifier=ci_id, session_timeout_seconds=max(300, int(timeout) + 180))
    try:
        _inject_scoped_credentials(ci)
        resp = ci.execute_code(code)
        return _extract_stdout(resp)
    finally:
        try:
            ci.stop()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


@tool
def crop_video(source_url: str, box: str = "", box_px: str = "", trim: str = "", timeout: float = 300.0,
               aspect: str = "") -> str:
    """Crop a video to a bounding box and return a new clip (the source is never modified).

    This is the deterministic path for an ARBITRARY region crop, which ``process_media`` cannot
    express (its crop is a top-left offset + scale only). Pass the box the render UI emitted
    VERBATIM — the aspect the user picked (9:16, 1:1, …) is ALREADY baked into ``w``/``h``. Do NOT
    recompute the box from an aspect-ratio label: substituting the ratio for the width fraction
    (e.g. treating "9:16" as ``w=9/16``) crops a square in the wrong place (the efa3569d bug).

    The crop runs in a code_interpreter sandbox (not the agent runtime); the returned CloudFront
    URL auto-renders.

    Args:
        source_url: HTTPS URL of the source video (the ``[source: …]`` the UI threads in). Not
            an ``s3://`` URI. Must be a public host.
        box: NORMALIZED box ``"x,y,w,h"`` as fractions of the frame in 0..1 — what the UI's crop
            selector emits (its ``--box x,y,w,h`` token). Pass these four numbers exactly.
        box_px: PIXEL box ``"x,y,w,h"`` in source pixels — only when the request gives explicit
            pixel numbers.
        aspect: ``"9:16"``, ``"1:1"``, ``"16:9"``, … — the largest CENTERED crop of that aspect, sized
            from the source's real dimensions. Use it whenever the user names an aspect without a box
            ("crop to 1:1", "make it vertical"): never compute a box from a guessed resolution.
            Pass EXACTLY ONE of ``box``, ``box_px`` or ``aspect``.
        trim: Optional temporal trim ``"in,out"`` in seconds, fused into the same pass.
        timeout: ffmpeg timeout in seconds (default 300).

    Returns:
        JSON string ``{"status":"cropped","url":<cloudfront>,"key":...,"crop":{"w","h","x","y"},
        "sourceDims":[w,h],"trim":[in,out]|null,"audio":bool}`` on success, or ``{"error": ...}``.
    """
    src = (source_url or "").strip()
    if not src:
        return json.dumps({"error": "source_url is required"})
    if sum(bool(v) for v in (box, box_px, aspect)) != 1:
        return json.dumps({"error": "pass EXACTLY ONE of box (normalized 0..1), box_px (pixels) or aspect (e.g. 1:1)"})

    mod = _load_script()
    try:
        if aspect:
            mod.parse_aspect(aspect)
            mode = "aspect"
        else:
            mod.parse_box(box or box_px)      # 4 numbers, positive w/h (else ValueError) — fast fail
            mode = "normalized" if box else "pixels"
        if trim:
            mod.parse_trim(trim)              # validate the trim spec too
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    # SSRF guard: the URL is model/UI-supplied. Validate scheme/host/resolved-IP before the sandbox
    # fetches it (an open fetcher would otherwise reach the VPC / metadata endpoint).
    try:
        from media import persist_media
        persist_media.validate_fetchable(src)
    except Exception as exc:  # noqa: BLE001 - PersistError or parse error → refuse cleanly
        return json.dumps({"error": f"source not fetchable: {exc}"})

    bucket = os.environ.get("SKILLS_S3_BUCKET", "")
    domain = os.environ.get("ARTIFACTS_DOMAIN", "")
    region = os.environ.get("AWS_REGION", "us-west-2")
    key = f"{_output_prefix()}crop_{uuid.uuid4().hex[:8]}.mp4"

    # The sandbox downloads `src` over HTTP. When the artifacts CDN is private, our own artifact
    # URL would 403 there, so hand the sandbox a short-lived presigned S3 GET instead (validated
    # above on the original URL; the presigned host is S3). Foreign URLs are untouched.
    fetch_src = src
    try:
        from jobs.context import trusted_project_id
        from media import artifact_fetch
        # Also a project ASSET of the turn's trusted project: session 61deed9b's crop of a project video 403'd twice
        # on the private CDN because only artifact keys were presigned here.
        if artifact_fetch.cdn_private():
            fetch_src = artifact_fetch.presign_for_trusted_turn(src, 3600, trusted_project_id()) or src
    except Exception:  # noqa: BLE001 - presign is best-effort; the original URL still works when public
        fetch_src = src

    code = _build_sandbox_code(
        _script_source(), src=fetch_src, box=box, box_px=box_px, mode=mode, aspect=aspect, trim=trim,
        bucket=bucket, key=key, domain=domain, region=region, timeout=timeout,
    )
    try:
        raw = _run_in_sandbox(code, timeout)
    except Exception as exc:  # noqa: BLE001 - sandbox start/invoke failure → actionable error
        return json.dumps({"error": f"sandbox run failed: {exc}"})

    line = next((ln for ln in raw.splitlines() if ln.startswith(_SENTINEL)), None)
    if line is None:
        return json.dumps({"error": f"sandbox produced no crop result: {raw[-400:].strip()}"})
    return line[len(_SENTINEL):]
