"""S3_Source_Registrar — the checks and registration behind ``register_s3_location`` (design §6, Req 2).

The checks run in Req 2.3 order and only the first failure is reported; no object is listed before the
format, allowlist, and Project_Prefix checks pass. This P0 slice implements:

- the pure format parse (Req 2.5), the allowlist (Req 2.6), and the Project_Prefix (Req 2.7) checks;
- object/prefix enumeration against the Ingest bucket with an injected S3 client, the Supported type +
  size admission (Req 2.2/2.11), and registration of each listed object in ascending key order through
  the shared :meth:`ingestion.store.AssetStore.register_asset` transaction (Req 2.13).

Deferred to a later wave and marked ``TODO(ingestion)`` inline — they need live AWS and do not change
the synth or the P0 unit tests:

- the account + region check (``HeadBucket(ExpectedBucketOwner)`` / ``GetBucketLocation``, Req 2.8);
- the M2C readability check (``iam:SimulatePrincipalPolicy``, Req 2.10);
- registration of a non-Ingest allowlisted bucket (Req 2.6) beyond parsing/allowlisting it.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from projects import schema

from .limits import DEFAULT_UPLOAD_LIMITS, admit, kind_for
from .store import AssetStore, ProjectNotActive, RegistrationConflict, asset_public_row

__all__ = [
    "LocationError",
    "ParsedLocation",
    "parse_s3_location",
    "check_allowlist",
    "check_project_prefix",
    "classify_object",
    "S3LocationRegistrar",
    "VIDEO_MAX_BYTES",
    "AUDIO_MAX_BYTES",
    "IMAGE_MAX_BYTES",
    "DOCUMENT_MAX_BYTES",
    "MAX_LISTED_OBJECTS",
]

# Upload/registration size limits (design §3 "projectUploadLimits"; Req 2.2, 8.3, 12.3). The defaults
# live in `ingestion.limits` (shared with the proxy twin) and are overridable by
# INGESTION_CONFIG.uploadLimits; these names stay for callers that read the defaults.
VIDEO_MAX_BYTES = DEFAULT_UPLOAD_LIMITS["video"]
AUDIO_MAX_BYTES = DEFAULT_UPLOAD_LIMITS["audio"]
IMAGE_MAX_BYTES = DEFAULT_UPLOAD_LIMITS["image"]
DOCUMENT_MAX_BYTES = DEFAULT_UPLOAD_LIMITS["document"]
MAX_LISTED_OBJECTS = 200

_S3_KEY_MAX_BYTES = 1024
# S3 bucket naming (the subset we accept): 3-63 chars, lowercase alnum/dot/hyphen, start/end alnum.
_BUCKET_RE = re.compile(r"^(?=.{3,63}$)[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

class LocationError(Exception):
    """A domain refusal of a submitted S3 location. ``code`` + ``message`` map to the Asset_Service reply."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ParsedLocation:
    bucket: str
    key: str  # "" for a bucket-only URI
    is_prefix: bool


def parse_s3_location(raw: object) -> ParsedLocation:
    """Parse + validate an ``s3://bucket[/key]`` location (Req 2.5). Raises :class:`LocationError`."""
    if not isinstance(raw, str):
        raise LocationError("invalid_location", "A location must be an s3:// URI")
    s = raw.strip()
    if not s.lower().startswith("s3://"):
        raise LocationError("invalid_location", "A location must begin with s3://")
    rest = s[len("s3://") :]
    bucket, slash, key = rest.partition("/")
    if not _BUCKET_RE.match(bucket) or ".." in bucket or _IPV4_RE.match(bucket):
        raise LocationError("invalid_location", "The bucket name is not a valid S3 bucket")
    if len(key.encode("utf-8")) > _S3_KEY_MAX_BYTES:
        raise LocationError("invalid_location", "The key exceeds 1024 bytes")
    is_prefix = (not key) or s.endswith("/")
    return ParsedLocation(bucket=bucket, key=key, is_prefix=is_prefix)


