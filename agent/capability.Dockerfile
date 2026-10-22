# Container image for the always-on Capability API (ECS Fargate).
#
# Build context is the `agent/` directory. Like the UI proxy image, this is
# built and pushed OUT OF BAND (kaniko in CI, `docker build` by hand locally)
# and referenced by tag — the GitLab deploy job runs on `node:20` with no
# Docker daemon, so a CDK `fromAsset`/`DockerImageFunction` build would fail
# there (see the comment above `build:proxy-image` in `.gitlab-ci.yml`).
#
# Build + push (from the repo root):
#
#   docker build --platform linux/amd64 --provenance=false --sbom=false \
#     -f agent/capability.Dockerfile \
#     -t <acct>.dkr.ecr.us-west-2.amazonaws.com/mediaworks-harness-capability-api:<tag> \
#     agent
#   docker push <acct>.dkr.ecr.us-west-2.amazonaws.com/mediaworks-harness-capability-api:<tag>
#
# `--platform linux/amd64` because the task definition pins X86_64 (an arm64
# image would fail as an opaque "exec format error" crash loop), and
# `--provenance=false --sbom=false` because Fargate rejects the multi-arch
# manifest list buildx otherwise emits.
#
# What is deliberately NOT installed: strands-agents, bedrock-agentcore, the
# model factory, MCP clients, skill packages. The Capability API must be
# runnable without any of it (R1); `tests/test_capability_api_isolation.py`
# asserts the import graph agrees.

ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.13-slim
FROM ${BASE_IMAGE}

WORKDIR /app

# fastapi/uvicorn/pyjwt mirror the `capability-api` extra in pyproject.toml;
# boto3's floor matches the project's (>= 1.43.64) because the `agent-registry`
# namespace does not exist below it. Pinned to majors so a rebuild of an old
# tag stays reproducible enough to debug. This service is internet-facing, so
# starlette / PyJWT / cryptography carry the security floors
# from pip-audit (2026-10) explicitly instead of trusting whatever fastapi pulls.
RUN pip install --no-cache-dir \
      "fastapi>=0.141,<1" \
      "starlette>=1.3.1,<2" \
      "uvicorn[standard]>=0.30,<1" \
      "boto3>=1.43.64,<2" \
      "PyJWT[crypto]>=2.15,<3" \
      "cryptography>=50.0.0"

# Only the control-plane half of the agent source. `lab/**` is the canonical
# domain layer the API reuses (R6); `capability/**` and `capability_api/**` are
# the new control plane.
#
# THE COPY LIST MUST COVER WHAT `lab/**` IMPORTS LAZILY, NOT JUST WHAT IT
# IMPORTS AT THE TOP. That is how the Hooks tab broke: flipping `hook` to
# `canonical` routed ListHooks here, `lab/service.py` does
# `from hooks.defaults import default_hook_specs` *inside* the handler, and the
# module was absent — so the tab rendered the raw
# `No module named 'hooks'` (a 200 carrying an error, per the runtime-parity
# envelope, so not even a 500 to notice). `model_catalog` was the same bug one
# step from surfacing: `specialty_crud._validate_model` returns early when the
# model is None, so only saving an agent with an EXPLICIT model hit it.
# `tests/test_capability_api_image_imports.py` now fails if this list falls
# behind the import graph again.
COPY src/lab ./src/lab
COPY src/capability ./src/capability
COPY src/capability_api ./src/capability_api
# Hook conditions and built-in hook specs. Safe despite the runtime-stack rule:
# `hooks/__init__.py` is empty, `hooks/expr.py` is stdlib-only, and the `strands`
# / `mcp_manager` imports in `hooks/provider.py` are function-local, so nothing
# heavy loads at import time — and no control-plane path imports `provider` or
# `loader` at all.
COPY src/hooks ./src/hooks
# Model validation for specialty-agent drafts (`discover_models`). boto3 only.
COPY src/model ./src/model
COPY src/prompts ./src/prompts
# Sub-agent runtime ARN resolution (`capability.sources._builtin_runtime_state`). stdlib + boto3
# only. Env (this task def carries the four ARNs) wins, and the task has no RUNTIME_ARN_PARAM_PATH,
# so this service never reads SSM.
COPY src/runtime_arns.py ./src/runtime_arns.py
COPY src/app_version.py ./src/app_version.py

# Still deliberately absent — and each is reachable ONLY from an action in
# `RUNTIME_ONLY_ACTIONS`, which this service refuses before dispatch, so their
# absence is a second line of defence rather than a gap:
#   * `skills_manager`, `dag_schema`  → RunHarness (`lab/test_harness.py`)
#   * `custom_agent_resolver`         → ResolveAgentConfig (`lab/service.py`)
# main.py, task_agent.py, mcp_manager and tools stay out for the original
# reason: importing them pulls the Runtime stack back in.

# Deliberately NOT copied: `agent/skills/`. The built-in skill catalog is a DynamoDB row
# written by `cdk deploy` (`infra/lib/builtin-skills.ts` + the `BuiltinSkillCatalog`
# custom resource) and read by `capability.sources.read_builtins("skill")`. Baking the
# packages in here would make this image a deploy input for *data*, so editing a
# `SKILL.md` and deploying would leave the UI showing the old text until someone
# remembered to rebuild.

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Matches the ALB target group's health check path and the UI proxy's port, so
# the two services are interchangeable from the load balancer's perspective.
EXPOSE 8080

# `--factory` so configuration is validated at startup and a cross-environment
# registry id exits the container instead of serving 500s. Single worker: the
# service scales by task count (desiredCount >= 1, never to zero), and the read
# path is I/O-bound fan-out that asyncio already overlaps.
CMD ["uvicorn", "capability_api.app:build_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
