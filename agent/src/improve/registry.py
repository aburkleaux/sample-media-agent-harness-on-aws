"""Read-only index over ``ideas/sigma-registry.yaml`` — the Σ component inventory.

The registry is the only place that records, per component, whether it is versioned,
what its blast radius is, and who may change it. The proposer consults it to answer
two questions: *which component does this failure belong to* (via
``known_failure_modes``) and *what am I allowed to propose for it* (via
``mutability``).

Default-deny is the load-bearing rule. The 2026-08-21 revalidation records
``control_decision_unrecorded: 30`` — components with no mutability decision. An unset,
unrecognised, or LOCKED* mutability yields ``editable=False``, so a component the
registry has not explicitly cleared can never be proposed as a user edit.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Component layers carrying per-component entries. `ingress`/`infra` arrive only via
# the delta block and are read from there.
_CORE_LAYER_KEYS = ("prompt_layer", "memory_layer", "tool_layer", "control_layer")

# Mutability is not one permission but two, and collapsing them was wrong.
#
# The registry's own legend (`permission_matrix`) says MANUAL is "loop may recommend only;
# cannot modify", and REVIEW likewise gates on a human. Treating either as editable let the
# proposer emit an applyable edit for `skills-store-s3`, `gateway-tool-allowlist` and
# `tool-collection-asset` — components the matrix explicitly withholds. Only the two
# auto-classes may carry a publishable proposal.
_AUTO_PROPOSABLE = frozenset({"THE_ACTUATOR", "AUTO_CANDIDATE"})
_RECOMMEND_ONLY = frozenset({"REVIEW", "MANUAL"})

# `versioned` must be an ALLOWLIST, not a denylist.
#
# It was a six-word denylist, so every unrecognised spelling read as VERSIONED — the exact
# inverse of the intent. Measured against the real registry that promoted six components to
# "publishable", including `tool-collection-asset`, whose own field reads `NO (flat)` and
# whose `promote` raises NotImplementedError. A proposal against it is not merely wrong, it
# is unexecutable.
#
# The document's convention is an explicit `YES` prefix for a real Lab lineage (immutable
# vN + working copy) and `NONE`/`NO`/prose for everything else. Requiring that prefix and
# vetoing any negation admits exactly the three genuine lineages (`custom-agent-persona`,
# `lab-user-skill-bodies`, `lab-asset-index`) and rejects git tags, ECR tags, S3 object
# versioning "with no rollback API", payload schema versions, and 24h-TTL plan rows.
_VERSIONED_AFFIRMATIVE = ("yes",)
_VERSIONED_NEGATIONS = (
    "none", " no ", "no(", "no (", "n/a", "flat", "no version", "no rollback",
    "not versioned", "opaque", "timestamps only",
)

# Categories that carry real version lineages in the Lab, so a proposal against them
# can be published and rolled back. Mirrors `lab/_canonical_ops._VERSIONED_CATEGORIES`
# — tool collections and hooks are FLAT and deliberately absent.
PUBLISHABLE_TARGET_TYPES = frozenset({"skill", "specialty-agent", "workflow"})


@dataclass(frozen=True)
class Component:
    component_id: str
    layer: str
    type: str = ""
    source_location: str = ""
    mutability: str = ""
    blast_radius: str = ""
    versioned_raw: str = ""
    hot_swappable_raw: str = ""
    known_failure_modes: str = ""
    notes: str = ""

    @property
    def declared_failure_mode(self) -> str:
        """The registry's own ``known_failure_modes`` sentence, bounded. ONE definition.

        This is the text an attribution QUOTES as its warrant (``SigmaRegistry.match`` returns it
        as ``matched_failure_mode`` for a ``registry_declared`` hit) and the same text the Σ map
        publishes on the component's own row. One property rather than two call sites, so the
        sentence a suggestion says it matched is byte-identical to the sentence shown against the
        component it blames — otherwise a reader comparing the two would be comparing two
        truncations.

        ``""`` when the registry declares no failure modes for this component, which is 37 of the
        80 rows in the real document: nothing to quote, and the attribution basis says so.
        """
        return first_sentence(self.known_failure_modes)

    @property
    def notes_summary(self) -> str:
        """The first sentence of ``notes``, bounded by the same rule.

        Bounded because ``notes`` is free prose (up to 375 characters in the real registry) and it
        travels on the one item every actor reads, where the whole Σ map's bytes come out of the
        suggestion list's allowance. The first sentence is what makes a ``keyword_inferred``
        attribution checkable — ``match`` searches ``notes`` for the weak basis — and the rest is
        commentary a reader can get from ``sourceLocation``.
        """
        return first_sentence(self.notes)

    @property
    def editable(self) -> bool:
        """May the loop propose an APPLYABLE edit? Only the two auto-classes."""
        return self.mutability.strip().upper() in _AUTO_PROPOSABLE

    @property
    def recommend_only(self) -> bool:
        """The registry permits a recommendation but not a change (REVIEW, MANUAL)."""
        return self.mutability.strip().upper() in _RECOMMEND_ONLY

    @property
    def versioned(self) -> bool:
        """Does this have a real version lineage we could publish to and roll back?

        Allowlist with a veto: an explicit affirmative marker AND no negation anywhere in
        the value. Ambiguity reads as UNVERSIONED, which downgrades a proposal to advisory
        rather than promising a publish path that does not exist.
        """
        raw = str(self.versioned_raw).strip().lower()
        if not raw:
            return False
        if any(negation in raw for negation in _VERSIONED_NEGATIONS):
            return False
        return raw.startswith(_VERSIONED_AFFIRMATIVE)

    @property
    def disposition(self) -> str:
        """What kind of proposal this component can carry.

        publishable — a user can edit and publish a new version
        advisory    — surfaced for information; no write path today
        code_change — lives in platform code; needs an engineer (the `fix_first` and
                      LOCKED cases, incl. the chainer Lambdas)

        Platform code is checked FIRST and vetoes. Previously `editable and versioned` was
        tested first, so `capability-source-flags` — a CDK TypeScript file at
        `infra/lib/env-config.ts` — came out `publishable` because its mutability was
        REVIEW and the old fail-open `versioned` said yes. Where the code lives is a harder
        fact than what the permission column says, so it wins.
        """
        if self._is_platform_code():
            return "code_change"
        if self.editable and self.versioned:
            return "publishable"
        return "advisory"

    def _is_platform_code(self) -> bool:
        loc = (self.source_location or "").lower()
        return (loc.startswith("infra/") or "/lambdas/" in loc
                or loc.endswith(".ts") or loc.startswith("agent/capability.dockerfile")
                or self.layer in {"ingress", "infra"})


def _json_safe(value):
    """Coerce YAML scalars that are not JSON-serializable into strings.

    An unquoted ``2026-08-21`` in YAML is a ``datetime.date``, not a string. It reaches
    a Proposal's provenance and then ``json.dump``, which raises. The real registry
    happens to quote its dates today, so this is a latent break that a future edit —
    removing one pair of quotes — would trigger. Normalising on load keeps "meta values
    are JSON-safe" a property of this loader rather than of the document's punctuation.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _as_text(value) -> str:
    """Registry fields are variously str, list or None. Flatten for matching."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def default_registry_path() -> str:
    override = os.environ.get("IMPROVE_SIGMA_REGISTRY")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(os.path.dirname(repo_root), "ideas", "sigma-registry.yaml")


class SigmaRegistry:
    """Loaded view of the registry. Read-only by construction — no setters, and the
    loop is never given a write path to its own permission model (REQ-019)."""

    def __init__(self, components: list[Component], meta: dict) -> None:
        self._components = components
        self._by_id = {c.component_id: c for c in components}
        self.meta = meta

    # ---- construction -------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None, missing_ok: bool = False) -> "SigmaRegistry":
        """Load the registry.

        ``missing_ok`` returns an EMPTY registry instead of raising when the file is
        absent, which a clone without the registry file will be. An empty registry
        attributes nothing, which surfaces as unattributed signatures rather than as a
        crash that discards mining results already computed.

        A ``.json`` path is read with the stdlib ``json`` module instead of PyYAML. This is
        the shape the in-cloud producer (the ``improve-worker`` Lambda) ships and points
        ``IMPROVE_SIGMA_REGISTRY`` at: the managed ``python3.13`` runtime bundles boto3 but
        NOT PyYAML, so a ``.yaml`` load would crash cold start. JSON is a strict subset of
        YAML for this document (``json.dump`` of ``yaml.safe_load`` output), so the two
        loaders produce identical ``Component`` sets — verified by component-count parity.
        The ``import yaml`` is reached ONLY on the ``.yaml`` branch, so importing this module
        stays stdlib-only (capability-API isolation) and the JSON path never needs PyYAML.
        """
        path = path or default_registry_path()
        if missing_ok and not os.path.exists(path):
            return cls([], {"unavailable": True, "path": path})
        if path.endswith(".json"):
            import json
            with open(path) as f:
                doc = json.load(f)
        else:
            import yaml  # PyYAML ships with the laptop deps; kept function-scoped and on this
                         # branch only so the module stays stdlib-only (capability-API isolation).
            with open(path) as f:
                doc = yaml.safe_load(f)
        components: list[Component] = []
        meta = _json_safe(doc.get("meta") or {})
        for key in _CORE_LAYER_KEYS:
            layer = key.replace("_layer", "")
            for raw in doc.get(key) or []:
                comp = cls._component(raw, default_layer=layer)
                if comp:
                    components.append(comp)
        # The delta block carries the 34 components added on 2026-08-21, each already
        # naming its own layer (incl. the new `ingress` / `infra`).
        for raw in (doc.get("delta_2026_08_21") or {}).get("new_components") or []:
            comp = cls._component(raw, default_layer=str(raw.get("layer") or "unknown"))
            if comp and comp.component_id not in {c.component_id for c in components}:
                components.append(comp)
        return cls(components, meta)

    @staticmethod
    def _component(raw: dict, default_layer: str) -> Optional[Component]:
        if not isinstance(raw, dict):
            return None
        component_id = str(raw.get("component_id") or "").strip()
        if not component_id:
            return None
        return Component(
            component_id=component_id,
            layer=str(raw.get("layer") or default_layer),
            type=_as_text(raw.get("type")),
            source_location=_as_text(raw.get("source_location")),
            mutability=_as_text(raw.get("mutability")),
            blast_radius=_as_text(raw.get("blast_radius")),
            versioned_raw=_as_text(raw.get("versioned")),
            hot_swappable_raw=_as_text(raw.get("hot_swappable")),
            known_failure_modes=_as_text(raw.get("known_failure_modes")),
            notes=_as_text(raw.get("notes")),
        )

    # ---- queries ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._components)

    def all(self) -> list[Component]:
        return list(self._components)

    def get(self, component_id: str) -> Optional[Component]:
        return self._by_id.get(component_id)

    def editable(self) -> list[Component]:
        return [c for c in self._components if c.editable]

    def by_layer(self) -> dict:
        out: dict = {}
        for c in self._components:
            out.setdefault(c.layer, []).append(c)
        return out

    def match(self, terms, require_declared: bool = False) -> list[tuple]:
        """Components matching the weighted search terms, strongest first.

        ``terms`` is ``{term: weight}`` (a bare iterable is accepted and weighted 1.0).
        Weights exist because the terms are not equally diagnostic: "readiness" or
        "chainer" name a subsystem, while "depend" or "order" appear in half the
        registry.

        Returns ``[(Component, basis, matched_text)]``. ``registry_declared`` means the
        hit was inside ``known_failure_modes`` — the registry asserting this about
        itself — and is worth double; ``keyword_inferred`` matched only the id, type,
        notes or path and is a hypothesis until a human confirms it. The multiplier is
        deliberately a factor rather than a large constant, so one strong, specific
        keyword hit can still outrank a weak declared hit on a generic word.

        Matching is on WORD BOUNDARIES. Substring matching made ``"depend"`` match
        ``"in-depend-ently"``, which attributed a DAG ordering failure to a container
        build artifact.
        """
        weights = dict(terms) if isinstance(terms, dict) else {t: 1.0 for t in terms}
        weights = {str(t).lower(): float(w) for t, w in weights.items() if t}
        if not weights:
            return []
        patterns = {t: re.compile(r"\b" + re.escape(t)) for t in weights}

        scored: list[tuple] = []
        for comp in self._components:
            declared = comp.known_failure_modes.lower()
            weak = " ".join(
                (comp.component_id, comp.type, comp.notes, comp.source_location)).lower()
            hit_declared = [t for t, p in patterns.items() if p.search(declared)]
            hit_weak = [t for t, p in patterns.items() if p.search(weak)]
            if hit_declared:
                score = 2.0 * sum(weights[t] for t in hit_declared)
                basis, matched = "registry_declared", comp.declared_failure_mode
            elif hit_weak and not require_declared:
                score = sum(weights[t] for t in hit_weak)
                basis, matched = "keyword_inferred", ""
            else:
                continue
            # A component whose own NAME carries the term is more likely to be about it
            # than one that merely mentions it in prose.
            if any(patterns[t].search(comp.component_id.lower()) for t in hit_weak):
                score += 1.5
            scored.append((score, comp, basis, matched))

        scored.sort(key=lambda row: (-row[0], row[1].component_id))
        return [(comp, basis, matched) for _, comp, basis, matched in scored]


def first_sentence(text: str, limit: int = 220) -> str:
    """First sentence of a registry prose field, whitespace-collapsed and capped at ``limit``.

    Public because the Σ map on the wire has to spell a component's declared failure mode exactly
    as an attribution quoting it does — see ``Component.declared_failure_mode``.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    cut = re.split(r"(?<=[.;])\s", text)[0]
    return cut[:limit]
