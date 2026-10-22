# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — first public release

The first public release of Media Agent Harness: an operating layer for media agents on Amazon Bedrock
AgentCore and Strands Agents, deployed with the Unified Media Agent Toolkit in one `cdk deploy`.

### Capabilities
- **A visual media workspace.** Chat beside a Renderer with timelines (shot, speaker and caption lanes),
  image galleries and document previews. Frame-accurate ranges, markers and crops go from the timeline into
  the conversation; clips and renders come back next to their source.
- **An orchestrator with sub-agents.** The main agent answers directly or dispatches task, planning, design
  and render sub-agents, each on its own AgentCore runtime. Multi-step requests become a DAG plan that fans
  out, waits on long-running jobs and hands results forward, so the work outlives the chat turn.
- **Media workflows as tools.** The Unified Media Agent Toolkit's micro-workflows (shot segmentation,
  diarization, transcoding, face recognition and more) sit behind an AgentCore Gateway; a job registry tracks
  them and their results stream back to the chat when they finish.
- **Media by reference.** Agents pass storage locations and stable links, never bytes or expiring URLs.
  Every output becomes an asset of its project, with lineage back to its inputs.
- **Projects.** Folders, bookmarks (saved moments), an import inbox, and search across video, audio, images
  and documents with citations that open at the cited moment or page.
- **Governed capabilities.** Skills, tool collections, hooks and custom agents are versioned assets with a
  draft → publish lifecycle, discoverable through the AgentCore Agent Registry. Built-in skills ship with the
  harness.
- **Custom agents for any OIDC application.** Publish an agent and call it from your own application with your
  own identity provider; external callers upload media and download results within their own scope.
- **A code sandbox** (AgentCore Code Interpreter) for analysis, documents and media edits, with an LGPL
  ffmpeg built in your account from pinned, checksummed sources.
- **One-command deploy.** A single `cdk deploy` (see the README's Quick Start) deploys the toolkit and the
  harness; the container images and the web app build in AWS CodeBuild, so no local Docker is needed.

### Security
- Session ownership is enforced wherever a request names a session: chat turns, the render endpoint, the
  render store and uploads. A session-less upload or render is tagged with its owner, and only that user can
  read it back.
- Media is private: the artifacts CDN refuses every path, and the authenticated proxy serves media through
  short-lived presigned URLs after an ownership or project-membership check. A key nothing attributes to the
  caller is served only to members of a project that registered it. Agent-written documents open in a
  sandboxed viewer with a single-document view ticket.
- The code sandbox runs with per-project, per-session scoped S3 credentials and fails closed when they cannot
  be installed.
- The UI load balancer accepts only CloudFront (origin-facing prefix list plus a secret origin header).
  Clients cannot send the proxy-reserved request fields the runtimes trust.
- Cognito self sign-up is off by default.
- Dependencies are on fixed releases: `bedrock-agentcore` 1.18.1 (CVE-2026-12530, CVE-2026-16796),
  Starlette 1.x, PyJWT 2.15, cryptography 50, pypdf 6.21 and Pillow 13 in the ingestion functions,
  `aws-cdk-lib` 2.273, and a web app and proxy with no known npm advisories.

### Known limitations
- CloudFront reaches the UI load balancer over HTTP inside AWS; see [SECURITY.md](SECURITY.md#known-limitations).
  A CloudFront VPC origin is the planned hardening.
- `mcp` stays on 2.1.x until Strands Agents allows 2.2 (the open advisory concerns MCP *servers*; the harness
  uses the client only), and the agent runtimes' Pillow stays on 12.x until Strands Agents Tools allows 13.
- One install per `projectName` per account and Region; deploy only to the toolkit's published Regions.
- This is sample code, not a production-ready service. It creates billable resources, and some (media buckets,
  image repositories, the user pool) are retained on teardown on purpose.

## Before 1.0.0

### Added
- Whole-skill loading: `skills_manager` now inlines a skill's text `references/*.md` into its
  instructions under a byte budget (`SKILL_BUNDLE_REFERENCE_MAX_BYTES`, default 24KB), so
  references reach detached `plan_and_execute` (DAG) nodes; over-budget skills keep pure
  progressive disclosure. S3-authored skills now download the whole package, not just `SKILL.md`.
- AgentCore **Cedar** policy engine on the shared Platform gateway (IaC, `ENFORCE`): scoped
  `permit` for the read-only AWS-doc tools, default-deny otherwise.
- Guard tests pinning the MCP 2.x SDK's automatic `2026-07-28` protocol negotiation.

### Changed
- Model tool caching migrated from the deprecated `cache_tools` flag to
  `CacheConfig(tools_ttl=…)` (behavior-preserving; removes the strands ≥1.55 DeprecationWarning).
- Content-moderation skill: the rating enum is self-contained in `sg5` with a deterministic code
  guard; the report is always emitted as a **PDF**; `sg5` outputs the report + PDF CloudFront URLs
  so `get_plan_results` surfaces them as downloads (no post-completion fan-out).

### Fixed
- Render image duplicates (two distinct causes): client-preview entries lingering beside the
  server-composed gallery (`mergeServerRenders` now dedups by content), and `preview`/`thumbnail`
  URLs being harvested as separate gallery tiles (`extract_urls` now excludes them).
- Platform-gateway federation: the task-agent runtime was missing the platform-gateway env, so
  shared AWS-doc tools didn't load there; fixed and verified live.
- Delegated (async-plan) gateway auth: `bind_delegation_grant_and_connect` kept a live
  user-token connection even after that token expired, so detached plan nodes presented a stale
  JWT and the AgentCore gateway rejected them with `403 insufficient_scope` (upstream of the
  ownership interceptor, never reaching the target). The pinned user token now carries expiry
  awareness; when provably expired it is cleared and the connection rebinds on the machine
  token + delegation grant. This was the intermittent user-MCP (fal) `insufficient_scope` seen on
  long/detached reels — it was never a fal or per-model problem.
- Cross-session render bleed: the end-of-turn render harvester rendered any media URL in a tool
  result, so a continuation session that loaded prior reusable work auto-opened a tab for a
  *different* session's asset. The auto-harvester now drops candidates under
  `artifacts/<uuid>/` for a uuid ≠ the current session (allows the shared `external/` namespace,
  the current session's prefix, and non-artifact URLs; fails open when the session is unknown).
  Explicit displays are unaffected.
- UI localStorage quota (`Failed to execute 'setItem' … exceeded the quota` on
  `lab-orchestrator-model-id`): the culprit was a **write-only** `lab-sessions` blob (never read
  back — the list reloads from DynamoDB) that carried every session's `renderHistory` and grew
  until the store filled. Removed that write and purge the stale key; model-id writes now go
  through `safeSetItem` (prune-and-retry); the per-session `lab-render-<id>` cache is bounded by
  an LRU and reclaimed by the pruner.
- **agent-olive skill (Phase 7 stitch):** the skill now tells the stitch dispatch to reuse the
  exact segment URLs approved at the Phase 6 gate (verbatim, in script order) and forbids
  re-discovering inputs (no S3 listing, no searching for "the reel", never a clip from another
  session). Removes the input re-hunt that inflated a one-shot concat into ~70 steps and could
  splice in a prior session's reel.
