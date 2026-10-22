"""M2C catalog check (Req 8.15, 11.2, 11.3, 11.5; design Component 9 "Catalog").

Pure over a set of listed tool names. Registration, Retry, Re-add, and rebuild retry call it before writing
anything, and refuse with the missing tool names. A ``tools/list`` that did not complete is "unreadable", which
the caller reports with that wording rather than as a missing-tool list. The image model-capability check
(``get_model``) is separate because it needs the configured ``model_chain``.
"""
from __future__ import annotations

from typing import Iterable

_COMMON = ("hybrid-search___create_collection", "hybrid-search___index_content", "hybrid-search___get_job_status",
           "hybrid-search-admin___delete_collection", "hybrid-search-admin___configure_search_backend")
_WF = {
    "shot-segmentation": ("shot-segmentation___segment_shots", "shot-segmentation___get_job_status"),
    "speech-diarization": ("speech-diarization___diarize_speech", "speech-diarization___get_job_status"),
    "media-processing": ("media-processing___process_media", "media-processing___get_job_status"),
    "shot-metadata-enrichment": ("shot-metadata-enrichment___enrich_shot_metadata",
                                 "shot-metadata-enrichment___get_job_status"),
    "conversation-analysis": ("conversation-analysis___analyze_conversation", "conversation-analysis___get_job_status"),
    "bedrock-inference": ("bedrock-inference___run_inference", "bedrock-inference___get_job_status",
                          "bedrock-inference___get_model"),
}
_BY_KIND = {
    "video": ("shot-segmentation", "speech-diarization", "media-processing", "shot-metadata-enrichment",
              "conversation-analysis"),
    "image": ("bedrock-inference",),
    "audio": ("speech-diarization", "conversation-analysis"),
}

UNREADABLE = "the M2C tool catalog is unreadable"


def required_tools(kind: str) -> tuple[str, ...]:
    if kind not in _BY_KIND:
        raise ValueError(f"no ingestion pipeline for kind {kind!r}")
    return _COMMON + tuple(t for wf in _BY_KIND[kind] for t in _WF[wf])


def missing_tools(kinds: Iterable[str], listed: Iterable[str]) -> tuple[str, ...]:
    """Required tools absent from the catalog, in a stable order (no duplicates)."""
    have = set(listed)
    out: list[str] = []
    for k in kinds:
        for t in required_tools(k):
            if t not in have and t not in out:
                out.append(t)
    return tuple(out)


def refusal(missing: Iterable[str]) -> str:
    names = ", ".join(missing)
    return f"the M2C catalog is missing {names}" if names else ""
