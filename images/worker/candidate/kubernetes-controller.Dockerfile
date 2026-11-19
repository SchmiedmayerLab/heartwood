# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# The controller of the Kubernetes qualification: the runtime image with the built
# OpenShell command-line tool, kubectl, and the qualification cases. It runs as a pod
# that stands in for the Heartwood gateway pod of one session.
ARG KUBECTL_IMAGE=registry.k8s.io/kubectl:v1.37.0@sha256:5ed410ebac5dc976cc717098994dcdb29bbbd38f6bd65f582311f5be4ba719cf
ARG RUNTIME_IMAGE
FROM ${KUBECTL_IMAGE} AS kubectl
FROM ${RUNTIME_IMAGE}

COPY --from=kubectl /bin/kubectl /opt/qualification/bin/kubectl
COPY --from=worker-qualification-tools --chmod=0555 /bin/ /opt/qualification/bin/
COPY --from=worker-qualification-tools /LICENSE.openshell /opt/qualification/LICENSE.openshell
# The cases shared with the Docker qualification, and what their modules import.
COPY deploy/tests/worker_network_*.py deploy/tests/worker_hostile_probe.py deploy/tests/worker_native_fixture.py deploy/tests/worker_telemetry.py deploy/tests/test_worker_native_integration.py deploy/tests/test_worker_lifecycle_integration.py /opt/qualification/tests/
COPY deploy/worker_model_profile.py /opt/qualification/worker_model_profile.py
COPY deploy/tests/checkpoint_records_integration.py deploy/tests/checkpoint_transport_integration.py /opt/qualification/deploy/tests/
COPY --from=records-tls-fixture /tls_fixture.py /opt/qualification/packages/audit/tests/tls_fixture.py
COPY deploy/tests/worker_kubernetes_fixture.py deploy/tests/test_worker_kubernetes_integration.py /opt/qualification/tests/
COPY images/worker/candidate/gateway.json /opt/qualification/gateway.json

USER heartwood
ENTRYPOINT ["/opt/heartwood/.venv/bin/python", "-I", "-B"]
