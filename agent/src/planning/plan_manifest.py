"""Plan Manifest — structured output registry for plan executions.

A single JSON document per plan run that accumulates structured outputs
from each node as they complete. Downstream nodes read their dependencies'
values from the manifest rather than parsing free-form text.

The manifest lives at:
    s3://{bucket}/plans/{session_id}/{plan_id}/manifest.json

Design inspired by Step Functions' ResultPath: each node writes a typed
value to a known key, and consumers reference it by node_id + output_key.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

logger = logging.getLogger(__name__)


class PlanManifest:
    """Read/write interface for a plan's output manifest."""

    def __init__(self, *, bucket: str, session_id: str, plan_id: str, region: str = "us-west-2"):
        self._bucket = bucket
        self._session_id = session_id
        self._plan_id = plan_id
        self._key = f"plans/{session_id}/{plan_id}/manifest.json"
        self._s3 = boto3.client("s3", region_name=region)

    @property
    def s3_uri(self) -> str:
        return f"s3://{self._bucket}/{self._key}"

    def create(self, *, display_name: str, goal: str, nodes: list[dict]) -> dict:
        """Create the initial manifest with empty outputs for all nodes."""
        manifest = {
            "plan_id": self._plan_id,
            "display_name": display_name,
            "goal": goal,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "nodes": {n["id"]: {"output_key": n.get("output_key", ""), "output_type": n.get("output_type", "text")} for n in nodes},
            "outputs": {},
        }
        self._write(manifest)
        return manifest

    def read(self) -> Optional[dict]:
        """Read the current manifest. Returns None if it doesn't exist."""
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._key)
            return json.loads(resp["Body"].read().decode("utf-8"))
        except self._s3.exceptions.NoSuchKey:
            return None
        except Exception:
            logger.warning("Failed to read manifest at %s", self._key, exc_info=True)
            return None

    def write_node_output(
        self,
        node_id: str,
        *,
        value: Any,
        status: str = "completed",
        output_type: Optional[str] = None,
    ) -> dict:
        """Write a node's structured output to the manifest.

        Reads the current manifest, updates the node's output, and writes back.
        Not concurrency-safe (single writer per node is the design assumption).
        """
        manifest = self.read() or {}
        if "outputs" not in manifest:
            manifest["outputs"] = {}

        # Determine output_type from the node definition if not provided
        if output_type is None:
            node_meta = manifest.get("nodes", {}).get(node_id, {})
            output_type = node_meta.get("output_type", "text")

        manifest["outputs"][node_id] = {
            "status": status,
            "output_type": output_type,
            "value": value,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Check if all nodes are complete
        all_node_ids = set(manifest.get("nodes", {}).keys())
        completed_ids = set(manifest.get("outputs", {}).keys())
        if all_node_ids and all_node_ids <= completed_ids:
            all_ok = all(
                manifest["outputs"][nid].get("status") == "completed"
                for nid in all_node_ids
            )
            manifest["status"] = "completed" if all_ok else "partial"

        self._write(manifest)
        return manifest

    def get_dependency_values(self, depends_on: list[str]) -> dict[str, Any]:
        """Get the output values for a list of dependency node IDs.

        Returns a dict of {node_id: value} for each completed dependency.
        Missing or failed dependencies are omitted.
        """
        manifest = self.read()
        if not manifest:
            return {}
        outputs = manifest.get("outputs", {})
        result = {}
        for dep_id in depends_on:
            dep_output = outputs.get(dep_id, {})
            if dep_output.get("status") == "completed" and "value" in dep_output:
                result[dep_id] = dep_output["value"]
        return result

    def _write(self, manifest: dict) -> None:
        """Write the manifest to S3."""
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key,
            Body=json.dumps(manifest, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
        )
