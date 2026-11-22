# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Collect the finished case files of an attended qualification from its deployed controller."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import subprocess
import time
import tomllib
from pathlib import Path

from gke_qualification import BOUNDARY, fields, read, write
from qualification_evidence import (
    DRILLS,
    EXPECTED_CASES,
    MANIFEST,
    REPORT,
    cases,
    drill_results,
    failures,
    verify,
    write_checksums,
)

PYTHON = "/opt/heartwood/.venv/bin/python"
SOURCES = {
    **{
        f"/opt/qualification/tests/{p.name}": p
        for p in Path("deploy/tests").glob("worker_network_*.py")
    },
    **{
        f"/opt/qualification/tests/{name}.py": Path(f"deploy/tests/{name}.py")
        for name in (
            "test_worker_kubernetes_integration",
            "worker_kubernetes_fixture",
            "gke_session_receipt",
            "worker_native_fixture",
            "test_worker_native_integration",
            "test_worker_lifecycle_integration",
            "worker_telemetry",
            "worker_hostile_probe",
        )
    },
    **{
        f"/opt/qualification/deploy/tests/{p}.py": Path(f"deploy/tests/{p}.py")
        for p in ("checkpoint_records_integration", "checkpoint_transport_integration")
    },
    "/opt/qualification/worker_model_profile.py": Path("deploy/worker_model_profile.py"),
    "/opt/qualification/packages/audit/tests/tls_fixture.py": Path(
        "packages/audit/tests/tls_fixture.py"
    ),
}


def command(*args: str) -> bytes:
    """Read one bounded command result."""
    return subprocess.check_output(args, timeout=60)


