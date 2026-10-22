"""Per-turn ledger of the files THIS runtime wrote, and what each was made from (lineage).

Two consumers, one ledger:

* The render auto-harvester's cross-session URL filter (``hooks.actions``). A path is not an
  identity: the main agent's code tool once wrote the user's clip to ``artifacts/<invented
  uuid>/output/`` (staging session 833a079f), and the filter -- which reads the session from the
  PATH -- dropped the user's own clip as "another session's". A key this runtime is known to
  have written during the turn is this session's output wherever it sits.
* The turn-end ``execution_summary.lineage`` (``main.py``), forwarded by the proxy in the
  Generated_Media turn message so the registrar can record ``derivedFrom`` (lineage design §3/§4):
  ``[{output, inputs, tool}]`` in canonical ``s3://bucket/key`` form.

Fed from the hook provider's ``AfterToolCallEvent`` (before the render rule fires, so the filter
sees this result's writes) and reset when an invocation starts. Module state, like the render
batch beside it: one invocation runs at a time per runtime process.

What counts as a write, per tool:

* ``artifact`` -- the ``key`` an upload/adopt/import returned (an import is derived from its
  ``source_uri``);
* ``crop_video`` -- the ``key`` it returned, derived from its ``source_url``;
* ``code_interpreter`` -- own refs (skills-bucket ``artifacts/`` keys) first appearing in the tool
  RESULT are outputs (when the path names ANOTHER session, only if one HEAD shows the object was
  written this turn -- the invented-uuid case, without letting a printed listing of someone
  else's files pass); a ref the code DID contain is an output only when its object was modified
  during this turn and no earlier tool this turn wrote it. Own ``artifacts/`` / ``projects/``
  refs in the code are the inputs. HEADs are capped per call.

Never raises into the hook: every entry point swallows its own failures.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from functools import lru_cache
from typing import Any, Iterable, Optional

from media import artifact_fetch

logger = logging.getLogger(__name__)

__all__ = [
    "open_turn",
    "note_write",
    "note_tool_result",
    "is_own_write",
    "lineage",
    "own_writes",
]

# A candidate reference inside free text (code, stdout, a JSON tool result). Stops at whitespace,
# quotes, brackets, backslashes (JSON escapes) and commas.
_STOP = r"\s'\"<>()\[\]{}`\\,"
_URL_RE = re.compile(rf"(?:s3|https?)://[^{_STOP}]+", re.IGNORECASE)
_TRAILING_PUNCT = ".;:!?"

_MAX_LINEAGE = 100
_MAX_INPUTS = 20          # derivedFrom is capped at 20 asset ids (design §2)
_MAX_HEADS_PER_CALL = 8   # bound the HEADs one code_interpreter result can cost
_CLOCK_SKEW_S = 5.0

_turn_started_at: float = 0.0
_own: set[str] = set()
# output canonical uri -> {"tool": str, "inputs": [canonical uri, ...]}, insertion-ordered.
_lineage: dict[str, dict] = {}


def open_turn(now: Optional[float] = None) -> None:
    """Start a new turn: forget the previous turn's writes and lineage."""
    global _turn_started_at
    _turn_started_at = time.time() if now is None else float(now)
    _own.clear()
    _lineage.clear()


def own_writes() -> set[str]:
    return set(_own)


def _artifacts_prefix() -> str:
    return (os.environ.get("ARTIFACTS_PREFIX") or "artifacts/").lstrip("/")


def _own_bucket() -> str:
    return artifact_fetch.artifacts_bucket()


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri[len("s3://"):].partition("/")
    return bucket, key


def _is_own_output_key(uri: str) -> bool:
    """A skills-bucket key under the artifacts prefix — or a projects-session OUTPUT key
    (asset-lineage Phase 3 (a)) — that can be a deliverable (not an input upload, not a render
    manifest)."""
    from projects.storage_layout import is_project_session_output_key

    bucket, key = _split(uri)
    if not bucket or bucket != _own_bucket():
        return False
    if is_project_session_output_key(key):
        return True
    if not key.startswith(_artifacts_prefix()):
        return False
    return "/input/" not in key and "/render/" not in key


def _is_own_ref(uri: str) -> bool:
    bucket, _ = _split(uri)
    return bool(bucket) and bucket == _own_bucket()


def canonical(ref: Any) -> Optional[str]:
    try:
        return artifact_fetch.canonical_location(ref)
    except Exception:  # noqa: BLE001 - an unparseable ref is simply not a location
        return None


@lru_cache(maxsize=8)
def _key_re(roots: tuple[str, ...]) -> "re.Pattern[str]":
    """Bare ``artifacts/...`` / ``projects/...`` keys not embedded in a longer path or URL."""
    return re.compile(r"(?<![\w/.:-])(?:" + "|".join(re.escape(r) for r in roots) + rf")[^{_STOP}]+")


