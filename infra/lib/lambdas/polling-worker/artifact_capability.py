"""Harness-owned renderability discovery for completed async jobs.

The polling worker sees the full status payload before it writes a terminal job
row.  This module determines whether that payload references user-facing
artifacts without relying on gateway ids, tool names, or cooperation from the
gateway implementation.

It deliberately returns only a boolean.  Artifact resolution remains in the
agent hook and Render Agent path; the Job Registry needs only enough information
to decide whether the browser should start the existing fetch-and-render turn.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

import render_types

# Type data comes from the vendored render-type registry, kept byte-identical to
# agent/src/render_types.py by test_render_types_parity. This lambda is a separate
# deploy unit and cannot import agent/src, so the parity test is what prevents the
# drift that once let .html render here but not in the hook gate.
_RENDERABLE_EXTENSIONS = render_types.RENDERABLE_EXTS
_RENDERABLE_MIME_PREFIXES = render_types.RENDERABLE_MIME_PREFIXES
_RENDERABLE_MIMES = render_types.RENDERABLE_MIMES

_OPERATIONAL_HINTS = (
    "log", "usage", "tracking", "config", "manifest", "probe", "ephemeral",
)
# Exact JSON basenames that are a spec/ANSWER, not a renderable deliverable. The M2C
# metadata PROBE emits metadata.json (+ probe.json) as the answer to a fast in-turn
# query — never a media tab. EXACT-match (not substring or a whole-token hint) so real
# deliverables like analysis_metadata.json / facemetadata.json still render. probe.json
# is also caught by the "probe" operational hint; listed for intent. Without this the
# poller flagged the probe as renderable → a "N files ready" notification whose click
# re-fetched and rendered the JSON in the generic renderer.
_NON_RENDERABLE_JSON_BASENAMES = ("metadata.json", "probe.json")
_INPUT_HINTS = (
    "input", "source", "asset",
)
_OUTPUT_HINTS = (
    "output", "result", "artifact", "render",
)
_REFERENCE_SUFFIXES = (
    "_key", "_keys", "_uri", "_uris", "_url", "_urls", "_prefix", "_prefixes",
)


def _is_operational(value: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", value.lower()))
    return any(hint in tokens for hint in _OPERATIONAL_HINTS)


def _is_renderable_object(value: str) -> bool:
    clean = value.lower().split("?", 1)[0].rstrip()
    if not any(clean.endswith(ext) for ext in _RENDERABLE_EXTENSIONS):
        return False
    # The probe answer files (metadata.json/probe.json) are not renderable media —
    # mirror the agent so a fast probe never gets a renderable notification.
    if clean.rsplit("/", 1)[-1] in _NON_RENDERABLE_JSON_BASENAMES:
        return False
    return True


def _is_renderable_mime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    mime = value.lower().strip()
    return mime in _RENDERABLE_MIMES or mime.startswith(_RENDERABLE_MIME_PREFIXES)


def _renderable_ref_in_item(item: dict) -> "str | None":
    """A RELATIVE reference (``*_key``/``*_uri``/``*_url``) inside an ``outputs[]``
    item that names a renderable object.

    A text-to-speech item exposes ``{"wav_key": ".../x.wav"}`` instead of an
    absolute ``uri``, so the envelope was "found but empty" and the whole result
    was judged non-renderable. Returns the raw relative key so the URL layer can
    qualify it with the discovered bucket. Generic: keyed on the reference suffix
    + a renderable extension, never on a specific field name.
    """
    for k, v in item.items():
        if not (isinstance(k, str) and isinstance(v, str) and v):
            continue
        if k.lower().endswith(_REFERENCE_SUFFIXES) and "://" not in v:
            if not _is_operational(v.rsplit("/", 1)[-1]) and _is_renderable_object(v):
                return v
    return None


def _iter_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for nested in value:
            if isinstance(nested, str):
                yield nested


def _explicit_outputs_capability(payload: Any) -> tuple[bool, bool]:
    """Return ``(envelope_found, has_artifacts)`` for explicit outputs.

    An explicit output is treated as user-facing unless it opts out with
    ``render: false`` or clearly names an operational artifact. Prefixes are
    accepted; the render path expands them to concrete objects later. Once an
    envelope exists it is authoritative, so heuristic scanning cannot undo an
    explicit opt-out.
    """
    if isinstance(payload, dict):
        found = False
        outputs = payload.get("outputs")
        if isinstance(outputs, list):
            found = True
            for item in outputs:
                if not isinstance(item, dict) or item.get("render") is False:
                    continue
                label = str(item.get("label") or item.get("role") or "")
                if _is_operational(label):
                    continue
                uri = item.get("uri") or item.get("url")
                if (isinstance(uri, str) and uri.strip()
                        and not _is_operational(uri.rsplit("/", 1)[-1])
                        and uri.split("?", 1)[0].rsplit("/", 1)[-1] not in _NON_RENDERABLE_JSON_BASENAMES
                        and uri.startswith(("s3://", "http://", "https://"))
                        and (uri.endswith("/") or _is_renderable_object(uri)
                             or _is_renderable_mime(item.get("mime")))):
                    return True, True
                # Relative *_key reference (e.g. TTS outputs[].wav_key): the URL
                # layer qualifies it with the discovered bucket.
                if _renderable_ref_in_item(item):
                    return True, True
        for value in payload.values():
            nested_found, nested_artifacts = _explicit_outputs_capability(value)
            found = found or nested_found
            if nested_artifacts:
                return True, True
        return found, False
    if isinstance(payload, (list, tuple)):
        found = False
        for value in payload:
            nested_found, nested_artifacts = _explicit_outputs_capability(value)
            found = found or nested_found
            if nested_artifacts:
                return True, True
        return found, False
    return False, False


def _key_tokens(value: str) -> set[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return set(re.findall(r"[a-z0-9]+", separated.lower()))


def _path_is_input(path: tuple[str, ...]) -> bool:
    return any(
        any(hint in _key_tokens(part) for hint in _INPUT_HINTS)
        for part in path
    )


def _walk_references(payload: Any, path: tuple[str, ...] = ()) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            next_path = path + (key_text,)
            key_lower = key_text.lower()
            if _path_is_input(next_path):
                continue
            if _is_operational(key_lower):
                continue

            output_scoped = any(
                any(hint in _key_tokens(part) for hint in _OUTPUT_HINTS)
                for part in next_path
            )
            # A media-container shape (fal: {"video": {"url": "...mp4"}}) exposes the file
            # under a plain "url"/"uri" key, not a "*_url" suffix; accept those too. Safe:
            # input paths are already filtered and a renderable extension is still required.
            reference_field = key_lower.endswith(_REFERENCE_SUFFIXES) or key_lower in ("url", "uri")

            if output_scoped or reference_field:
                for candidate in _iter_values(value):
                    if not candidate or _is_operational(candidate.rsplit("/", 1)[-1]):
                        continue
                    if _is_renderable_object(candidate):
                        return True
                    if key_lower.endswith(("_prefix", "_prefixes")) and candidate.endswith("/"):
                        return True
                    if output_scoped and candidate.startswith(("s3://", "http://", "https://")):
                        return True

            if _walk_references(value, next_path):
                return True
        return False
    if isinstance(payload, (list, tuple)):
        return any(_walk_references(value, path) for value in payload)
    return False


def _unwrap_mcp(payload: Any) -> Any:
    """Return the structured result inside an MCP ``tools/call`` envelope.

    M2C status tools reply as ``{"content": [{"type": "text", "text": "<json>"}]}``
    (or a newer ``structuredContent`` dict). The renderable checks operate on the
    parsed result — otherwise the ``outputs[]`` list is hidden inside a JSON
    string and never detected, so a finished job (e.g. a text-to-speech WAV) is
    marked ``hasRenderableArtifacts=false`` and never auto-renders. Returns the
    payload unchanged when it is not such an envelope, so a normal parsed body is
    unaffected.
    """
    if not isinstance(payload, dict):
        return payload
    structured = payload.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)):
                try:
                    parsed = json.loads(block["text"])
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, (dict, list)):
                    return parsed
    return payload


def _is_metadata_only(payload: Any) -> bool:
    """True when a producer DECLARES its output is metadata/spec-only (a probe), so there is
    nothing renderable regardless of how its output ``s3uri`` (typically a bare prefix) would
    otherwise be walked. Honours two explicit, tool-agnostic signals a producer emits:
      - ``metadata_only: true`` anywhere in the status body, or
      - every ``output_groups[].format`` is a non-renderable spec format (e.g. ``metadata``).
    This is what stops a fast metadata probe from getting a renderable notification/bell, a
    generic-preview JSON tab, and a stuck render of a directory that holds only probe JSON."""
    _NON_RENDERABLE_FORMATS = {"metadata", "probe", "spec"}
    found = False

    def walk(p: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(p, dict):
            if p.get("metadata_only") is True:
                found = True
                return
            og = p.get("output_groups")
            if (isinstance(og, list) and og and all(
                    isinstance(g, dict) and str(g.get("format", "")).lower() in _NON_RENDERABLE_FORMATS
                    for g in og)):
                found = True
                return
            for v in p.values():
                walk(v)
        elif isinstance(p, (list, tuple)):
            for v in p:
                walk(v)

    walk(payload)
    return found


def has_renderable_artifacts(status_payload: Any) -> bool:
    """Return whether a completed status payload references renderable output.

    Explicit output envelopes take priority, followed by generic output/result/
    artifact references and conventional ``*_key``/``*_prefix`` fields.  Input
    and operational references are ignored.  The algorithm is intentionally
    independent of tool and gateway names.
    """
    status_payload = _unwrap_mcp(status_payload)
    # A producer that declares metadata-only output (a probe) has nothing to render — honour that
    # BEFORE walking references, else its bare output prefix gets collected by the output-scoped
    # catch-all and a metadata probe wrongly reads as renderable (bell + JSON tab + stuck render).
    if _is_metadata_only(status_payload):
        return False
    envelope_found, explicit_artifacts = _explicit_outputs_capability(status_payload)
    if envelope_found:
        return explicit_artifacts
    return _walk_references(status_payload)


def _collect_explicit_outputs(payload: Any, acc: list[str]) -> bool:
    """Collect renderable URIs from explicit output envelopes (mirrors
    ``_explicit_outputs_capability``). Returns whether an envelope was found."""
    found = False
    if isinstance(payload, dict):
        outputs = payload.get("outputs")
        if isinstance(outputs, list):
            found = True
            for item in outputs:
                if not isinstance(item, dict) or item.get("render") is False:
                    continue
                label = str(item.get("label") or item.get("role") or "")
                if _is_operational(label):
                    continue
                uri = item.get("uri") or item.get("url")
                if (isinstance(uri, str) and uri.strip()
                        and not _is_operational(uri.rsplit("/", 1)[-1])
                        and uri.split("?", 1)[0].rsplit("/", 1)[-1] not in _NON_RENDERABLE_JSON_BASENAMES
                        and uri.startswith(("s3://", "http://", "https://"))
                        and (uri.endswith("/") or _is_renderable_object(uri)
                             or _is_renderable_mime(item.get("mime")))):
                    acc.append(uri)
                rk = _renderable_ref_in_item(item)
                if rk:
                    acc.append(rk)
        for value in payload.values():
            if _collect_explicit_outputs(value, acc):
                found = True
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            if _collect_explicit_outputs(value, acc):
                found = True
    return found


def _collect_references(payload: Any, acc: list[str], path: tuple[str, ...] = ()) -> None:
    """Collect renderable references (mirrors ``_walk_references``)."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            next_path = path + (key_text,)
            key_lower = key_text.lower()
            if _path_is_input(next_path):
                continue
            if _is_operational(key_lower):
                continue
            output_scoped = any(
                any(hint in _key_tokens(part) for hint in _OUTPUT_HINTS)
                for part in next_path
            )
            # A media-container shape (fal: {"video": {"url": "...mp4"}}) exposes the file
            # under a plain "url"/"uri" key, not a "*_url" suffix; accept those too. Safe:
            # input paths are already filtered and a renderable extension is still required.
            reference_field = key_lower.endswith(_REFERENCE_SUFFIXES) or key_lower in ("url", "uri")
            if output_scoped or reference_field:
                for candidate in _iter_values(value):
                    if not candidate or _is_operational(candidate.rsplit("/", 1)[-1]):
                        continue
                    if _is_renderable_object(candidate):
                        acc.append(candidate)
                    elif key_lower.endswith(("_prefix", "_prefixes")) and candidate.endswith("/"):
                        acc.append(candidate)
                    elif output_scoped and candidate.startswith(("s3://", "http://", "https://")):
                        acc.append(candidate)
            _collect_references(value, acc, next_path)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _collect_references(value, acc, path)


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


