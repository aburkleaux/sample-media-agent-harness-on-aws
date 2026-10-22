/**
 * Emergency kill switch for the external invoke path (`POST /invoke/:agentId`)
 * — task 17.1, Requirements 17.2 / 17.3 / 17.4.
 *
 * Two independent, ADMIN-controlled switches:
 *
 *   * **global**    — when active, EVERY external invocation is rejected
 *                     (Req 17.3). An operator's platform-wide emergency stop.
 *   * **per-agent** — when an `agentId` is on the disabled set, invocations for
 *                     that one agent are rejected (Req 17.2).
 *
 * The switch is checked **FIRST** in the invoke middleware order — before the
 * rate limiter and before the agent/version is even resolved (design §Req 17,
 * middleware-order diagram) — so an emergency stop short-circuits with the
 * least possible work and never depends on the agent's own stored config.
 *
 * ── Independent of Owner action (Req 17.4) ──────────────────────────────────
 * The switch state lives HERE, in operator-controlled process state seeded from
 * the environment and mutable at runtime via {@link KillSwitch.setGlobal} /
 * {@link KillSwitch.setAgent}. It is deliberately NOT derived from the agent's
 * published-version `integration_config` (which the Owner controls) nor from
 * the `external_access_enabled` revocation gate (also Owner-driven, Req 16), so
 * an operator can stop external invocation for an agent — or the whole
 * platform — regardless of, and unoverridable by, any Owner setting.
 *
 * NOTE (scope): like the rate limiter and stream guard, this state is
 * per-proxy-instance. A control-plane-backed distributed switch (e.g. a flag in
 * DynamoDB/SSM polled or pushed to every task) is the production hardening path;
 * the injectable, mutable surface here is designed so that swap is localized —
 * an admin toggle route or a config poller simply calls `setGlobal`/`setAgent`.
 */

/** Which switch blocked a request (global checked before per-agent). */
export type KillSwitchScope = "global" | "agent";

/** Result of evaluating the kill switch for one `agentId`. */
export interface KillSwitchDecision {
  /** True when the request must be rejected. */
  blocked: boolean;
  /** Which switch blocked it (only set when `blocked === true`). */
  scope?: KillSwitchScope;
}

export interface KillSwitch {
  /**
   * Evaluate both switches for `agentId`. The GLOBAL switch is checked first,
   * so a platform-wide stop reports `scope: "global"` even for an agent that is
   * also individually disabled.
   */
  evaluate(agentId: string): KillSwitchDecision;
  /** True while the global (platform-wide) kill switch is active (Req 17.3). */
  isGloballyDisabled(): boolean;
  /** True while the given agent's per-agent kill switch is active (Req 17.2). */
  isAgentDisabled(agentId: string): boolean;
  /** Admin control: flip the global switch (Req 17.4). */
  setGlobal(disabled: boolean): void;
  /** Admin control: flip one agent's switch (Req 17.4). */
  setAgent(agentId: string, disabled: boolean): void;
  /** Snapshot of the currently-disabled agent ids (for tests / an admin view). */
  disabledAgents(): string[];
}

export interface KillSwitchOptions {
  /** Initial global switch state. Defaults to `false` (not disabled). */
  global?: boolean;
  /** Initial set of per-agent-disabled agent ids. Defaults to empty. */
  agents?: Iterable<string>;
}

export function createKillSwitch(opts: KillSwitchOptions = {}): KillSwitch {
  let globalDisabled = Boolean(opts.global);
  const disabled = new Set<string>();
  for (const id of opts.agents ?? []) {
    const trimmed = String(id).trim();
    if (trimmed !== "") disabled.add(trimmed);
  }

  return {
    evaluate(agentId: string): KillSwitchDecision {
      // Global first: a platform-wide stop supersedes per-agent state.
      if (globalDisabled) return { blocked: true, scope: "global" };
      if (disabled.has(agentId)) return { blocked: true, scope: "agent" };
      return { blocked: false };
    },
    isGloballyDisabled(): boolean {
      return globalDisabled;
    },
    isAgentDisabled(agentId: string): boolean {
      return disabled.has(agentId);
    },
    setGlobal(value: boolean): void {
      globalDisabled = Boolean(value);
    },
    setAgent(agentId: string, value: boolean): void {
      const id = String(agentId).trim();
      if (id === "") return;
      if (value) disabled.add(id);
      else disabled.delete(id);
    },
    disabledAgents(): string[] {
      return [...disabled];
    },
  };
}

/**
 * Truthy-env parser for boolean flags: `1`, `true`, `yes`, `on` (any case) →
 * true; everything else (including unset/empty) → false.
 */
function envFlag(raw: string | undefined): boolean {
  const v = (raw ?? "").trim().toLowerCase();
  return v === "1" || v === "true" || v === "yes" || v === "on";
}

/**
 * Build a {@link KillSwitch} seeded from the environment. The seed is only the
 * INITIAL state — an admin can still toggle either switch at runtime.
 *
 * Env (all optional):
 *   INVOKE_KILL_SWITCH_GLOBAL  — `1`/`true`/`yes`/`on` activates the global stop.
 *   INVOKE_KILL_SWITCH_AGENTS  — comma/space-separated agent ids to disable.
 */
export function killSwitchFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): KillSwitch {
  const agents = (env.INVOKE_KILL_SWITCH_AGENTS ?? "")
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s !== "");
  return createKillSwitch({
    global: envFlag(env.INVOKE_KILL_SWITCH_GLOBAL),
    agents,
  });
}
