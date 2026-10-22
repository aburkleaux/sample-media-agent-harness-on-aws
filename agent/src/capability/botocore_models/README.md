# Vendored botocore service models — `agent-registry`

The AWS Agent Registry namespace shipped 2026-08-06. `agent-registry` and
`agent-registry-control` exist in `boto3 >= 1.43.64` (the floor in
`agent/pyproject.toml`), but **not** in the boto3/botocore bundled with the
Lambda `python3.13` managed runtime, and not in the `aws` CLI v2 currently
installed on developer machines (`aws agent-registry-control help` → usage
error). The projection Lambda is a plain `Code.fromAsset` zip — the GitLab
deploy job runs on `node:20` with no Docker, so CDK asset bundling and
container-image functions are both unavailable (see `.gitlab-ci.yml`, the
comment above `build:proxy-image`).

Rather than vendor all of boto3 (~50 MB) to get 15 KB of JSON, these model
files are copied verbatim from the pinned botocore and loaded through
botocore's own `AWS_DATA_PATH` search path. `capability/registry.py`
(`_ensure_model_path`) prepends this directory to `AWS_DATA_PATH` before the
first client is constructed, so `boto3.client("agent-registry-control")`
resolves the operations and the endpoint ruleset from here when the ambient
botocore does not know the service. Where the ambient botocore *does* know it
(local dev, the Capability API container), botocore's own copy is found first
and these files are inert.

Refresh procedure — run from `agent/` after bumping the boto3 floor:

```bash
uv run python -c "
import botocore, os, shutil
base = os.path.join(os.path.dirname(botocore.__file__), 'data')
for svc in ('agent-registry', 'agent-registry-control'):
    dst = os.path.join('src/capability/botocore_models', svc)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(os.path.join(base, svc), dst)
"
uv run --extra dev pytest tests/test_capability_registry.py -q
```

Do not hand-edit these files. If an API shape changes, bump boto3 and re-copy;
`tests/test_capability_registry.py` asserts the operations the projector and
Capability API actually call are still present, so a stale copy fails there
rather than at deploy time.
