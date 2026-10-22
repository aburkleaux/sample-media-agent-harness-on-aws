# Knowledge-graph Lambda image (writer + reader; infra/lib/graph/graph-stack.ts picks the handler via CMD).
#
# NOT an AWS Lambda base image: lance-graph 0.5.4 ships only a manylinux_2_39_x86_64 wheel, and the
# public.ecr.aws/lambda/python images are Amazon Linux 2023 (glibc 2.34). Debian trixie has glibc 2.41.
# The Lambda Runtime Interface Client (awslambdaric) turns a plain Python image into a Lambda runtime.
#
# Build context = agent/   (CI: kaniko --context agent --dockerfile agent/graph.Dockerfile --target runtime)
# Must be linux/amd64 (kaniko on the amd64 runners, or `docker build --platform linux/amd64`).
# BASE_IMAGE: the in-stack CodeBuild build (infra/lib/distribution/harness-assets.ts) passes the
# ECR Public mirror of this same image to avoid Docker Hub's anonymous pull limits.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-trixie
FROM ${BASE_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /var/task
COPY graph-requirements.txt /tmp/graph-requirements.txt
RUN pip install --only-binary=:all: -r /tmp/graph-requirements.txt

# Optional: `docker build --target test` runs the graph tests (incl. the lance-graph compat/parity ones)
# against the exact wheels the Lambda ships.
FROM base AS test
RUN pip install "pytest>=7" "moto[s3,dynamodb]>=5"
COPY src/project_graph /var/task/src/project_graph
COPY src/projects /var/task/src/projects
COPY src/ingestion /var/task/src/ingestion
COPY tests/test_project_graph_*.py /var/task/tests/
RUN cd /var/task && RUST_LOG=error python -m pytest -q -p no:cacheprovider tests/

FROM base AS runtime
# Only what the handlers import: project_graph, plus projects (keys/ids) and ingestion (store helpers).
COPY src/project_graph /var/task/project_graph
COPY src/projects /var/task/projects
COPY src/ingestion /var/task/ingestion
ENV PYTHONPATH=/var/task RUST_LOG=error
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["project_graph.handler.reader_handler"]