def check_allowlist(loc: ParsedLocation, *, ingest_bucket: str, allowlist: list[str]) -> None:
    """Req 2.6: the bucket must be the Ingest bucket or a name in ``INGESTION_CONFIG.s3Allowlist``."""
    if loc.bucket == ingest_bucket or loc.bucket in set(allowlist or []):
        return
    raise LocationError(
        "bucket_not_allowed",
        "That bucket is not allowed for this environment",
        status=400,
    )


def check_project_prefix(loc: ParsedLocation, *, pid: str, ingest_bucket: str) -> None:
    """Req 2.7: inside the Ingest bucket, the key must begin with ``projects/<pid>/``.

    One message whether or not objects exist, so a probe cannot distinguish an empty prefix from a
    missing one. A non-Ingest allowlisted bucket is exempt (its objects are registered in place).
    """
    if loc.bucket != ingest_bucket:
        return
    required = schema.project_prefix(pid)  # projects/<pid>/
    if not loc.key.startswith(required):
        raise LocationError(
            "outside_project_prefix",
            f"In the Ingest bucket a location must be under {required}",
            status=400,
        )


def classify_object(
    key: str, content_type: Optional[str], size: int, limits: Optional[dict] = None
) -> Optional[str]:
    """The asset kind for a listed object, or ``None`` when its size is not admitted (Req 2.2).

    Every type is a Project_Asset (``document`` for anything that is not video/audio/image), so only
    the per-kind size limit can refuse an object here.
    """
    kind = kind_for(content_type, key)
    return kind if admit(kind, size, limits or DEFAULT_UPLOAD_LIMITS) is None else None


@dataclass
class RegistrationSummary:
    """What one ``register_s3_location`` did, object by object (the proxy returns these lists)."""

    registered: list = field(default_factory=list)  # ASSET rows written by this call
    skipped: list = field(default_factory=list)  # {key, reason}
    errors: list = field(default_factory=list)  # {key, code}
    scanned: int = 0


