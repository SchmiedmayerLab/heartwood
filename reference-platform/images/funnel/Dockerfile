# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Image of the Funnel server and worker, built from the pinned source with the recorded
# patches. Prepare the build context and build from the repository root:
#   python3 reference-platform/tools/prepare_funnel.py CONTEXT
#   docker build --file reference-platform/images/funnel/Dockerfile \
#     --build-arg FUNNEL_VERSION=... --build-arg FUNNEL_REVISION=... CONTEXT
# The target test runs the tests of the packages that the patches change.
# The base images are those of funnel.json.

FROM golang:1.27.3-alpine@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS build
ENV CGO_ENABLED=0 GOFLAGS=-buildvcs=false
WORKDIR /src
COPY funnel/go.mod funnel/go.sum ./
# Modules are verified against go.sum.
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY funnel/ ./
ARG FUNNEL_VERSION
ARG FUNNEL_REVISION
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    test -n "${FUNNEL_VERSION}" && test -n "${FUNNEL_REVISION}" && \
    go build -trimpath -o /out/funnel -ldflags "-s -w \
      -X github.com/calypr/funnel/version.Version=${FUNNEL_VERSION} \
      -X github.com/calypr/funnel/version.GitCommit=${FUNNEL_REVISION}" .

FROM build AS test
ARG FUNNEL_TESTS
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    test -n "${FUNNEL_TESTS}" && go test -count=1 ${FUNNEL_TESTS}

# The server runs as a non-root user and the worker as root in its own user namespace;
# both are set by the pod templates.
FROM gcr.io/distroless/static-debian13@sha256:1a64f46364620465ef9e9a8f5413757dfdf22695e050bbfd7303eeaf17a59383
COPY --from=build /out/funnel /app/funnel
COPY funnel/License.txt /usr/share/doc/funnel/License.txt
ENTRYPOINT ["/app/funnel"]
