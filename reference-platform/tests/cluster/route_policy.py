# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Learn the effective sandbox policy digest of hosted workers on an installed chart.

python3 route_policy.py --cluster-context CONTEXT

The OpenShell gateway admits a worker only with the digest of its effective policy,
which covers the endpoint of the model route's provider. The script launches a session
as the spawner would, runs a pod with the session's gateway Secret and the hosted gateway
image, creates a sandbox with the worker image, the policy and the provider, prints the
digest the gateway admitted it with, and removes the sandbox, the pod and the session.
Set hub.worker.sandboxPolicySha256 to the printed digest.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from identity import (
    FIRST,
    PLATFORM,
    SESSIONS,
    launch_url,
    probe,
    request,
    spawner_token,
)
from kube import PROBE_CA, Cluster

_POD = "hw-route-policy"
_PROBE_IMAGE = (
    "registry.k8s.io/e2e-test-images/agnhost:2.53"
    "@sha256:99c6b4bb4a1e1df3f0b3752168c89358794d02258ebebc26bf21c29399011a85"
)
# Runs in the pod: it creates a disposable sandbox as the hosted gateway would and
# prints the policy digest of its admission.
_PROGRAM = """import json,secrets,sys,time,tomllib
from pathlib import Path
import openshell
from google.protobuf.json_format import ParseDict
from openshell._proto import openshell_pb2 as pb, sandbox_pb2 as policy_pb
source=Path('/source')
manifest=tomllib.loads((source/'deployment.toml').read_text())
worker=manifest['worker']
launch=json.loads((source/'launch.json').read_text())
client=openshell.SandboxClient(launch['openshell_endpoint'].removeprefix('https://'),
 tls=openshell.TlsConfig(ca_path=source/'openshell-ca.crt',cert_path=source/'openshell-tls.crt',
  key_path=source/'openshell-tls.key'),timeout=30)
policy=ParseDict(json.loads((source/'worker-policy.json').read_text()),policy_pb.SandboxPolicy())
name='policy-probe'
labels={'heartwood.project':'policy-probe','heartwood.attempt':name}
created=client.create(workspace=worker['workspace'],name=name,labels=labels,spec=pb.SandboxSpec(
 template=pb.SandboxTemplate(image=worker['image_repository']+'@'+worker['image_digest'],
  resources={'limits':{'cpu':worker['cpu'],'memory':worker['memory']}},
  labels={**labels,'heartwood.role':'worker'}),
 policy=policy,providers=[worker['provider']],
 environment={'SESSION_API_KEY':secrets.token_urlsafe(32),'HEARTWOOD_SKILL_PLATFORM':'generic'},
 command=['/opt/heartwood/.venv/bin/python','-I','-B','-u','/opt/heartwood/images/worker/launch.py']))
try:
 ready=client.wait_ready(name,workspace=worker['workspace'],timeout_seconds=240)
 admission=ready.status.configuration_admission
 assert ready.id==created.id and admission.state==pb.CONFIGURATION_ADMISSION_STATE_ACCEPTED
 print('policy-hash',admission.policy_hash,flush=True)
finally:
 client.delete(name,workspace=worker['workspace'],allow_missing=True,timeout_seconds=90)
 client.wait_deleted(name,workspace=worker['workspace'],expected_sandbox_id=created.id,
  timeout_seconds=90)
"""


def _settings(cluster: Cluster) -> dict[str, object]:
    result = cluster.kubectl(
        "get", f"--namespace={PLATFORM}", "configmap", "heartwood-hub", "--output=json"
    )
    assert result.returncode == 0, result.stderr
    settings = json.loads(json.loads(result.stdout)["data"]["settings.json"])
    assert isinstance(settings, dict)
    return settings


def policy_digest(cluster: Cluster) -> str:
    """Return the effective policy digest of hosted workers on the cluster."""
    image = _settings(cluster)["gateway_image"]
    # The launcher pod trusts the platform CA through the tests' ConfigMap.
    result = cluster.kubectl(
        "get", f"--namespace={PLATFORM}", "configmap", "platform-ca", "--output=json"
    )
    assert result.returncode == 0, result.stderr
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": PROBE_CA, "namespace": SESSIONS},
            "data": {"ca.crt": json.loads(result.stdout)["data"]["ca.crt"]},
        }
    )
    with probe(cluster, "hw-route-policy-launcher", _PROBE_IMAGE) as launcher:
        status, launch = request(
            cluster, launcher, "PUT", launch_url(FIRST, "demo"), spawner_token(cluster)
        )
        assert status == 200, launch
        try:
            pod = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": _POD,
                    "namespace": SESSIONS,
                    "labels": {"heartwood.platform/session": launch["session"]},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "command": ["/opt/heartwood/.venv/bin/python", "-I", "-c", _PROGRAM],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "source", "mountPath": "/source", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "source",
                            "secret": {
                                "secretName": launch["gateway_service_account"],
                                "defaultMode": 0o440,
                            },
                        }
                    ],
                },
            }
            cluster.apply(pod)
            phase = ""
            deadline = time.monotonic() + 300
            while phase not in {"Succeeded", "Failed"} and time.monotonic() < deadline:
                time.sleep(3)
                phase = cluster.kubectl(
                    "get",
                    f"--namespace={SESSIONS}",
                    f"pod/{_POD}",
                    "--output=jsonpath={.status.phase}",
                ).stdout
            logs = cluster.kubectl("logs", f"--namespace={SESSIONS}", f"pod/{_POD}").stdout
            if phase != "Succeeded":
                described = cluster.kubectl("describe", f"--namespace={SESSIONS}", f"pod/{_POD}")
                logs += described.stdout[-3000:]
            assert phase == "Succeeded", logs[-4000:]
            found = re.search(r"^policy-hash ([0-9a-f]{64})$", logs, re.MULTILINE)
            assert found is not None, logs[-2000:]
            return found.group(1)
        finally:
            cluster.kubectl("delete", f"--namespace={SESSIONS}", f"pod/{_POD}", "--wait")
            request(cluster, launcher, "DELETE", launch_url(FIRST, "demo"), spawner_token(cluster))


def main() -> None:
    """Print the effective policy digest of hosted workers on the named cluster."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-context", required=True)
    print(policy_digest(Cluster(parser.parse_args().cluster_context)))


if __name__ == "__main__":
    main()