class S3LocationRegistrar:
    """Runs the Req 2.3 checks, then registers each admitted object under the location."""

    def __init__(
        self,
        store: AssetStore,
        *,
        s3_client: Any,
        ingest_bucket: str,
        allowlist: Optional[list[str]] = None,
        limits: Optional[dict] = None,
    ):
        self._store = store
        self._s3 = s3_client
        self._ingest_bucket = ingest_bucket
        self._allowlist = allowlist or []
        self._limits = limits or dict(DEFAULT_UPLOAD_LIMITS)

    def register(self, *, pid: str, location: str, added_by: str, added_by_display: str) -> dict:
        """Register every object at ``location``; returns ``{registered, skipped, errors, scanned}``.

        ``registered`` holds the ASSET rows written; ``skipped`` holds ``{key, reason}`` with reason
        ``already_registered`` (the SRC guard: the bucket+key is already an asset of this Project),
        ``managed_object`` (a Harness-managed key under ``projects/<pid>/<assetId>/``), ``too_large``,
        ``empty``, or ``limit`` (beyond the first :data:`MAX_LISTED_OBJECTS`); ``errors`` holds
        ``{key, code}`` for an object that could not be read (``not_found``) or written.
        """
        loc = parse_s3_location(location)
        check_allowlist(loc, ingest_bucket=self._ingest_bucket, allowlist=self._allowlist)
        check_project_prefix(loc, pid=pid, ingest_bucket=self._ingest_bucket)
        # TODO(ingestion): account+region check (Req 2.8) and M2C readability (Req 2.10) are live-AWS
        # only (HeadBucket/GetBucketLocation, iam:SimulatePrincipalPolicy) and land with the pipeline
        # wave; a deployed env still gets format/allowlist/prefix enforcement plus registration here.

        summary = RegistrationSummary()
        managed_prefix = schema.project_prefix(pid)
        for obj in self._enumerate(loc, summary):
            summary.scanned += 1
            key = obj["key"]
            if summary.scanned > MAX_LISTED_OBJECTS:
                summary.skipped.append({"key": key, "reason": "limit"})
                continue
            if loc.bucket == self._ingest_bucket and _is_managed_key(key, managed_prefix):
                summary.skipped.append({"key": key, "reason": "managed_object"})
                continue
            kind = kind_for(obj.get("contentType"), key)
            refusal = admit(kind, obj["size"], self._limits)
            if refusal is not None:
                summary.skipped.append({"key": key, "reason": refusal})
                continue
            self._register_one(
                pid=pid,
                kind=kind,
                loc_bucket=loc.bucket,
                obj=obj,
                added_by=added_by,
                added_by_display=added_by_display,
                summary=summary,
            )
        return {
            "registered": summary.registered,
            "skipped": summary.skipped,
            "errors": summary.errors,
            "scanned": summary.scanned,
        }

    def _head(self, bucket: str, key: str, summary: RegistrationSummary) -> Optional[dict]:
        try:
            head = self._s3.head_object(Bucket=bucket, Key=key)
        except Exception:  # noqa: BLE001 — a 403/404 head is "this object cannot be registered"
            summary.errors.append({"key": key, "code": "not_found"})
            return None
        return {
            "key": key,
            "size": int(head.get("ContentLength", 0)),
            "contentType": head.get("ContentType"),
        }

    def _enumerate(self, loc: ParsedLocation, summary: RegistrationSummary):
        """Yield ``{key, size, contentType}`` for the object or every object under the prefix (sorted)."""
        if not loc.is_prefix:
            obj = self._head(loc.bucket, loc.key, summary)
            if obj is not None:
                yield obj
            return
        token: Optional[str] = None
        keys: list[str] = []
        while True:
            kwargs = {"Bucket": loc.bucket, "Prefix": loc.key}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            for entry in resp.get("Contents", []) or []:
                key = entry.get("Key", "")
                if key and not key.endswith("/"):
                    keys.append(key)
            if not resp.get("IsTruncated") or len(keys) > MAX_LISTED_OBJECTS:
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
        for i, key in enumerate(sorted(keys, key=lambda k: k.encode("utf-8"))):
            if i >= MAX_LISTED_OBJECTS:
                # Past the cap: reported as skipped without a HEAD (no metadata needed).
                yield {"key": key, "size": 0, "contentType": None}
                continue
            obj = self._head(loc.bucket, key, summary)
            if obj is not None:
                yield obj

    def _register_one(self, *, pid, kind, loc_bucket, obj, added_by, added_by_display, summary) -> None:
        asset_id = f"{kind}:{uuid.uuid4()}"
        file_name = obj["key"].rsplit("/", 1)[-1]
        try:
            row = self._store.register_asset(
                pid=pid,
                asset_id=asset_id,
                source="s3",
                source_uri=f"s3://{loc_bucket}/{obj['key']}",
                source_bucket=loc_bucket,
                source_key=obj["key"],
                source_size=obj["size"],
                content_type=obj.get("contentType") or "application/octet-stream",
                file_name=file_name,
                added_by=added_by,
                added_by_display=added_by_display,
            )
            summary.registered.append(asset_public_row(row))
        except RegistrationConflict:
            # The SRC guard already exists → this bucket+key is already an asset (Req 2.13-2.15).
            summary.skipped.append({"key": obj["key"], "reason": "already_registered"})
        except ProjectNotActive:
            raise


def _is_managed_key(key: str, project_prefix: str) -> bool:
    """True for bytes the Harness wrote under the Project prefix: ``projects/<pid>/<assetId>/…`` (upload,
    rendition), ``projects/<pid>/graph/…`` (the knowledge graph's Lance tables and ``_meta.json``) and
    ``projects/<pid>/sessions/…`` (session outputs, registered in place by Generated_Media — asset-lineage
    Phase 3 (a))."""
    if not key.startswith(project_prefix):
        return False
    first, slash, _rest = key[len(project_prefix) :].partition("/")
    return bool(slash) and (first in ("graph", "sessions") or schema.is_asset_id(first))


def allowlist_from_env(env: Optional[dict] = None) -> list[str]:
    """``INGESTION_CONFIG.s3Allowlist`` from the hydrated env, or ``[]`` when unset/malformed."""
    import json

    env = os.environ if env is None else env
    raw = env.get("INGESTION_CONFIG", "")
    if not raw:
        return []
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    allow = cfg.get("s3Allowlist") if isinstance(cfg, dict) else None
    return [b for b in allow if isinstance(b, str)] if isinstance(allow, list) else []
