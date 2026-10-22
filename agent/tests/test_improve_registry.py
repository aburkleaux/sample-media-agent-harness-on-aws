"""Registry loader: default-deny permissions, word-boundary matching, weighted ranking.

The loader is unit-tested against a small inline YAML rather than the real
`ideas/sigma-registry.yaml`, which lives OUTSIDE the repo (OURS-2) and would make these
tests fail on any other checkout. One skip-guarded test reads the real file when present,
because the loader's contract is ultimately with that document.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.registry import SigmaRegistry, default_registry_path  # noqa: E402

FIXTURE = """
meta:
  head_sha: deadbee
  revalidated: 2026-08-21
prompt_layer:
  - component_id: editable-persona
    type: system_prompt
    source_location: agent/src/lab/specialty.py:1
    versioned: YES (Lab asset lifecycle, immutable vN + working copy)
    mutability: AUTO_CANDIDATE
    blast_radius: single
    known_failure_modes: drifts from the tool descriptions
  - component_id: undecided-prompt
    type: system_prompt
    source_location: agent/src/main.py:611
    versioned: NONE
    blast_radius: global
  - component_id: flat-collection
    type: asset
    source_location: agent/src/lab/tool_collection_crud.py:1
    versioned: NO (flat)
    mutability: REVIEW
    blast_radius: single
  - component_id: recommend-only-store
    type: bucket
    source_location: agent/src/lab/s3.py:1
    versioned: S3 object versioning (coarse, unlabelled, no rollback API)
    mutability: MANUAL
    blast_radius: global
  - component_id: git-only-flags
    type: config
    source_location: infra/lib/env-config.ts:141
    versioned: git only — NO SSM/AppConfig/DDB indirection
    mutability: REVIEW
    blast_radius: global
  - component_id: timestamps-only
    type: allowlist
    source_location: agent/src/lab/gateway.py:1
    versioned: created/updated timestamps only
    mutability: MANUAL
    blast_radius: shared
control_layer:
  - component_id: chainer-readiness-rule
    type: gate
    source_location: infra/lib/lambdas/plan-chainer/handler.py:432-470
    versioned: NONE
    mutability: LOCKED
    blast_radius: single
  - component_id: capability-image-artifact
    type: build
    source_location: agent/capability.Dockerfile
    versioned: NONE
    mutability: LOCKED
    blast_radius: global
    known_failure_modes: feeds THREE independently-deployed artifacts
delta_2026_08_21:
  new_components:
    - component_id: slack-signature-gate
      layer: ingress
      type: validator
      source_location: infra/lib/lambdas/slack-shared/signature.ts:14
      versioned: NONE
      mutability: LOCKED
      blast_radius: global
