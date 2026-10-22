"""Projects table key schema — Python twin of ``ui/server/projects/schema.ts``.

media-harness-projects-access task 6.14 (design Data Models "Projects table", D5; Interfaces provided to
sibling specs → project-ingestion).

Single-table, string ``PK``/``SK``, no GSIs. Every item a registry operation touches is named here, so a
typo in a key prefix cannot split one logical item into two. The key builders refuse a malformed project
id or an empty sub, exactly as the TS twin does: an empty sub would build ``USER#``, a key every actor's
code path could collide on. Timestamps (``createdAt``, ``deletedAt``, ``addedAt``, ``purgedAt``) are ISO
8601 strings.

This module is import-only data plumbing (no boto3, no I/O); the membership read lives in ``access.py``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Mapping, Optional

from projects.ids import is_project_id

META_SK = "META"
PERSONAL_SK = "PERSONAL"
COUNTERS_SK = "COUNTERS"
MEMBER_SK_PREFIX = "MEMBER#"
MIRROR_SK_PREFIX = "PROJECT#"
NAME_SK_PREFIX = "NAME#"

PROJECT_PK_PREFIX = "PROJECT#"
USER_PK_PREFIX = "USER#"

# --- media-harness-project-ingestion additions (design §2 "Asset_Service and the Python registry",
# Data Models "Projects table additions"). Project_Asset rows and their guards live in the Project's
# own partition (PROJECT#<pid>), so the registration transaction and the deletion purge both stay
# single-partition. These are import-only key builders; the row reader/writer is `ingestion.store`.
ASSET_SK_PREFIX = "ASSET#"
SRC_SK_PREFIX = "SRC#"
DNAME_SK_PREFIX = "DNAME#"
QUEUE_SK_PREFIX = "QUEUE#"
FAILIDX_SK_PREFIX = "FAILIDX#"
# media-harness-asset-lineage (design §2): the per-Project content-hash dedupe guard and the M2C job record.
HASH_SK_PREFIX = "HASH#"
JOB_SK_PREFIX = "JOB#"
# A bookmark: a saved moment of a Project_Asset (its id + an in/out range) filed in a folder like a file. Its own
# row type, NOT an ASSET row, so ingestion, search, purge scans and the asset list never see it.
BOOKMARK_SK_PREFIX = "BOOKMARK#"
# A folder made empty ("New folder"): one row per path, so every device and every member sees it before anything
# is filed in it. A folder that holds files needs no row (the files name it); renaming or deleting one re-keys these.
FOLDER_SK_PREFIX = "FOLDER#"
BOOKMARK_ID_PATTERN = re.compile(r"^bookmark:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# A Project_Asset id is "<kind>:<lowercase UUID v4>" (Req 1.17; the orphan sweep's filter, design §19).
# `document` is every file that is not video, audio, or an image (Projects Phase B: index ALL file
# types; its indexing lands later, so document rows wait in `queued`). `data` is a session or M2C
# analysis file (JSON/CSV/text/subtitles, asset-lineage design §2); it is never queued for indexing.
ASSET_KINDS = ("video", "image", "audio", "document", "data")
ASSET_ID_PATTERN = re.compile(
    r"^(?:video|image|audio|document|data):"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Zero-pad the queued/failed millisecond timestamp so a lexical sort over the sort key is a
# chronological sort (design §7: "in order of the time each entered queued"). 13 digits holds every
# epoch-millisecond value through the year 2286.
_MS_WIDTH = 13

Key = dict[str, str]


def project_pk(pid: str) -> str:
    if not is_project_id(pid):
        raise ValueError(f"not a project id: {pid!r}")
    return PROJECT_PK_PREFIX + pid


def user_pk(sub: str) -> str:
    if not isinstance(sub, str) or not sub:
        raise ValueError("actor sub must be a non-empty string")
    return USER_PK_PREFIX + sub


def meta_key(pid: str) -> Key:
    """``PROJECT#<pid> / META``: the Project meta item, or its id tombstone after purge."""
    return {"PK": project_pk(pid), "SK": META_SK}


def member_key(pid: str, sub: str) -> Key:
    """``PROJECT#<pid> / MEMBER#<sub>``: one membership (the Owner has one too, ``role = owner``)."""
    return {"PK": project_pk(pid), "SK": MEMBER_SK_PREFIX + _non_empty(sub)}


def mirror_key(sub: str, pid: str) -> Key:
    """``USER#<sub> / PROJECT#<pid>``: the user → Project mirror that listing queries."""
    return {"PK": user_pk(sub), "SK": MIRROR_SK_PREFIX + _project_id(pid)}


def personal_key(sub: str) -> Key:
    """``USER#<sub> / PERSONAL``: the pointer to the Actor's one Personal_Project."""
    return {"PK": user_pk(sub), "SK": PERSONAL_SK}


