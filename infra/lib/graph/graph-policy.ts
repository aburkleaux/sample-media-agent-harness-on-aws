/**
 * Graph-tool policy builder: one Cedar `permit` per knowledge-graph tool on the project Search_Gateway.
 *
 * Privacy is exactly search's (`../projects/search-policy.ts`): the gateway accepts only a Project_Token, and a
 * graph tool call is permitted only when its `project_id` argument equals the token's `project_id` claim —
 * everything else (other tools, other Projects, a principal without the tag) is Cedar default-deny under the
 * engine's ENFORCE mode. Pure: returns the statement; `Fn.join` when the gateway ARN is a token.
 */
import { Fn, Token } from 'aws-cdk-lib';

export const GRAPH_TARGET_NAME = 'project-graph';

/** `project-graph___<tool>` — the action name the gateway gives a Lambda-target tool. */
export function graphActionName(tool: string): string {
  if (!/^[a-z][a-z0-9_]*$/.test(tool)) throw new Error(`graph tool name ${JSON.stringify(tool)} is not snake_case`);
  return `${GRAPH_TARGET_NAME}___${tool}`;
}

export function buildGraphToolPolicy(tool: string, gatewayArn: string): string {
  const parts = [
    'permit(\n  principal,\n  action == AgentCore::Action::"',
    graphActionName(tool),
    '",\n  resource == AgentCore::Gateway::"',
    gatewayArn,
    '"\n) when {\n' +
      '  principal.hasTag("project_id") &&\n' +
      '  context has input &&\n' +
      '  context.input has project_id &&\n' +
      '  context.input.project_id == principal.getTag("project_id")\n};',
  ];
  return parts.some((p) => Token.isUnresolved(p)) ? Fn.join('', parts) : parts.join('');
}
