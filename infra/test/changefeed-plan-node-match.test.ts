/**
 * The plan-state bridge in `lib/lambdas/changefeed-fanout` (session c3447913,
 * plan-meridian-reel-recovery). Run: `npm run test:changefeed-bridge` (node:assert, no framework).
 *
 * The bug: an 8-way parallel plan; a fal job finished while its node was still running (the node
 * polled its own job), so the node row had no `job_id` yet. The bridge fell back to "the first
 * running node" WITHOUT writing the job id, the chainer failed that node "missing job_id", the node
 * then wrote `processing` over it, and the plan hung.
 */
import * as assert from "node:assert/strict";
import { marshall } from "@aws-sdk/util-dynamodb";
import { matchPlanNode, nodeIdOfKey, type PlanStateRow } from "../lib/lambdas/changefeed-fanout/planNodeMatch";

process.env.JOB_FANOUT_QUEUE_URL = process.env.JOB_FANOUT_QUEUE_URL ?? "https://example/queue";
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { classify } = require("../lib/lambdas/changefeed-fanout/handler");

const PLAN = "c86ac937-9b98-4523-9a2c-1e2442a5bd34";
const JOB = "01a1142a-d61a-7731-b64a-08f397b81c09";
const running = (k: string): PlanStateRow => ({ nodeKey: k, status: "running", jobId: "" });

// ---- matchPlanNode ------------------------------------------------------------------------------
{
  const eight = ["PLAN", ...[1, 2, 3, 4, 5, 6, 7, 8].map((i) => `animate_shot${i}`)].map(running);

  // The job row names its node (set by the job-tracking wrap inside the node): exact, despite 8 running.
  assert.deepEqual(matchPlanNode(eight, { jobId: JOB, runId: PLAN, nodeId: "animate_shot2" }),
    { nodeKey: "animate_shot2", by: "node_id" });

  // No node id and several running nodes: refuse to guess (the old code took the first one).
  assert.equal(matchPlanNode(eight, { jobId: JOB, runId: PLAN, nodeId: null }), null);

  // Exactly one running node: the only safe guess.
  const one = [running("PLAN"), { nodeKey: "animate_shot2", status: "running", jobId: "" },
    { nodeKey: "stitch_mux_reel", status: "pending", jobId: "" }];
  assert.deepEqual(matchPlanNode(one, { jobId: JOB, runId: PLAN, nodeId: null }),
    { nodeKey: "animate_shot2", by: "only_running" });

  // A node that already recorded the job id is matched by it (node finished first: the normal order).
  const recorded = [...eight.slice(0, 3), { nodeKey: "animate_shot3", status: "processing", jobId: JOB }];
  assert.deepEqual(matchPlanNode(recorded, { jobId: JOB, runId: PLAN, nodeId: null }),
    { nodeKey: "animate_shot3", by: "job_id" });

  // Qualified keys: only this plan's rows count, and PLAN rows never do.
  const qualified = [running(`PLAN#${PLAN}`), running(`other-plan#animate_shot2`), running(`${PLAN}#animate_shot2`)];
  assert.deepEqual(matchPlanNode(qualified, { jobId: JOB, runId: PLAN, nodeId: "animate_shot2" }),
    { nodeKey: `${PLAN}#animate_shot2`, by: "node_id" });
  assert.equal(nodeIdOfKey(`${PLAN}#animate_shot2`), "animate_shot2");
  assert.equal(nodeIdOfKey("animate_shot2"), "animate_shot2");

  // A node id the plan does not have falls through to the other rules rather than inventing a row.
  assert.equal(matchPlanNode(eight, { jobId: JOB, runId: PLAN, nodeId: "no_such_node" }), null);

  // Two plans in one session with the same node ids (session 7b74d118): the job's own
  // `<runId>#<node>` row wins, never the other plan's row or a legacy bare row.
  const OTHER = "c60cc7cc-0000-0000-0000-000000000000";
  const twoPlans: PlanStateRow[] = [
    running(`PLAN#${OTHER}`), running(`${OTHER}#sg1`), running("sg1"),
    running(`PLAN#${PLAN}`), running(`${PLAN}#sg1`),
  ];
  assert.deepEqual(matchPlanNode(twoPlans, { jobId: JOB, runId: PLAN, nodeId: "sg1" }),
    { nodeKey: `${PLAN}#sg1`, by: "node_id" });
  assert.deepEqual(matchPlanNode(twoPlans, { jobId: JOB, runId: OTHER, nodeId: "sg1" }),
    { nodeKey: `${OTHER}#sg1`, by: "node_id" });
  // The only-running guess is confined to the job's plan too (the other plan's sg1 runs as well).
  assert.deepEqual(matchPlanNode(twoPlans, { jobId: JOB, runId: PLAN, nodeId: null }),
    { nodeKey: `${PLAN}#sg1`, by: "only_running" });
  // And a recorded job id on another plan's row is not this plan's node.
  const recordedElsewhere: PlanStateRow[] = [
    { nodeKey: `${OTHER}#sg2`, status: "processing", jobId: JOB },
    { nodeKey: `${PLAN}#sg2`, status: "processing", jobId: "" },
    { nodeKey: `${PLAN}#sg3`, status: "processing", jobId: "" },
  ];
  assert.equal(matchPlanNode(recordedElsewhere, { jobId: JOB, runId: PLAN, nodeId: null }), null);

  // Legacy plans (no `<runId>#` rows) still settle on their bare rows, skipping a bare row that
  // is stamped with another plan's id.
  const legacy: PlanStateRow[] = [
    running("PLAN"),
    { nodeKey: "sg1", status: "running", jobId: "", planId: OTHER },
    { nodeKey: "sg2", status: "processing", jobId: JOB, planId: PLAN },
  ];
  assert.deepEqual(matchPlanNode(legacy, { jobId: JOB, runId: PLAN, nodeId: null }),
    { nodeKey: "sg2", by: "job_id" });
  assert.equal(matchPlanNode(legacy, { jobId: "other-job", runId: PLAN, nodeId: "sg1" }), null);
  const unstamped: PlanStateRow[] = [running("PLAN"), running("sg1"), running(`${OTHER}#sg1`)];
  assert.deepEqual(matchPlanNode(unstamped, { jobId: JOB, runId: PLAN, nodeId: "sg1" }),
    { nodeKey: "sg1", by: "node_id" });
  console.log("matchPlanNode: ok");
}

