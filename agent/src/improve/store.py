"""A suggestion store, file-backed, so a finding does not vanish when its evidence expires.

The pipeline is a pure function of its input, and the input scrolls. Plan-state has a 24h TTL,
alarm history keeps ~13 days, and a harvest is a snapshot. So:

    day 1   a run fails, the loop mines it, a suggestion appears
    day 3   plan-state expires, the DAG shape is gone
    day 4   the loop runs, cannot re-derive the pattern
            -> the suggestion DISAPPEARS

Not fixed, not dismissed: un-found, silently, because the evidence aged out. That is the worst
failure mode this surface can have, and it already happened once in practice when a new harvest
replaced the one our only real finding came from.

So the pipeline produces OBSERVATIONS and this holds FINDINGS. Upsert by `proposal_id`, which is
already a deterministic `sha256(component | shape)`, so the same defect seen again lands on the
same row instead of duplicating.

Two staleness signals are recorded and neither is treated as resolution:

    reproducing=False   the last run did not find it again. Could be low traffic, could be
                        expired evidence, could be genuinely fixed. Not a conclusion.
    (Group 1)           the target's source changed after the run. Also not a conclusion.

Deliberately a JSON file: this is local scratch until step 2 replaces it with DynamoDB. The
interface is the part meant to survive — `upsert_all` / `all_suggestions` map onto a
conditional PutItem and a Query, so the swap is a transport change rather than a redesign.

The one correctness rule: a pipeline run must never clobber a human decision. `status` and
`firstSeenAt` are preserved on every upsert; everything else is refreshed from the new
observation.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Iterable, Optional

# Fields a pipeline run must NOT overwrite. `status` is the human's; `firstSeenAt` is history.
#
# `dismissReason` used to be here too and is gone with the method that wrote it: nothing in this
# repo can produce one. A dismissal is per-actor and lives in the served store's `SUGG#<id>` overlay
# (`capability.improve_store.put_dismissal`), never on this producer-private row — and preserving a
# field no writer can set is a rule about nothing. `dto.GLOBAL_WITHHELD_FIELDS` still DETECTS the
# name, which is a different job: that guards a bundle handed in from outside this code.
_PRESERVED = ("status", "firstSeenAt")

DEFAULT_STORE_NAME = "suggestions_store.json"


def default_store_path() -> str:
    override = os.environ.get("IMPROVE_STORE")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(repo_root, "tests", "evaluation", "experiments", DEFAULT_STORE_NAME)


class SuggestionStore:
    """Suggestions that persist across pipeline runs."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_store_path()
        self._rows: dict = {}
        self._load()

    # ---- persistence --------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._rows = {}
            return
        with open(self.path) as f:
            payload = json.load(f)
        self._rows = payload.get("suggestions") or {}

    def save(self) -> None:
        """Atomic write: a crash mid-save must not leave a truncated store, because the store is
        now the only copy of a finding whose evidence may have expired."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        directory = os.path.dirname(self.path)
        handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as f:
                json.dump({"version": 1, "suggestions": self._rows}, f, indent=2, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    # ---- writes -------------------------------------------------------------
    def upsert_all(self, suggestions: Iterable[dict], *, seen_at: str) -> dict:
        """Record this run's observations. Returns a summary of what changed.

        Every stored suggestion NOT in this batch is marked `reproducing=False` and kept. That is
        the whole point: absence from one run is information, not grounds for deletion.
        """
        batch = {s["suggestionId"]: s for s in suggestions}
        added, updated = [], []

        for suggestion_id, observed in batch.items():
            existing = self._rows.get(suggestion_id)
            row = dict(observed)
            row["lastSeenAt"] = seen_at
            row["reproducing"] = True
            if existing is None:
                row["firstSeenAt"] = seen_at
                row.setdefault("status", "open")
                added.append(suggestion_id)
            else:
                # A pipeline run refreshes the evidence and never the decision.
                for field in _PRESERVED:
                    if field in existing:
                        row[field] = existing[field]
                row.setdefault("firstSeenAt", seen_at)
                row.setdefault("status", "open")
                updated.append(suggestion_id)
            self._rows[suggestion_id] = row

        quiet = [sid for sid in self._rows if sid not in batch]
        for suggestion_id in quiet:
            self._rows[suggestion_id]["reproducing"] = False

        return {"added": added, "updated": updated, "quiet": quiet,
                "total": len(self._rows)}

    # ---- reads --------------------------------------------------------------
    def all_suggestions(self, include_dismissed: bool = False) -> list[dict]:
        """Every stored suggestion, newest first.

        There is deliberately NO time filter. The runs have a window; the findings must not, or
        the disappearance this store exists to prevent comes back wearing a filter.
        """
        rows = [
            dict(row) for row in self._rows.values()
            if include_dismissed or row.get("status") != "dismissed"
        ]
        return sorted(rows, key=lambda r: r.get("lastSeenAt") or "", reverse=True)

    def __len__(self) -> int:
        return len(self._rows)
