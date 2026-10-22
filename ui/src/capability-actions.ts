/**
 * Which lab actions the Capability API serves, and which need the agent runtime.
 *
 * This is the client half of a split defined in
 * `agent/src/capability_api/lab_actions.py`. Both halves must agree: the server
 * refuses an action it does not serve with a 400, so a client that sends a
 * data-plane action to the control plane gets an error rather than an answer, and
 * a client that sends a control-plane action to the runtime merely pays a cold
 * start it did not need to. `capability-actions.contract.test.ts` parses the Python
 * and asserts equality, because a duplicated list is only safe while something
 * checks it.
 *
 * The split is by **dependency, not verb**. `PublishAsset` writes and is served
 * here; `ResolveAgentConfig` reads and is not, because it resolves tool bindings
 * against a live gateway projection. `Create`/`UpdateToolCollectionDraft` are the
 * subtle ones — they validate tool refs against the gateway, so they stay on the
 * runtime even though their siblings on the same category do not.
 *
 * No imports on purpose: this module is the one thing both `lab-api` and
 * `capability-api` may depend on without risking a cycle.
 */

/** Actions the Capability API can serve — pure DynamoDB + S3, no model, no gateway. */
export const CONTROL_PLANE_ACTIONS: ReadonlySet<string> = new Set([
  // Skills
  'CreateSkillDraft',
  'ImportSkillPackage',
  'ReadSkillDraft',
  'ReadPublishedSkill',
  'UpdateSkillMd',
  'PutSupportingFile',
  'DeleteSupportingFile',
  'DeleteSkillDraft',
  'ListSkillDrafts',
  // Custom agents
  'CreateSpecialtyAgentDraft',
  'ReadSpecialtyAgentDraft',
  'UpdateSpecialtyAgentDraft',
  'DeleteSpecialtyAgentDraft',
  'ListSpecialtyAgentDrafts',
  'ResolvePublishedAgentRef',
  // Tool collections — reads and delete only; create/update validate against
  // the gateway and belong to the runtime.
  'ReadToolCollectionDraft',
  'DeleteToolCollectionDraft',
  'ListToolCollectionDrafts',
  // Workflows
  'CreateWorkflowDraft',
  'ReadWorkflowDraft',
  'UpdateWorkflowDraft',
  'DeleteWorkflowDraft',
  'ListWorkflowDrafts',
  // Hooks
  'CreateHook',
  'ReadHook',
  'UpdateHook',
  'DeleteHook',
  'ListHooks',
  'ValidateHookCondition',
  // Lifecycle, any category
  'RenameAsset',
  'PublishAsset',
  'EditAsset',
  'DiscardChanges',
  'ListAssetVersions',
  'ListMyPublishedAssets',
]);

/**
 * Whether `action` may be sent to the Capability API.
 *
 * Fail-closed on anything unrecognised, so a newly added action defaults to the
 * runtime — the host that can always serve it, at worst slowly. The inverse
 * default would send a new gateway-dependent action somewhere it cannot work.
 */
export function isControlPlaneAction(action: string): boolean {
  return CONTROL_PLANE_ACTIONS.has(action);
}
