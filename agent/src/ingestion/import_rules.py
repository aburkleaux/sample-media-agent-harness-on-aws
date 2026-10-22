"""What the import inbox does with each file (pure, no I/O): ingest it, keep it unindexed, flag it, or ignore it.

A shoot copied to ``projects/<pid>/import/`` holds far more than media: camera sidecars, OS litter, LUTs, edit
decision lists, camera RAW the pipeline cannot decode. Every object is classified by its name and size alone, so
the decision needs no download:

* ``ingest`` — media and documents the pipeline handles (the same kinds and per-kind size limits as an upload);
* ``keep`` — production files worth having in the project but with nothing to index (LUTs, EDL/XML/AAF, project
  files): registered as stored, never queued;
* ``attention`` — a file the user meant to import that cannot be (``not_supported`` camera RAW, ``too_large`` over
  the kind's limit, ``empty``): listed under "Needs attention" with what to do;
* ``ignore`` — sidecars and system files (``.DS_Store``, ``Thumbs.db``, XDCAM ``…M01.XML``, ``.BIM``/``.THM``/…),
  skipped silently.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

from .limits import admit, kind_for

# Camera RAW and image-sequence formats the media pipeline cannot decode.
RAW_EXT = frozenset({"r3d", "braw", "ari", "arx", "crm", "dng", "cine", "nev", "rmf", "exr", "dpx"})
# Production files with nothing to index, kept so the project holds the whole job.
KEEP_EXT = frozenset({"cube", "3dl", "lut", "edl", "aaf", "omf", "fcpxml", "prproj", "drp", "otio", "ale", "csv"})
# Camera and editor sidecars: their data belongs to the clip next to them, not the project grid.
SIDECAR_EXT = frozenset({"bim", "thm", "ppn", "cpi", "mpl", "bdm", "lrf", "sav", "ind", "smi", "bin", "dat"})
_SONY_XML_RE = re.compile(r"M\d{2}\.xml\Z", re.IGNORECASE)   # XDCAM / XAVC clip metadata: A003_C017_0815XKM01.XML
_SYSTEM_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store", "icon\r"})

__all__ = ["classify", "RAW_EXT", "KEEP_EXT", "SIDECAR_EXT"]


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def classify(key: str, size: int, content_type: Optional[str], limits: Mapping[str, int]) -> dict:
    """``{"action": "ingest"|"keep"|"attention"|"ignore", "kind"?: str, "reason"?: str}`` for one inbox object."""
    name = key.rsplit("/", 1)[-1]
    low = name.lower()
    ext = _ext(name)
    if not name or low in _SYSTEM_NAMES or name.startswith("._") or name.startswith("."):
        return {"action": "ignore", "reason": "system_file"}
    if ext in SIDECAR_EXT or (ext == "xml" and _SONY_XML_RE.search(name)):
        return {"action": "ignore", "reason": "sidecar"}
    if ext in RAW_EXT:
        return {"action": "attention", "reason": "not_supported"}
    if ext in KEEP_EXT or ext == "xml":
        return {"action": "keep", "kind": "document"}
    kind = kind_for(content_type, key)
    refusal = admit(kind, size, limits)
    if refusal is not None:
        return {"action": "attention", "kind": kind, "reason": refusal}
    return {"action": "ingest", "kind": kind}