def collect(root: Path) -> None:
    """Bind candidate code to the observed controller and retain its completed suite."""
    env = os.environ
    namespace, pod, container = (
        env[k] for k in ("CONTROLLER_NAMESPACE", "CONTROLLER_POD", "CONTROLLER_CONTAINER")
    )
    target = ("kubectl", "-n", namespace)

    def identity() -> tuple[str, str, str]:
        live = json.loads(command(*target, "get", "pod", pod, "-ojson"))
        (spec,) = [c for c in live["spec"]["containers"] if c["name"] == container]
        (status,) = [c for c in live["status"]["containerStatuses"] if c["name"] == container]
        if (
            live["spec"]["serviceAccountName"] != env["CONTROLLER_ACCOUNT"]
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9./:_-]+@sha256:[0-9a-f]{64}", env["CONTROLLER_IMAGE"]
            )
            or spec["image"] != env["CONTROLLER_IMAGE"]
            or not status["ready"]
        ):
            raise ValueError("qualification controller differs from protected configuration")
        return live["metadata"]["uid"], status["containerID"], live["spec"]["nodeName"]

    def remote(code: str, *args: str) -> bytes:
        return command(*target, "exec", pod, "-c", container, "--", PYTHON, "-I", "-c", code, *args)

    before = identity()
    hash_code = (
        "import pathlib,hashlib,json,sys;print(json.dumps({p:hashlib.sha256("
        "pathlib.Path(p).read_bytes()).hexdigest() for p in sys.argv[1:]}))"
    )
    expected = {p: hashlib.sha256(f.read_bytes()).hexdigest() for p, f in SOURCES.items()}
    if json.loads(remote(hash_code, *SOURCES)) != expected:
        raise ValueError("controller does not contain the candidate qualification sources")
    run = env["GITHUB_RUN_ID"]
    path = f"/qualification/output/{run}.1"
    if remote("import os,sys;print(os.path.lexists(sys.argv[1]))", path).strip() != b"False":
        raise ValueError("qualification output already exists before collection")
    print(f"Awaiting the attended suite in {path} on the selected controller", flush=True)
    deadline = time.monotonic() + 5400
    while (
        remote(
            "import pathlib,sys;print(pathlib.Path(sys.argv[1]).is_file())",
            path + "/session-receipt.json",
        ).strip()
        != b"True"
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError("no completed attended suite")
        time.sleep(10)

    def file(name: str) -> bytes:
        return remote(
            "import pathlib,sys;root=pathlib.Path(sys.argv[1]);p=root/sys.argv[2];"
            "assert p.resolve()==p and p.is_relative_to(root) and p.is_file();"
            "assert p.stat().st_size<16777216;"
            "sys.stdout.buffer.write(p.read_bytes())",
            path,
            name,
        )

    receipt = json.loads(file("session-receipt.json"))
    execution = {"run": int(run), "attempt": 1, "commit": env["GITHUB_SHA"]}
    if (
        fields(receipt, *execution) != execution
        or type(receipt["exitstatus"]) is not int
        or receipt["exitstatus"] != 0
    ):
        raise ValueError("GKE pytest execution failed or belongs to another run")
    permitted = {
        case.split("::")[-1] + "/evidence.json" for case in EXPECTED_CASES["synthetic-kubernetes"]
    } | {REPORT}
    retained = receipt["files"]
    if not {REPORT, BOUNDARY} <= retained.keys() <= permitted:
        raise ValueError("unexpected GKE evidence inventory")
    dest = root / "observations/gke-native"
    dest.mkdir(parents=True)
    for relative, digest in retained.items():
        raw = file(relative)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("GKE case file differs from pytest receipt")
        output = dest / relative
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(raw)
    boundary = read(dest / BOUNDARY)
    binding = read(root / "binding.json")
    if (
        boundary["gke_execution"] != {"run": int(run), "attempt": 1, "commit": env["GITHUB_SHA"]}
        or boundary["kubernetes_identities"]["namespace_uid"] != binding["namespace_uid"]
    ):
        raise ValueError("boundary belongs to another execution or session")
    nodes = json.loads(command("kubectl", "get", "nodes", "-ojson"))["items"]
    platforms = [
        {
            "name": n["metadata"]["name"],
            **fields(n["status"]["nodeInfo"], "operatingSystem", "architecture"),
        }
        for n in nodes
    ]
    (node,) = [n for n in platforms if n["name"] == before[2]]
    if before != identity() or json.loads(remote(hash_code, *SOURCES)) != expected:
        raise ValueError("controller identity or code changed during qualification")
    # Namespace retirement is performed by its owner. A replacement of the same name is ambiguous.
    if command(
        "kubectl", "get", "namespace", binding["namespace"], "--ignore-not-found", "-oname"
    ).strip():
        command(
            "kubectl", "wait", "--for=delete", "namespace/" + binding["namespace"], "--timeout=45s"
        )
    if command(
        "kubectl", "get", "namespace", binding["namespace"], "--ignore-not-found", "-oname"
    ).strip():
        raise ValueError("session namespace is still present")
    current_system = json.loads(command("kubectl", "get", "namespace", "kube-system", "-ojson"))
    if current_system["metadata"]["uid"] != binding["kube_system_uid"]:
        raise ValueError("cluster changed during collection")
    commit = command("git", "rev-parse", "HEAD").decode().strip()
    if commit != env["GITHUB_SHA"] or command("git", "status", "--porcelain").strip():
        raise ValueError("qualification checkout differs from the dispatched commit")
    published = read(root / "published-images.json")
    embedded_records = runpy.run_path("deploy/qualify-native-worker.py")["embedded_build_records"]
    sources = {}
    for role in ("worker", "runtime"):
        item = published["images"][role]
        ref = item["repository"] + "@" + item["digest"]
        inspected = json.loads(command("docker", "image", "inspect", ref))[0]
        build = embedded_records(ref)["application"]
        if role == "runtime":
            running_build = json.loads(
                remote(
                    "from pathlib import Path;root=Path('/opt/heartwood/qualification');"
                    "print((root/'application-build-record.json').read_text())"
                )
            )
            if running_build != build:
                raise ValueError(
                    "deployed controller application differs from the candidate runtime"
                )
        sources[role] = {
            "revision": inspected["Config"]["Labels"]["org.opencontainers.image.revision"],
            "build_records": {"application": build},
        }
    manifest = {
        "qualification_kind": "synthetic-kubernetes",
        "driver": "openshell-kubernetes",
        "source": {"commit": commit, "tree_clean": True},
        "qualification_sources_unchanged": True,
        "host": {"os": node["operatingSystem"], "architecture": node["architecture"]},
        "cluster": {"nodes": platforms, "kubernetes": binding["kubernetes"]},
        "workflow_run": {"id": int(run), "attempt": 1},
        "release_version": tomllib.loads(Path("VERSION.toml").read_text())["version"],
        "kubernetes_identities": fields(binding, "namespace", "namespace_uid", "kube_system_uid"),
        "session_removed": True,
        "image_sources": sources,
        "requested_model_profile": None,
        "cases": cases(dest / REPORT),
        "returncode": receipt["exitstatus"],
        "records_service": None,
    }
    manifest["passed"] = not failures(manifest)
    write(dest / MANIFEST, manifest)
    write(dest / DRILLS, drill_results(dest))
    write_checksums(dest)
    if problems := verify(dest, commit):
        raise ValueError("GKE qualification failed: " + "; ".join(problems))


if __name__ == "__main__":
    collect(Path(os.environ["RUN_ROOT"]))