_INPUT_SCOPE_KEYS = frozenset({"input", "inputs", "input_config", "source", "sources", "request"})


def _discover_bucket(payload: Any) -> str | None:
    """Best-effort discovery of the OUTPUT bucket so relative object keys can be
    qualified into ``s3://`` URLs.

    OUTPUT-biased and input-aware — this is the fix for the shot-segmentation
    wrong-bucket bug. A workflow payload carries buckets for BOTH sides: an
    input/source bucket (where the media came from) and an output bucket (where
    the deliverables were written). shot-segmentation, for example, carries
    ``input.s3uri`` / ``data.source_bucket`` (the skills-store input) AND
    ``output.s3uri`` / ``data.proxy_bucket`` (the proxy output). The old code
    returned the FIRST ``*bucket*`` key it walked — ``source_bucket`` — and
    qualified the proxy-relative OUTPUT keys with the skills-store INPUT bucket,
    producing refs that 404 (the object lives in the proxy bucket at that key).

    So classify candidates and prefer the output side, in order:
      1. the bucket of an ``s3://`` value under an ``output``/``destination`` subtree,
      2. an output-named bucket hint (``proxy_bucket`` / ``output_bucket`` / ``destination.bucket``),
      3. a neutral bucket hint (not named source/input),
      4. a neutral ``s3://`` bucket (not under an input/source subtree),
      5. only as a last resort, an input/source bucket.
    """
    out_uri: list[str] = []
    out_hint: list[str] = []
    neutral_hint: list[str] = []
    neutral_uri: list[str] = []
    input_side: list[str] = []

    def bucket_of(uri: str) -> str:
        rest = uri[len("s3://"):]
        return rest.split("/", 1)[0] if "/" in rest else rest

    def walk(p: Any, in_input: bool, in_output: bool) -> None:
        if isinstance(p, dict):
            for k, v in p.items():
                kl = str(k).lower()
                child_in = in_input or kl in _INPUT_SCOPE_KEYS or kl.startswith("source")
                child_out = in_output or "output" in kl or "destination" in kl
                if isinstance(v, str) and v and "://" not in v and "bucket" in kl:
                    if "source" in kl or "input" in kl:
                        input_side.append(v)
                    elif "proxy" in kl or "output" in kl or "out_" in kl or "dest" in kl or child_out:
                        out_hint.append(v)
                    elif child_in:
                        input_side.append(v)
                    else:
                        neutral_hint.append(v)
                walk(v, child_in, child_out)
        elif isinstance(p, (list, tuple)):
            for v in p:
                walk(v, in_input, in_output)
        elif isinstance(p, str) and p.startswith("s3://"):
            b = bucket_of(p)
            (out_uri if in_output else (input_side if in_input else neutral_uri)).append(b)

    walk(payload, False, False)
    for lst in (out_uri, out_hint, neutral_hint, neutral_uri, input_side):
        if lst:
            return lst[0]
    return None