"""


@pytest.fixture()
def registry(tmp_path):
    path = tmp_path / "sigma.yaml"
    path.write_text(FIXTURE)
    return SigmaRegistry.load(str(path))


def test_loads_core_layers_and_the_delta_block(registry):
    ids = {c.component_id for c in registry.all()}
    assert "editable-persona" in ids
    assert "chainer-readiness-rule" in ids
    # The 34 components added on 2026-08-21 arrive only via `delta_2026_08_21`; a loader
    # reading the four core layer keys alone would silently see a smaller Σ.
    assert "slack-signature-gate" in ids
    assert registry.get("slack-signature-gate").layer == "ingress"


def test_unrecorded_mutability_is_not_editable(registry):
    """`control_decision_unrecorded: 30` in the real registry — every one of those must
    fail closed, or the loop could propose an edit to a component nobody has cleared."""
    assert registry.get("undecided-prompt").mutability == ""
    assert registry.get("undecided-prompt").editable is False
    assert registry.get("undecided-prompt").disposition != "publishable"


def test_locked_is_never_editable_and_platform_code_is_a_code_change(registry):
    chainer = registry.get("chainer-readiness-rule")
    assert chainer.editable is False
    # It lives under infra/lib/lambdas, so the honest proposal is an escalation rather
    # than an advisory note the user might mistake for something they can act on.
    assert chainer.disposition == "code_change"


def test_only_the_auto_classes_are_editable(registry):
    """The registry's own legend says MANUAL is "loop may recommend only; cannot modify", and
    REVIEW gates on a human. Treating either as editable let the proposer emit an applyable
    edit for components the permission matrix explicitly withholds."""
    assert registry.get("editable-persona").editable is True
    assert registry.editable() == [registry.get("editable-persona")]
    for component_id in ("flat-collection", "recommend-only-store", "timestamps-only"):
        component = registry.get(component_id)
        assert component.editable is False, component_id
        assert component.recommend_only is True, component_id
        assert component.disposition != "publishable", component_id


def test_versioned_is_an_allowlist_so_prose_reads_as_unversioned(registry):
    """It was a six-word DENYLIST, so every unrecognised spelling read as VERSIONED — the
    inverse of the intent. On the real registry that promoted six components to publishable,
    including one whose own field says `NO (flat)` and whose promote() raises."""
    assert registry.get("editable-persona").versioned is True   # "YES (Lab asset lifecycle…)"
    assert registry.get("chainer-readiness-rule").versioned is False
    # Every real spelling that must NOT count as a publish path:
    assert registry.get("flat-collection").versioned is False          # NO (flat)
    assert registry.get("recommend-only-store").versioned is False     # no rollback API
    assert registry.get("git-only-flags").versioned is False           # git only
    assert registry.get("timestamps-only").versioned is False          # timestamps only


def test_flat_category_can_never_be_publishable(registry):
    """`ToolCollectionCRUD.promote` raises NotImplementedError, so a publishable proposal
    against a collection is not merely wrong — the platform would reject it."""
    assert registry.get("flat-collection").disposition == "advisory"


def test_platform_code_vetoes_before_the_permission_column(registry):
    """`git-only-flags` sits at infra/lib/env-config.ts. Where the code lives is a harder
    fact than what the permission column says, so it must win."""
    assert registry.get("git-only-flags").disposition == "code_change"


def test_absent_registry_can_degrade_instead_of_crashing(tmp_path):
    """The registry lives outside the repo, so any other clone lacks it. Losing
    already-computed mining results to a FileNotFoundError is the worst trade available."""
    missing = str(tmp_path / "nope.yaml")
    with pytest.raises(FileNotFoundError):
        SigmaRegistry.load(missing)
    degraded = SigmaRegistry.load(missing, missing_ok=True)
    assert len(degraded) == 0
    assert degraded.meta["unavailable"] is True
    assert degraded.match({"anything": 1.0}) == []


def test_match_is_word_boundary_not_substring(registry):
    """Regression: 'depend' matched inside 'in-depend-ently', which attributed a DAG
    ordering failure to a container build artifact."""
    hits = [c.component_id for c, _, _ in registry.match({"depend": 5.0})]
    assert "capability-image-artifact" not in hits


def test_match_prefers_a_specific_name_over_a_generic_declared_hit(registry):
    """`chainer-readiness-rule` declares no failure mode, so it can only ever match on
    its name. It must still outrank a component whose declared text happens to contain a
    low-weight generic term."""
    ranked = registry.match({"readiness": 6.0, "order": 1.0})
    assert ranked, "expected at least one match"
    assert ranked[0][0].component_id == "chainer-readiness-rule"
    assert ranked[0][1] == "keyword_inferred"


def test_declared_hits_are_labelled_and_carry_the_matched_text(registry):
    ranked = registry.match({"drifts": 5.0})
    assert ranked[0][0].component_id == "editable-persona"
    assert ranked[0][1] == "registry_declared"
    assert "drifts" in ranked[0][2]


def test_require_declared_drops_the_weak_tier(registry):
    assert registry.match({"readiness": 6.0}, require_declared=True) == []


def test_empty_terms_match_nothing(registry):
    assert registry.match({}) == []
    assert registry.match([]) == []


@pytest.mark.skipif(not os.path.exists(default_registry_path()),
                    reason="ideas/sigma-registry.yaml lives outside the repo (OURS-2)")
def test_real_registry_loads_and_defaults_deny():
    real = SigmaRegistry.load()
    # The 2026-08-21 revalidation records ~80 components across six layers.
    assert len(real) >= 70
    assert {"prompt", "memory", "tool", "control"} <= set(real.by_layer())
    # Editable must stay a small minority of Σ — if this ever inverts, default-deny broke.
    assert 0 < len(real.editable()) < len(real) / 2
