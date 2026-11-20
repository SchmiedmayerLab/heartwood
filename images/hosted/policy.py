# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Record the effective sandbox policy digest of the session's workers in the manifest.

The OpenShell gateway admits a worker with the digest of its effective policy, which
covers the endpoints of the providers the worker attaches, stamped with the identity of
each provider object. That identity is particular to the session's OpenShell gateway,
so the digest is learned there: the init container creates a sandbox with the worker
image, the manifest's policy and the model route's provider, reads the digest of its
admission, removes the sandbox and writes the digest into the manifest's [worker] table.

Usage: policy.py DEPLOYMENT_DIRECTORY NATIVE_CONFIG_HOME
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import tomllib
from pathlib import Path

import openshell
from google.protobuf.json_format import ParseDict
from openshell._proto import openshell_pb2, sandbox_pb2

_DIGEST = re.compile(r'^sandbox_policy_sha256 = "[0-9a-f]{64}"$', re.MULTILINE)


def main() -> None:
    """Learn the digest and write it into the manifest."""
    deployment, native = Path(sys.argv[1]), Path(sys.argv[2])
    manifest = deployment / "deployment.toml"
    text = manifest.read_text(encoding="utf-8")
    worker = tomllib.loads(text)["worker"]
    gateway = native / "openshell" / "gateways" / worker["gateway"]
    endpoint = json.loads((gateway / "metadata.json").read_text(encoding="utf-8"))
    client = openshell.SandboxClient(
        endpoint["gateway_endpoint"].removeprefix("https://").rstrip("/"),
        tls=openshell.TlsConfig(
            ca_path=gateway / "mtls" / "ca.crt",
            cert_path=gateway / "mtls" / "tls.crt",
            key_path=gateway / "mtls" / "tls.key",
        ),
        timeout=30,
    )
    policy = ParseDict(
        json.loads((deployment / Path(worker["policy_path"]).name).read_text(encoding="utf-8")),
        sandbox_pb2.SandboxPolicy(),
    )
    workspace = worker["workspace"]
    # The gateway refuses a name that a deletion found absent, so each probe has its own.
    name = f"policy-probe-{secrets.token_hex(6)}"
    labels = {"heartwood.project": "policy-probe", "heartwood.attempt": name}
    created = client.create(
        workspace=workspace,
        name=name,
        labels=labels,
        spec=openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(
                image=f"{worker['image_repository']}@{worker['image_digest']}",
                resources={"limits": {"cpu": worker["cpu"], "memory": worker["memory"]}},
                labels={**labels, "heartwood.role": "worker"},
            ),
            policy=policy,
            providers=[worker["provider"]],
            environment={
                "SESSION_API_KEY": secrets.token_urlsafe(32),
                "HEARTWOOD_SKILL_PLATFORM": "generic",
            },
            command=[
                "/opt/heartwood/.venv/bin/python",
                "-I",
                "-B",
                "-u",
                "/opt/heartwood/images/worker/launch.py",
            ],
        ),
    )
    try:
        ready = client.wait_ready(name, workspace=workspace, timeout_seconds=240)
        admission = ready.status.configuration_admission
        if (
            ready.id != created.id
            or admission is None
            or admission.state != openshell_pb2.CONFIGURATION_ADMISSION_STATE_ACCEPTED
            or re.fullmatch(r"[0-9a-f]{64}", admission.policy_hash) is None
        ):
            raise SystemExit("the session's OpenShell gateway did not admit the worker policy")
        digest = admission.policy_hash
    finally:
        client.delete(name, workspace=workspace, allow_missing=True, timeout_seconds=90)
        client.wait_deleted(
            name, workspace=workspace, expected_sandbox_id=created.id, timeout_seconds=90
        )
    updated, count = _DIGEST.subn(f'sandbox_policy_sha256 = "{digest}"', text)
    if count != 1:
        raise SystemExit("the manifest names no sandbox policy digest")
    manifest.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
