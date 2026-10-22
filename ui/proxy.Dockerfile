# Container image for the agentcore-proxy backend (ECS Fargate, staging/prod).
#
# Bundles `ui/server/index.ts` (the standalone Express entry point — see
# that file and `ui/server/app.ts`) into a single CommonJS file with esbuild,
# so the runtime image only needs a Node.js base — no TypeScript/Vite/React
# build tooling, no `node_modules` tree to copy.
#
# Build context is the `ui/` directory (see the CDK `ecs.ContainerImage.
# fromAsset` call in `infra/lib/agent-infra-stack.ts`, which passes
# `directory: path.join(__dirname, "../../ui")`).

ARG BASE_IMAGE=public.ecr.aws/docker/library/node:22-slim
FROM ${BASE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY server ./server
COPY src/types ./src/types
COPY src/shared ./src/shared
# `server/projects/registry.ts` imports `src/projects/orderProjects` (the one ordering the SPA shares).
COPY src/projects ./src/projects
COPY auth ./auth
RUN npm run build:server

FROM ${BASE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/server-dist/index.cjs ./index.cjs
EXPOSE 8080
CMD ["node", "index.cjs"]