def counters_key(sub: str) -> Key:
    """``USER#<sub> / COUNTERS``: ``ownedCount``, the non-Personal Projects the Actor owns."""
    return {"PK": user_pk(sub), "SK": COUNTERS_SK}


def name_guard_key(owner_sub: str, name_fold: str) -> Key:
    """``USER#<ownerSub> / NAME#<nameFold>``: the per-Owner name uniqueness guard."""
    if not isinstance(name_fold, str) or not name_fold:
        raise ValueError("name fold must be non-empty")
    return {"PK": user_pk(owner_sub), "SK": NAME_SK_PREFIX + name_fold}


def sub_from_member_sk(sk: str) -> Optional[str]:
    """The sub of a ``MEMBER#<sub>`` sort key, or ``None`` for any other sort key."""
    if isinstance(sk, str) and sk.startswith(MEMBER_SK_PREFIX) and len(sk) > len(MEMBER_SK_PREFIX):
        return sk[len(MEMBER_SK_PREFIX):]
    return None


def pid_from_mirror_sk(sk: str) -> Optional[str]:
    """The project id of a ``PROJECT#<pid>`` mirror sort key, or ``None`` when it is not one."""
    if not isinstance(sk, str) or not sk.startswith(MIRROR_SK_PREFIX):
        return None
    pid = sk[len(MIRROR_SK_PREFIX):]
    return pid if is_project_id(pid) else None


def project_prefix(pid: str) -> str:
    """The S3 prefix under which a Project's content lives (``projects/<pid>/``)."""
    return f"projects/{_project_id(pid)}/"


def tombstone_item(pid: str, deleted_at: str, purged_at: str) -> dict[str, Any]:
    """What the ingestion purge leaves at ``META`` once every other item of the Project is gone."""
    return {**meta_key(pid), "tombstone": True, "deletedAt": deleted_at, "purgedAt": purged_at}


def is_tombstone(item: Optional[Mapping[str, Any]]) -> bool:
    """True only for the purge tombstone (``tombstone == True``), matching the TS ``isTombstone``."""
    return bool(item) and item.get("tombstone") is True


# ---------------------------------------------------------------------------------------------------
# media-harness-project-ingestion row keys (design §2, Data Models). Every one names the key
# PROJECT#<pid> / <prefix>…, so an asset or guard of another Project is simply absent, never confused
# for one of this Project's (Req 3.11).
# ---------------------------------------------------------------------------------------------------

def is_asset_id(s: object) -> bool:
    """True for a well-formed Project_Asset id ``<kind>:<lowercase UUID v4>`` (Req 1.17, design §19)."""
    return isinstance(s, str) and ASSET_ID_PATTERN.fullmatch(s) is not None


def asset_kind(asset_id: str) -> Optional[str]:
    """The ``video``/``image``/``audio``/``document``/``data`` kind of a well-formed asset id, else ``None``."""
    if not is_asset_id(asset_id):
        return None
    return asset_id.split(":", 1)[0]


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fold_name(name: str) -> str:
    """``unicodedata.normalize("NFC", name).casefold()`` — the Display_Name guard's case/width fold."""
    if not isinstance(name, str):
        raise ValueError("display name must be a string")
    return unicodedata.normalize("NFC", name).casefold()


def asset_key(pid: str, asset_id: str) -> Key:
    """``PROJECT#<pid> / ASSET#<assetId>``: one Project_Asset row (design §2)."""
    if not is_asset_id(asset_id):
        raise ValueError(f"not an asset id: {asset_id!r}")
    return {"PK": project_pk(pid), "SK": ASSET_SK_PREFIX + asset_id}


def is_bookmark_id(s: object) -> bool:
    """True for a well-formed bookmark id ``bookmark:<lowercase UUID v4>``."""
    return isinstance(s, str) and BOOKMARK_ID_PATTERN.fullmatch(s) is not None


def folder_key(pid: str, folder: str) -> Key:
    """``PROJECT#<pid> / FOLDER#<path>``: a folder a member made, which may hold nothing yet."""
    return {"PK": project_pk(pid), "SK": f"{FOLDER_SK_PREFIX}{folder}"}


def bookmark_key(pid: str, bookmark_id: str) -> Key:
    """``PROJECT#<pid> / BOOKMARK#<bookmarkId>``: one saved moment."""
    if not is_bookmark_id(bookmark_id):
        raise ValueError(f"not a bookmark id: {bookmark_id!r}")
    return {"PK": project_pk(pid), "SK": BOOKMARK_SK_PREFIX + bookmark_id}


def src_guard_key(pid: str, bucket: str, key: str) -> Key:
    """``PROJECT#<pid> / SRC#<sha256(bucket + "/" + key)>``: at most one asset per bucket+key (Req 2.14).

    The guard is a hash, so a 1,024-byte S3 key never meets DynamoDB's 1,024-byte sort-key limit.
    """
    if not isinstance(bucket, str) or not bucket:
        raise ValueError("source bucket must be a non-empty string")
    if not isinstance(key, str) or not key:
        raise ValueError("source key must be a non-empty string")
    return {"PK": project_pk(pid), "SK": SRC_SK_PREFIX + _sha256_hex(f"{bucket}/{key}")}


