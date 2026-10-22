"""Always-on Capability API — HTTP surface for the capability control plane.

A dedicated Python Fargate service behind the existing CloudFront/ALB, routed
at ``/control-plane/*`` (R6). It exists because deterministic inventory and
CRUD had been travelling through the Main Agent AgentCore Runtime, which meant
listing skills could cold-start Strands, a model client, OAuth, MCP sessions,
and skill packages to answer a question that needs none of them.

Design constraints this package holds:

* **No Runtime, ever.** Nothing here imports ``main``, ``strands``, a model
  factory, or an MCP client, and no failure path invokes
  ``InvokeAgentRuntime``. ``tests/test_capability_api_isolation.py`` asserts
  the import graph stays clean, because the guarantee is worth nothing if a
  convenience import quietly reintroduces it.
* **Always warm.** Desired count is at least one and the service never scales
  to zero, so no user request waits on container provisioning (R13).
* **Trusted identity only.** The Cognito access token is verified here and the
  actor/group/org context is derived from its claims; request bodies cannot
  assert who they are (R5).
"""
