# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

ARG DOCKER_CLIENT_IMAGE
ARG RUNTIME_IMAGE
FROM ${DOCKER_CLIENT_IMAGE} AS docker-client
FROM ${RUNTIME_IMAGE}

COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY --from=worker-qualification-tools --chmod=0555 /bin/ /opt/qualification/bin/
COPY --from=worker-qualification-tools /LICENSE.openshell /opt/qualification/LICENSE.openshell
COPY --from=records-qualification-tools --chmod=0555 /bin/ /opt/qualification/bin/
COPY --from=records-qualification-tools /LICENSE.minio /LICENSE.mc /opt/qualification/
COPY deploy/tests/worker_network_*.py deploy/tests/worker_hostile_probe.py deploy/tests/worker_native_fixture.py deploy/tests/worker_telemetry.py deploy/tests/test_worker_native_integration.py /opt/qualification/tests/
COPY deploy/tests/test_worker_external_integration.py /opt/qualification/tests/
COPY deploy/tests/external_service_fixture.py /opt/qualification/deploy/tests/
COPY deploy/tests/test_worker_model_integration.py /opt/qualification/tests/
COPY deploy/worker_model_profile.py /opt/qualification/worker_model_profile.py
COPY deploy/tests/checkpoint_records_integration.py deploy/tests/checkpoint_transport_integration.py /opt/qualification/deploy/tests/
COPY --from=records-tls-fixture /tls_fixture.py /opt/qualification/packages/audit/tests/tls_fixture.py
COPY images/worker/candidate/gateway.json /opt/qualification/gateway.json

USER heartwood
ENTRYPOINT ["/opt/heartwood/.venv/bin/python", "-I", "-B"]
