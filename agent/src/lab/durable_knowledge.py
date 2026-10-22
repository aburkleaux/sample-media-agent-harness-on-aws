"""DurableKnowledge — actor-scoped long-term memory for cross-session preferences.

Persists user preferences and notes to S3 under the namespace
`knowledge/` (S3UserFilesTool prepends `{actor_id}/`, yielding the
effective path `{actor_id}/knowledge/{key}.json`).

Unlike ConversationalMemory (which is session-scoped), Durable_Knowledge
persists across all sessions for the same actor. It is used by the
Lab_Chat_Agent for cross-session preferences and notes.

Requirements: 9.6
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .context import LabRequestContext
from .s3_user_files_tool import S3UserFilesTool

logger = logging.getLogger(__name__)

# The storage prefix within the actor's namespace.
# S3UserFilesTool already prepends `{actor_id}/`, so the effective S3 key
# becomes `{actor_id}/knowledge/{key}.json`.
_KNOWLEDGE_PREFIX = "knowledge/"


class DurableKnowledge:
    """Actor-scoped long-term memory for cross-session preferences and notes.

    All operations are scoped to the current actor_id. The actor_id scoping
    is enforced by the S3UserFilesTool which prepends `{actor_id}/` to all
    keys. This class further scopes to the `knowledge/` namespace within
    the actor's prefix.

    Storage layout:
        {actor_id}/knowledge/{key}.json

    Each entry is stored as a JSON object with the structure:
        {"key": "<key>", "value": <value>}

    Requirement 9.6: Durable_Knowledge entries are scoped under a path
    containing actorId.
    """

    def __init__(self, s3: S3UserFilesTool, ctx: LabRequestContext) -> None:
        """Initialize DurableKnowledge.

        Args:
            s3: The S3UserFilesTool instance (already bound to ctx.actor_id).
            ctx: The LabRequestContext providing actor_id.
        """
        self._s3 = s3
        self._ctx = ctx

    @property
    def actor_id(self) -> str:
        """The actor_id this knowledge store is scoped to."""
        return self._ctx.actor_id

    async def get(self, key: str) -> Optional[Any]:
        """Read a knowledge entry by key.

        Args:
            key: The knowledge entry key (e.g. "preferred_language").

        Returns:
            The stored value if found, None if the entry does not exist.

        Raises:
            ValueError: If key is empty.
        """
        if not key:
            raise ValueError("key must not be empty")

        path = f"{_KNOWLEDGE_PREFIX}{key}.json"

        try:
            data = await self._s3.read(path)
            entry = json.loads(data.decode("utf-8"))
            return entry.get("value")
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(
                "Failed to read knowledge entry key=%s for actor=%s: %s",
                key,
                self._ctx.actor_id,
                str(e),
            )
            return None

    async def put(self, key: str, value: Any) -> None:
        """Write or update a knowledge entry.

        Args:
            key: The knowledge entry key (e.g. "preferred_language").
            value: The value to store. Must be JSON-serializable.

        Raises:
            ValueError: If key is empty.
            TypeError: If value is not JSON-serializable.
        """
        if not key:
            raise ValueError("key must not be empty")

        entry = {"key": key, "value": value}

        try:
            data = json.dumps(entry, indent=2).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise TypeError(
                f"value for key '{key}' is not JSON-serializable: {e}"
            ) from e

        path = f"{_KNOWLEDGE_PREFIX}{key}.json"
        await self._s3.write(path, data)

        logger.info(
            "Persisted knowledge entry key=%s for actor=%s",
            key,
            self._ctx.actor_id,
        )

    async def delete(self, key: str) -> None:
        """Remove a knowledge entry.

        Deleting a non-existent key is a no-op (idempotent).

        Args:
            key: The knowledge entry key to remove.

        Raises:
            ValueError: If key is empty.
        """
        if not key:
            raise ValueError("key must not be empty")

        path = f"{_KNOWLEDGE_PREFIX}{key}.json"
        await self._s3.delete(path)

        logger.info(
            "Deleted knowledge entry key=%s for actor=%s",
            key,
            self._ctx.actor_id,
        )

    async def list_keys(self) -> list[str]:
        """List all knowledge entry keys for the current actor.

        Returns:
            A sorted list of key names (without the .json suffix).
        """
        paths = await self._s3.list(_KNOWLEDGE_PREFIX)

        keys: list[str] = []
        for path in paths:
            # Paths are relative to {actor_id}/, e.g. "knowledge/my_key.json"
            if not path.endswith(".json"):
                continue
            # Strip the prefix and .json suffix to get the key name
            relative = path[len(_KNOWLEDGE_PREFIX):]
            if relative.endswith(".json"):
                key_name = relative[: -len(".json")]
                if key_name:
                    keys.append(key_name)

        keys.sort()
        return keys
