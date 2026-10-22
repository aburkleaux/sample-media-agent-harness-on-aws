/**
 * Which plan node a finished gateway job belongs to — pure, so it is unit-tested
 * (`infra/test/changefeed-plan-node-match.test.ts`) without AWS.
 *
 * Order, most to least certain:
 *   1. `planNodeId` on the job row. The task agent binds the node it is executing
 *      (`jobs.context.set_node_id`), so a job submitted from inside a plan node
 *      is registered with that node.
 *   2. A plan-state node row whose `job_id` is this job. The node writes it at
 *      the END of its run, so it is absent while the node is still running.
 *   3. The only node in `running` — a guess, and only when it cannot be wrong
 *      about WHICH node. Picking "the first running node" among several
 *      parallel ones (8 in session c3447913) marks an unrelated node.
 *
 * Plan-state sort keys are qualified (`<plan_id>#animate_shot2`, plan row `PLAN#<plan_id>`) for
 * new plans, and bare (`animate_shot2`, plan row `PLAN`) for plans written before that. Node ids
 * repeat across plans in a session (sg1..sg5), so the job's own plan is matched FIRST: when it has
 * `<runId>#` rows only those are candidates. Bare rows are the legacy fallback, and a bare row
 * stamped with another plan's `plan_id` is never one.
 */

export interface PlanStateRow {
  nodeKey: string; // the row's sort key, as stored
  status: string;
  jobId: string;
  planId?: string; // the row's plan_id attribute, when stamped
}

export interface FinishedJob {
  jobId: string;
  runId: string;
  nodeId: string | null;
}

export type MatchedBy = "node_id" | "job_id" | "only_running";

export function nodeIdOfKey(nodeKey: string): string {
  return nodeKey.includes("#") ? nodeKey.split("#").slice(1).join("#") : nodeKey;
}

function isPlanRow(nodeKey: string): boolean {
  return nodeKey === "PLAN" || nodeKey.startsWith("PLAN#");
}

/** The rows that can belong to this job's plan: its qualified rows, else its legacy bare rows. */
function candidateRows(rows: PlanStateRow[], runId: string): PlanStateRow[] {
  const nodes = rows.filter((r) => !isPlanRow(r.nodeKey));
  const qualified = nodes.filter((r) => r.nodeKey.startsWith(`${runId}#`));
  if (qualified.length) return qualified;
  return nodes.filter((r) => !r.nodeKey.includes("#") && (!r.planId || r.planId === runId));
}

export function matchPlanNode(
  rows: PlanStateRow[],
  job: FinishedJob,
): { nodeKey: string; by: MatchedBy } | null {
  const nodes = candidateRows(rows, job.runId);
  if (job.nodeId) {
    const hit = nodes.find((r) => nodeIdOfKey(r.nodeKey) === job.nodeId);
    if (hit) return { nodeKey: hit.nodeKey, by: "node_id" };
  }
  const byJob = nodes.find((r) => r.jobId && r.jobId === job.jobId);
  if (byJob) return { nodeKey: byJob.nodeKey, by: "job_id" };
  const running = nodes.filter((r) => r.status === "running");
  if (running.length === 1) return { nodeKey: running[0].nodeKey, by: "only_running" };
  return null;
}