// ---- classify: the bridge must see a node job's FAILURE although the UI frame is downgraded ------
{
  const record = (oldImg: Record<string, unknown>, newImg: Record<string, unknown>) => ({
    eventName: "MODIFY",
    dynamodb: { OldImage: marshall(oldImg), NewImage: marshall(newImg) },
  });
  const base = { jobId: JOB, actorId: "a", sessionId: "s", runId: PLAN, nodeId: "animate_shot2",
    planNodeId: "animate_shot2", endpointId: "minimax/h3-max", gatewayId: "harness-user-mcp" };
  const failed = classify(record({ ...base, status: "RUNNING" },
    { ...base, status: "FAILED", terminalAt: "2026-10-07T02:22:22Z" }));
  assert.equal(failed.payload.event, "job.update", "a plan-node failure is not a user-facing terminal");
  assert.equal(failed.terminalTransition, true, "but the bridge still sees the terminal transition");
  assert.equal(failed.payload.data.planNodeId, "animate_shot2");
  assert.equal(failed.payload.data.endpointId, "minimax/h3-max");

  const settled = classify(record({ ...base, status: "SUCCEEDED", terminalAt: "t" },
    { ...base, status: "SUCCEEDED", terminalAt: "t", hasRenderableArtifacts: true }));
  assert.ok(settled === null || settled.terminalTransition === false, "a second write is not a new terminal");
  console.log("classify terminalTransition: ok");
}