def refs_in_text(text: str) -> list[str]:
    """Canonical locations named in ``text``, in first-seen order (URLs, s3 URIs, bare keys)."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        loc = canonical(raw.rstrip(_TRAILING_PUNCT))
        if loc and loc not in seen:
            seen.add(loc)
            found.append(loc)

    for m in _URL_RE.finditer(text):
        _add(m.group(0))
    roots = tuple(sorted({_artifacts_prefix(), "artifacts/", "projects/"}, key=len, reverse=True))
    for m in _key_re(roots).finditer(_URL_RE.sub(" ", text)):
        _add(m.group(0))
    return found


def _strings(node: Any, depth: int = 0) -> Iterable[str]:
    """Every string inside a tool input/result structure (bounded depth)."""
    if depth > 8:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v, depth + 1)


def _refs_in(node: Any) -> list[str]:
    out: list[str] = []
    for s in _strings(node):
        for r in refs_in_text(s):
            if r not in out:
                out.append(r)
    return out


def _json_payloads(result: Any) -> list[dict]:
    """The JSON objects a tool returned as text blocks (``@tool`` functions return a JSON string)."""
    out: list[dict] = []
    for s in _strings(result):
        t = s.strip()
        if not t.startswith("{"):
            continue
        try:
            obj = json.loads(t)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def note_write(output: Any, inputs: Iterable[Any] = (), tool: str = "") -> Optional[str]:
    """Record that this runtime wrote ``output`` during the turn, made from ``inputs``.

    Returns the canonical output location, or None when ``output`` is not a location.
    """
    out = canonical(output)
    if not out:
        return None
    _own.add(out)
    ins: list[str] = []
    for i in inputs or ():
        loc = canonical(i)
        if loc and loc != out and loc not in ins:
            ins.append(loc)
    entry = _lineage.get(out)
    if entry is None:
        if len(_lineage) >= _MAX_LINEAGE:
            return out
        entry = {"tool": tool or "", "inputs": []}
        _lineage[out] = entry
    for loc in ins:
        if loc not in entry["inputs"] and len(entry["inputs"]) < _MAX_INPUTS:
            entry["inputs"].append(loc)
    if tool and not entry["tool"]:
        entry["tool"] = tool
    return out


def is_own_write(url: Any) -> bool:
    """True when ``url`` names an object this runtime wrote during the current turn."""
    loc = canonical(url)
    return bool(loc) and loc in _own


def lineage() -> list[dict]:
    """The turn's lineage, ``[{output, inputs, tool}]`` in canonical ``s3://bucket/key`` form."""
    return [{"output": out, "inputs": list(e["inputs"]), "tool": e["tool"]}
            for out, e in _lineage.items()]


def _modified_this_turn(uri: str) -> bool:
    """One HEAD: was this object last written at/after the turn started? False on any failure."""
    if not _turn_started_at:
        return False
    bucket, key = _split(uri)
    try:
        head = artifact_fetch._client().head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 - absent / unreadable: not provably ours
        return False
    lm = head.get("LastModified")
    try:
        ts = lm.timestamp() if hasattr(lm, "timestamp") else float(lm)
    except (TypeError, ValueError):
        return False
    return ts >= _turn_started_at - _CLOCK_SKEW_S


_SESSION_SEG_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _current_session() -> str:
    try:
        from jobs.context import current_session_id
        return (current_session_id() or "").strip()
    except Exception:  # noqa: BLE001 - unbound outside a request
        return ""


def _under_other_session(uri: str) -> bool:
    """True when the key sits under a session root — ``artifacts/<uuid>/`` or
    ``projects/<pid>/sessions/<uuid>/`` — for a uuid that is not this session."""
    from projects.storage_layout import parse_session_key

    _, key = _split(uri)
    sk = parse_session_key(key)
    if sk is not None and sk.layout == "project":
        seg = sk.sid
    elif key.startswith(_artifacts_prefix()):
        seg = key[len(_artifacts_prefix()):].split("/", 1)[0]
    else:
        return False
    return bool(_SESSION_SEG_RE.match(seg)) and seg.lower() != _current_session().lower()


def _code_interpreter(tool_input: Any, result: Any) -> None:
    code_refs = [r for r in _refs_in(tool_input) if _is_own_ref(r)]
    result_refs = [r for r in _refs_in(result) if _is_own_ref(r)]
    outputs: list[str] = []
    heads = 0

    def _fresh(r: str) -> bool:
        nonlocal heads
        if heads >= _MAX_HEADS_PER_CALL:
            return False
        heads += 1
        return _modified_this_turn(r)

    # A ref first appearing in the RESULT is an output -- verified by one HEAD when its path names
    # another session, so a listing that merely PRINTS someone else's files never passes as ours.
    for r in result_refs:
        if r in code_refs or r in _own or not _is_own_output_key(r):
            continue
        if not _under_other_session(r) or _fresh(r):
            outputs.append(r)
    # A key literally in the code is an input, unless the code wrote it this turn (a model that
    # types its output key -- invented uuid and all -- into put_object). Something an earlier tool
    # call wrote this turn is an input to this one.
    for r in code_refs:
        if r in outputs or r in _own or not _is_own_output_key(r):
            continue
        if _fresh(r):
            outputs.append(r)
    inputs = [r for r in code_refs if r not in outputs]
    for out in outputs:
        note_write(out, inputs, tool="code_interpreter")


def _keyed_result(result: Any, ok_status: tuple[str, ...]) -> Optional[dict]:
    for obj in _json_payloads(result):
        if obj.get("error"):
            continue
        if str(obj.get("status") or "") in ok_status and isinstance(obj.get("key"), str):
            return obj
    return None


def note_tool_result(tool_name: str, tool_input: Any, result: Any) -> None:
    """Record the writes (and lineage) of one finished tool call. Never raises."""
    try:
        name = str(tool_name or "")
        if isinstance(result, dict) and str(result.get("status") or "") == "error":
            return
        inp = tool_input if isinstance(tool_input, dict) else {}
        if name == "artifact":
            obj = _keyed_result(result, ("uploaded", "adopted", "imported"))
            if obj:
                src = inp.get("source_uri") if obj.get("status") == "imported" else None
                note_write(obj["key"], [src] if src else [], tool="artifact")
        elif name == "crop_video":
            obj = _keyed_result(result, ("cropped",))
            if obj:
                note_write(obj["key"], [inp.get("source_url")], tool="crop_video")
        elif name == "code_interpreter":
            _code_interpreter(tool_input, result)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a tool call
        logger.debug("turn_outputs: could not record %s: %s", tool_name, exc)
