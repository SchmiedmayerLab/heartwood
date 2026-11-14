# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Run installed native-worker tests using verified candidate build artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str]) -> None:
    """Require successful execution of an outside build operation."""
    subprocess.run(arguments, check=True)


def digest(path: Path) -> str:
    """Identify exact artifact bytes before image assembly."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualify(worker: str, runtime: str, artifacts: Path, output: Path) -> None:
    """Assemble the trusted controller and retain the installed test outcome."""
    artifacts, output = artifacts.resolve(), output.resolve()
    if "," in str(artifacts) or "," in str(output):
        raise ValueError("Docker paths must not contain commas")
    manifest_path = ROOT / "images/worker/candidate/gateway.json"
    manifest = json.loads(manifest_path.read_text())
    record = json.loads((artifacts / "gateway-build-record.json").read_text())
    if (
        record["manifest_sha256"] != digest(manifest_path)
        or record["builder_sha256"] != digest(ROOT / "deploy/build-worker-candidate.py")
        or record["source_revision"] != manifest["source"]["revision"]
        or record["version"] != manifest["version"]
        or record["architecture"] not in {"amd64", "arm64"}
        or set(record["binaries"]) != {"openshell", "openshell-gateway"}
    ):
        raise ValueError("Gateway artifacts do not match the current reviewed manifest")
    for name, expected in record["binaries"].items():
        if Path(name).name != name or digest(artifacts / "bin" / name) != expected:
            raise ValueError("Gateway binary digest mismatch")
    dockerfile = artifacts / "deploy/docker/Dockerfile.gateway"
    prebuilt = (
        artifacts
        / "deploy/docker/.build/prebuilt-binaries"
        / record["architecture"]
        / "openshell-gateway"
    )
    if (
        digest(dockerfile) != record["dockerfile_sha256"]
        or digest(prebuilt) != record["binaries"]["openshell-gateway"]
    ):
        raise ValueError("Gateway packaging inputs differ from their build record")
    image_ids = {}
    for name, image in (("worker", worker), ("runtime", runtime)):
        image_ids[name] = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True
        ).strip()
    host = json.loads(
        subprocess.check_output(
            [
                "docker",
                "info",
                "--format",
                '{"kernel":{{json .KernelVersion}},"os":{{json .OSType}},'
                '"architecture":{{json .Architecture}},"docker_version":{{json .ServerVersion}}}',
            ],
            text=True,
        )
    )
    if set(host) != {"kernel", "os", "architecture", "docker_version"} or not all(
        isinstance(value, str) and value for value in host.values()
    ):
        raise ValueError("Docker host identity is incomplete")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Artifact services discard execute bits. Normalize a private packaging copy,
    # preserving both the supplied artifacts and the upstream Dockerfile bytes.
    packaging = output / "gateway"
    packaged_binary = packaging / prebuilt.relative_to(artifacts)
    packaged_binary.parent.mkdir(parents=True)
    shutil.copyfile(prebuilt, packaged_binary)
    packaged_binary.chmod(0o555)
    packaged_dockerfile = packaging / dockerfile.relative_to(artifacts)
    shutil.copyfile(dockerfile, packaged_dockerfile)
    if (
        digest(packaged_binary) != record["binaries"]["openshell-gateway"]
        or digest(packaged_dockerfile) != record["dockerfile_sha256"]
    ):
        raise ValueError("Gateway packaging inputs changed during staging")
    run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--file",
            str(packaged_dockerfile),
            "--build-arg",
            "GATEWAY_BASE_IMAGE=" + manifest["images"]["gateway_base"],
            "--iidfile",
            str(output / "gateway-image-id"),
            str(packaging),
        ]
    )
    image_ids["gateway"] = (output / "gateway-image-id").read_text().strip()
    name = "hw-controller-" + uuid4().hex[:12]
    runtime_tag = name + ":runtime"
    run(["docker", "tag", image_ids["runtime"], runtime_tag])
    # Keep the alias with the retained images; removing its last tag could delete an input image.
    run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--file",
            str(ROOT / "images/worker/candidate/controller.Dockerfile"),
            "--build-arg",
            "RUNTIME_IMAGE=" + runtime_tag,
            "--build-arg",
            "DOCKER_CLIENT_IMAGE=" + manifest["images"]["docker_client"],
            "--build-context",
            "worker-qualification-tools=" + str(artifacts),
            "--iidfile",
            str(output / "controller-image-id"),
            str(ROOT),
        ]
    )
    image_ids["controller"] = (output / "controller-image-id").read_text().strip()
    arguments = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        "heartwood-qualification-owner=" + name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--group-add",
        "0",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        "512",
        "--memory",
        "3g",
        "--network",
        "bridge",
        "--mount",
        f"type=bind,src={output},dst={output}",
        "--mount",
        "type=bind,src=/var/run/docker.sock,dst=/run/docker.sock",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev",
        "--workdir",
        str(output),
    ]
    environment = {
        "HW_CONTROLLER_NAME": name,
        "HW_WORKER_IMAGE": image_ids["worker"],
        "HW_GATEWAY_IMAGE": image_ids["gateway"],
        "HW_QUALIFICATION_OUTPUT": str(output),
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    for key, value in environment.items():
        arguments.extend(["--env", f"{key}={value}"])
    arguments.extend(
        [
            image_ids["controller"],
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-v",
            "--junitxml=" + str(output / "junit.xml"),
            "/opt/qualification/tests/test_worker_native_integration.py",
        ]
    )
    with (output / "qualification.log").open("w") as log:
        result = subprocess.run(arguments, stdout=log, stderr=subprocess.STDOUT)
    evidence = {
        "host": host,
        "qualification_sources": {
            str(path.relative_to(ROOT)): digest(path)
            for path in (
                ROOT / "deploy/qualify-native-worker.py",
                ROOT / "deploy/tests/test_worker_native_integration.py",
                ROOT / "deploy/tests/worker_native_fixture.py",
                ROOT / "deploy/tests/worker_hostile_probe.py",
            )
        },
        "images": image_ids,
        "gateway_build": record,
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "sensitive_data_ready": False,
    }
    (output / "qualification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    if result.returncode:
        raise RuntimeError("Native qualification failed; inspect qualification.log and junit.xml")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--gateway-artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    qualify(args.worker_image, args.runtime_image, args.gateway_artifacts, args.output)