// ---- bridgeToPlanState: a job settling on an ALREADY-finished node must re-fire the chainer ------
// Staging 2026-10-09 (plan-shotspeaker01): the chainer used each node's SFN output as its result and
// marked it completed, then deferred the plan's completion because the spawned job's registry row
// was still RUNNING (F2), relying on this bridge to re-fire it. The job settled, the bridge's
// conditional write failed ("already finished"), it wrote nothing — and the plan stayed pending.
(async () => {
  const { bridgeToPlanState } = require("../lib/lambdas/changefeed-fanout/handler");
  const sent: any[] = [];
  const fake = {
    send: async (cmd: any) => {
      sent.push(cmd);
      const name = cmd.constructor.name;
      if (name === "QueryCommand") {
        return { Items: [
          marshall({ session_id: "s1", node_id: `PLAN#${PLAN}`, status: "pending", plan_id: PLAN }),
          marshall({ session_id: "s1", node_id: `${PLAN}#t1`, status: "completed", plan_id: PLAN, job_id: JOB }),
        ] };
      }
      if (name === "UpdateItemCommand" && cmd.input.UpdateExpression.includes(":status")) {
        const err: any = new Error("conditional"); err.name = "ConditionalCheckFailedException"; throw err;
      }
      return {};
    },
  };
  const frame = { terminalTransition: true, payload: { event: "job.update", data: {
    jobId: JOB, runId: PLAN, sessionId: "s1", status: "SUCCEEDED", planNodeId: "t1", gatewayId: "media-workflow-gateway",
  } }, groupId: "g", dedupId: "d" };
  await bridgeToPlanState([frame], fake, "plan-state");
  const updates = sent.filter((c) => c.constructor.name === "UpdateItemCommand");
  assert.equal(updates.length, 2, "the failed status write is followed by a re-arm write");
  const rearm = updates[1].input;
  assert.deepEqual(rearm.Key, { session_id: { S: "s1" }, node_id: { S: `${PLAN}#t1` } });
  assert.ok(!/#st\s*=/.test(rearm.UpdateExpression), "the re-arm never changes the node's status");
  assert.match(rearm.ConditionExpression, /#st IN \(:done, :skipped\)/, "only a finished node is touched");
  assert.match(rearm.UpdateExpression, /updated_at = :now/, "a MODIFY the chainer reacts to");
  console.log("bridgeToPlanState re-arm: ok");

  // ---- a FAILED job's real reason reaches the node row (and the registry node row) --------------
  // Staging plan-shotdiar-render (session cd4e3c19): M2C failed sg2 with "No VAD turns found" (no
  // speech); the bridge wrote `failed` with no `error`, the aggregate said "node failed", and the
  // agent told the user the clip was "too short to diarize".
  const { jobFailureReason } = require("../lib/lambdas/changefeed-fanout/handler");
  const CAUSE = '{"errorMessage":"No VAD turns found after flattening — check VAD output","errorType":"RuntimeError",' +
    '"requestId":"4803ed10","stackTrace":["  File \\"/var/task/lambda_function.py\\", line 211"]}';
  assert.equal(jobFailureReason(CAUSE, "FAILED"),
    "No VAD turns found after flattening — check VAD output (RuntimeError)");
  // A cause the registry truncated mid-stack-trace still yields its message.
  assert.equal(jobFailureReason(CAUSE.slice(0, 130), "FAILED"),
    "No VAD turns found after flattening — check VAD output (RuntimeError)");
  assert.equal(jobFailureReason("Lambda.Unknown: boom", "FAILED"), "Lambda.Unknown: boom");
  assert.equal(jobFailureReason(null, "TIMED_OUT"), "job TIMED_OUT");

  const sent2: any[] = [];
  const fake2 = {
    send: async (cmd: any) => {
      sent2.push(cmd);
      if (cmd.constructor.name === "QueryCommand") {
        return { Items: [
          marshall({ session_id: "s1", node_id: `PLAN#${PLAN}`, status: "pending", plan_id: PLAN }),
          marshall({ session_id: "s1", node_id: `${PLAN}#sg2_diarize_speech`, status: "running", plan_id: PLAN }),
        ] };
      }
      return {};
    },
  };
  process.env.JOB_REGISTRY_TABLE = "job-registry";
  await bridgeToPlanState([{ terminalTransition: true, payload: { event: "job.update", data: {
    jobId: JOB, runId: PLAN, sessionId: "s1", status: "FAILED", planNodeId: "sg2_diarize_speech",
    gatewayId: "__aws_sfn__", lastError: CAUSE,
  } }, groupId: "g", dedupId: "d" }], fake2, "plan-state");
  const [nodeWrite, registryWrite] = sent2.filter((c) => c.constructor.name === "UpdateItemCommand").map((c) => c.input);
  assert.match(nodeWrite.UpdateExpression, /#err = :err/, "the node row records the failure reason");
  assert.equal(nodeWrite.ExpressionAttributeNames["#err"], "error");
  assert.equal(nodeWrite.ExpressionAttributeValues[":err"].S,
    "No VAD turns found after flattening — check VAD output (RuntimeError)");
  assert.equal(nodeWrite.ExpressionAttributeValues[":status"].S, "failed");
  assert.match(registryWrite.UpdateExpression, /lastError = :err/);
  console.log("bridgeToPlanState failure reason: ok");
  console.log("changefeed-plan-node-match: all assertions passed");
})().catch((e) => { console.error(e); process.exit(1); });