def renderable_artifact_urls(status_payload: Any) -> list[str]:
    """Return the concrete renderable output URL(s) in a completed status
    payload, in document order, deduped.

    Uses the SAME precedence as :func:`has_renderable_artifacts` (explicit
    output envelope is authoritative; otherwise generic output/reference
    scanning), then RESOLVES + FILTERS the raw candidates so the URL(s) are
    directly renderable:

    - relative object keys (e.g. ``…/proxy/transcoded.mp4``) are qualified into
      ``s3://<bucket>/<key>`` using the payload's ``destination.bucket`` (or a
      bucket parsed from a sibling ``s3://`` value);
    - concrete file objects (with a renderable extension) are preferred over
      bare prefixes/directories — a ``process_media`` output exposes BOTH an
      output prefix and the concrete ``transcoded.mp4`` key, and only the file
      yields a valid render manifest (a prefix renders as duration 0).

    Persisted on the job row by the poller so the browser can render a finished
    job deterministically (no LLM turn) via the ``/render`` endpoint.
    """
    status_payload = _unwrap_mcp(status_payload)
    if _is_metadata_only(status_payload):
        return []
    envelope: list[str] = []
    if _collect_explicit_outputs(status_payload, envelope):
        raw = _dedupe(envelope)
    else:
        refs: list[str] = []
        _collect_references(status_payload, refs)
        raw = _dedupe(refs)

    bucket = _discover_bucket(status_payload)
    resolved: list[str] = []
    for u in raw:
        if u.startswith(("s3://", "http://", "https://")):
            resolved.append(u)
        elif bucket:
            resolved.append(f"s3://{bucket}/{u.lstrip('/')}")
        # else: an unresolvable relative reference with no known bucket — drop.

    # Prefer concrete file objects over bare prefixes/directories.
    concrete = [u for u in resolved if _is_renderable_object(u)]
    return _dedupe(concrete if concrete else resolved)
