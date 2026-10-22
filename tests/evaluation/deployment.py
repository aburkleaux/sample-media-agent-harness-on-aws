"""Deployment coordinates for the evaluation tooling, read from the CDK config.

Why this exists
---------------
Region was hardcoded or defaulted to `us-east-1` across this package
(`optimize_run.py`, `online_eval_setup.py`, and the `--region` defaults in
`harvester.py` / `runner.py`) while every deployed environment is us-west-2. That
fails silently rather than loudly: a CloudWatch log group simply does not exist in
the wrong region, so span collection returns nothing and the run reports "no
low-scoring sessions" instead of an error.

Replacing one literal with another would leave the same class of bug, so this reads
`infra/lib/env-config.ts` — the definition the CDK itself deploys from. If a
deployment moves region, this follows automatically and there is no second copy to
update.

Precedence, most specific first:
  1. an explicit argument (a `--region` flag)
  2. EVAL_REGION — deliberate override for one-off targets
  3. the region declared for the selected env in the CDK config
  4. nothing: raise, rather than guess a region and silently find no data

A generic AWS_REGION is NOT consulted. It commonly points somewhere unrelated to the
deployment (this workstation exports us-east-1), which is exactly how the original
defect went unnoticed.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

__all__ = ["deployment_config", "deployment_region", "deployment_account",
           "available_envs", "ENV_CONFIG_PATH"]

ENV_CONFIG_PATH = Path(__file__).resolve().parents[2] / "infra" / "lib" / "env-config.ts"

_DEFAULT_ENV = "staging"


@lru_cache(maxsize=1)
def _parse_env_config() -> dict[str, dict[str, str]]:
    """Extract `{env: {account, region}}` from the CDK env config."""
    if not ENV_CONFIG_PATH.exists():
        raise FileNotFoundError(f"CDK env config not found at {ENV_CONFIG_PATH}")
    text = ENV_CONFIG_PATH.read_text()
    out: dict[str, dict[str, str]] = {}
    # Each environment is declared as `  <name>: {` with `env:`/`account:`/`region:`
    # among its first fields.
    for m in re.finditer(r"^\s{2}([A-Za-z][A-Za-z0-9_]*):\s*\{", text, re.M):
        name = m.group(1)
        block = text[m.end():m.end() + 2000]
        env_name = re.search(r'env:\s*"([^"]+)"', block)
        region = re.search(r'region:\s*"([^"]+)"', block)
        account = re.search(r'account:\s*"([^"]+)"', block)
        if env_name and region and env_name.group(1) == name:
            out[name] = {
                "env": name,
                "region": region.group(1),
                "account": account.group(1) if account else "",
            }
    if not out:
        raise ValueError(f"no environments parsed from {ENV_CONFIG_PATH}")
    return out


def available_envs() -> list[str]:
    return sorted(_parse_env_config())


def deployment_config(env: str | None = None) -> dict[str, str]:
    """Coordinates for one deployment environment."""
    env = env or os.environ.get("DEPLOY_ENV") or _DEFAULT_ENV
    cfg = _parse_env_config()
    if env not in cfg:
        raise KeyError(f"unknown env {env!r}; known: {', '.join(sorted(cfg))}")
    return dict(cfg[env])


def deployment_region(region: str | None = None, env: str | None = None) -> str:
    """Region to talk to, without ever guessing one."""
    if region:
        return region
    override = os.environ.get("EVAL_REGION")
    if override:
        return override
    return deployment_config(env)["region"]


def deployment_account(env: str | None = None) -> str:
    return deployment_config(env)["account"]