def hash_guard_key(pid: str, algo: str, value: str, size: int) -> Key:
    """``PROJECT#<pid> / HASH#<algo>:<value>:<size>``: at most one asset per content in a Project (R5).

    ``value`` is the S3 checksum as S3 reports it (base64, at most ~48 chars for SHA-256), so the key fits.
    """
    if not isinstance(algo, str) or not algo or ":" in algo:
        raise ValueError("checksum algorithm must be a non-empty string without ':'")
    if not isinstance(value, str) or not value:
        raise ValueError("checksum value must be a non-empty string")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError(f"size must be a non-negative int, got {size!r}")
    return {"PK": project_pk(pid), "SK": f"{HASH_SK_PREFIX}{algo}:{value}:{size}"}


def job_key(pid: str, job_id: str) -> Key:
    """``PROJECT#<pid> / JOB#<jobId>``: one M2C job whose outputs the Project tracks (R4.1)."""
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job id must be a non-empty string")
    return {"PK": project_pk(pid), "SK": JOB_SK_PREFIX + job_id}


def dname_guard_key(pid: str, display_name: str) -> Key:
    """``PROJECT#<pid> / DNAME#<sha256(fold(displayName))>``: per-Project name uniqueness (Req 3.5, 3.6).

    Exists only while the asset is not ``removed``; the guard is hashed so a 255-code-point name fits.
    """
    if not isinstance(display_name, str) or not display_name:
        raise ValueError("display name must be a non-empty string")
    return {"PK": project_pk(pid), "SK": DNAME_SK_PREFIX + _sha256_hex(fold_name(display_name))}


def queue_key(pid: str, queued_at_ms: int, asset_id: str) -> Key:
    """``PROJECT#<pid> / QUEUE#<queuedAtMs:013d>#<assetId>``: the Dispatcher's scheduler input (design §7).

    Zero-padding the millisecond timestamp makes a lexical sort chronological, ties broken by ascending
    ``asset_id`` (Req 4.5).
    """
    return {"PK": project_pk(pid), "SK": _queue_sk(QUEUE_SK_PREFIX, queued_at_ms, asset_id)}


def failidx_key(pid: str, failed_at_ms: int, asset_id: str) -> Key:
    """``PROJECT#<pid> / FAILIDX#<failedAtMs:013d>#<assetId>``: newest-failure-first index (Req 3.7)."""
    return {"PK": project_pk(pid), "SK": _queue_sk(FAILIDX_SK_PREFIX, failed_at_ms, asset_id)}


def _queue_sk(prefix: str, at_ms: int, asset_id: str) -> str:
    if not isinstance(at_ms, int) or isinstance(at_ms, bool) or at_ms < 0:
        raise ValueError(f"timestamp must be a non-negative int ms, got {at_ms!r}")
    if not is_asset_id(asset_id):
        raise ValueError(f"not an asset id: {asset_id!r}")
    return f"{prefix}{at_ms:0{_MS_WIDTH}d}#{asset_id}"


def _non_empty(sub: str) -> str:
    if not isinstance(sub, str) or not sub:
        raise ValueError("actor sub must be a non-empty string")
    return sub


def _project_id(pid: str) -> str:
    if not is_project_id(pid):
        raise ValueError(f"not a project id: {pid!r}")
    return pid


__all__ = [
    "ASSET_ID_PATTERN",
    "ASSET_KINDS",
    "ASSET_SK_PREFIX",
    "BOOKMARK_SK_PREFIX",
    "bookmark_key",
    "is_bookmark_id",
    "COUNTERS_SK",
    "DNAME_SK_PREFIX",
    "FAILIDX_SK_PREFIX",
    "HASH_SK_PREFIX",
    "JOB_SK_PREFIX",
    "Key",
    "MEMBER_SK_PREFIX",
    "META_SK",
    "MIRROR_SK_PREFIX",
    "NAME_SK_PREFIX",
    "PERSONAL_SK",
    "PROJECT_PK_PREFIX",
    "QUEUE_SK_PREFIX",
    "SRC_SK_PREFIX",
    "USER_PK_PREFIX",
    "asset_key",
    "asset_kind",
    "counters_key",
    "dname_guard_key",
    "failidx_key",
    "fold_name",
    "hash_guard_key",
    "is_asset_id",
    "is_tombstone",
    "job_key",
    "member_key",
    "meta_key",
    "mirror_key",
    "name_guard_key",
    "personal_key",
    "pid_from_mirror_sk",
    "project_pk",
    "project_prefix",
    "queue_key",
    "src_guard_key",
    "sub_from_member_sk",
    "tombstone_item",
    "user_pk",
]
