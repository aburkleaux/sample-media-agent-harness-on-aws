"""Harness-side argument and image-path checks for a Search_Tool call (design Component 2, Req 1.19–1.21).

Pure: no I/O, no network, no AWS. These run before the Project_Token is read, so a rejected call sends
nothing to the Search_Gateway and triggers no token refresh. Both checks fail with the existing ``other``
error category (media-harness-projects-access Req 7.6); no new failure type is introduced.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from . import errors
from .compact import ASSET_KINDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallScope:
    """What the image-path check needs about the current turn.

    An empty ``ingest_bucket`` disables sources (a) and (b); an empty ``proxy_bucket`` disables source (c).
    """

    session_id: str                               # chat session of the turn (arm B: the dispatching turn)
    project_id: str                               # the turn's Active_Project id
    ingest_bucket: str                            # SKILLS_S3_BUCKET; "" ⇒ sources (a) and (b) unavailable
    proxy_bucket: str                             # MEDIA_PROXY_BUCKET; "" ⇒ source (c) unavailable
    frame_keys: Callable[[], "frozenset[str]"]    # frame_s3_key values returned this session under project_id


def check_args(args: dict) -> Optional[str]:
    """The violated rule's message, or ``None`` when the arguments are valid.

    Checked in this order:
    - exactly one of ``search_term`` / ``search_image_s3uri`` is a non-empty ``str``;
    - ``k`` absent, or an ``int`` (not ``bool``) with ``1 <= k <= 50``;
    - ``asset_kinds`` absent, or a non-empty list of distinct values from ``video``, ``image``, ``audio``.
    """
    term = args.get("search_term")
    image = args.get("search_image_s3uri")
    term_ok = isinstance(term, str) and term != ""
    image_ok = isinstance(image, str) and image != ""
    if term_ok == image_ok:
        return "exactly one of search_term and search_image_s3uri must be a non-empty string"

    if "k" in args:
        k = args["k"]
        if isinstance(k, bool) or not isinstance(k, int) or not (1 <= k <= 50):
            return "k must be an integer from 1 to 50"

    if "asset_kinds" in args:
        kinds = args["asset_kinds"]
        if (
            not isinstance(kinds, list)
            or len(kinds) == 0
            or any(kind not in ASSET_KINDS for kind in kinds)
            or len(set(kinds)) != len(kinds)
        ):
            return "asset_kinds must be a non-empty list of distinct values from video, image, audio"

    return None


def check_image_uri(uri: str, scope: CallScope) -> bool:
    """``True`` exactly when ``uri`` is a Search_Image_Source.

    - no ``%``, ``?`` or ``#`` anywhere;
    - starts with the case-sensitive literal ``s3://``;
    - ``bucket, "/", key = uri[5:].partition("/")`` with the ``/`` present;
    - no key segment (``key.split("/")``) is ``""`` or ``".."``;
    - one of:
      (b) ``bucket == ingest_bucket`` and key is ``f"projects/{project_id}/<assetId>/rendition.jpg"``: an
          asset's Search_Rendition. The index embeds renditions, and M2C's search role may read only them, so
          an original (an attached file's "Full S3 URI") is refused here rather than failing at M2C with
          AccessDenied; a search by an image passes ``search_image_asset_id`` and gets its rendition;
      (c) ``bucket == proxy_bucket`` and key in ``frame_keys()``.

    (The former (a), ``artifacts/<session>/input/`` uploads, is gone: M2C cannot read it, and Phase 2 chat
    uploads are project assets with renditions.)

    Bucket equality is exact string equality, so ``"<ingest-bucket>-evil"`` and ``"S3://"`` fail.
    """
    if not isinstance(uri, str):
        return False
    if "%" in uri or "?" in uri or "#" in uri:
        return False
    if not uri.startswith("s3://"):
        return False

    bucket, sep, key = uri[5:].partition("/")
    if sep != "/":
        return False
    if any(seg == "" or seg == ".." for seg in key.split("/")):
        return False

    # (b) the Search_Rendition of an asset of the active project
    if scope.ingest_bucket and bucket == scope.ingest_bucket and scope.project_id:
        parts = key.split("/")
        if len(parts) == 4 and parts[0] == "projects" and parts[1] == scope.project_id \
                and parts[2] and parts[3] == RENDITION_NAME:
            return True

    # (c) a hit's frame from this session, under the turn's Active_Project
    if scope.proxy_bucket and bucket == scope.proxy_bucket:
        try:
            keys = scope.frame_keys()
        except Exception:
            logger.warning("frame_keys() failed for session %s; treating as empty", scope.session_id)
            keys = frozenset()
        if key in keys:
            return True

    return False


RENDITION_NAME = "rendition.jpg"

IMAGE_SOURCE_TEXT = (
    "the query image must be an image of the active project, passed as search_image_asset_id (its asset_id, "
    "e.g. from an attached image's asset_id: line), or a frame from this turn's search results; never a "
    "file's S3 location"
)


def precheck(args: dict, scope: CallScope) -> Optional[tuple[str, str]]:
    """``(errors.OTHER, message)`` for the first failed check, else ``None``.

    ``check_image_uri`` runs only when ``check_args`` passed and ``search_image_s3uri`` is present.
    """
    message = check_args(args)
    if message is not None:
        return errors.OTHER, message

    image = args.get("search_image_s3uri")
    if isinstance(image, str) and image != "":
        if not check_image_uri(image, scope):
            return errors.OTHER, IMAGE_SOURCE_TEXT

    return None
