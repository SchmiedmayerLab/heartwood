# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Publish the platform images of one commit and pin them in chart values.

``build`` builds the worker, runtime and hosted gateway images with
``images/worker/build-candidate-images.sh``, packages the sandbox gateway image from a
verified gateway build, and builds the platform services and Funnel images, each as
``heartwood-<name>:publish``.
It needs no registry credential.
``push`` pushes each image to ``REGISTRY/heartwood-<name>:sha-<commit>``, refuses to
replace a tag that already exists, and writes a record with the digest of each push.
``values`` verifies the attestations of a record and of each image in it with
``gh attestation verify`` and writes chart values that pin the images by digest.

Usage: publish_images.py build --commit COMMIT --wheels DIRECTORY
           --gateway-artifacts DIRECTORY --output BUILD
       publish_images.py push BUILD --registry REGISTRY --output RECORD
       publish_images.py values RECORD [--output VALUES] [--repository OWNER/NAME]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VALUES = ROOT / "reference-platform/chart/heartwood-platform/values-published.yaml"
WORKFLOW = ".github/workflows/native-qualification.yml"
IMAGES = (
    "worker",
    "runtime",
    "hosted-gateway",
    "sandbox-gateway",
    "platform-services",
    "funnel",
)
_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REGISTRY = re.compile(r"[a-z0-9]+([.-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+")
_PYTHON = "/opt/heartwood/.venv/bin/python"
_PUSHED = re.compile(r"digest: (sha256:[0-9a-f]{64}) ")
_HEADER = """\
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Images that the Native Qualification workflow published for commit {commit}, pinned by
# digest. reference-platform/tools/publish_images.py values wrote this file from that
# run's record after it verified the attestations of the record and of each image.
"""


class RecordError(Exception):
    """A record of published images is incomplete or malformed."""


def _output(arguments: list[str]) -> str:
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout


def _run(arguments: list[str]) -> None:
    subprocess.run(arguments, check=True)


def _native() -> Any:  # noqa: ANN401
    """Load the Docker qualification's runner, which owns the gateway packaging."""
    specification = importlib.util.spec_from_file_location(
        "native_qualification", ROOT / "deploy/qualify-native-worker.py"
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    # The runner imports its sibling modules in deploy/.
    if str(ROOT / "deploy") not in sys.path:
        sys.path.insert(0, str(ROOT / "deploy"))
    specification.loader.exec_module(module)
    return module


def build_images(wheels: Path, artifacts: Path, commit: str, work: Path) -> None:
    """Build every image of the commit as heartwood-<name>:publish."""
    _run(
        [
            "bash",
            str(ROOT / "images/worker/build-candidate-images.sh"),
            str(wheels),
            commit,
            "publish",
        ]
    )
    native = _native()
    manifest, record = native.verified_gateway(artifacts.resolve())
    gateway = native.gateway_image(artifacts.resolve(), manifest, record, work)
    _run(["docker", "tag", gateway, "heartwood-sandbox-gateway:publish"])
    _run(
        [
            "docker",
            "build",
            "--file",
            str(ROOT / "reference-platform/services/Dockerfile"),
            "--tag",
            "heartwood-platform-services:publish",
            str(ROOT),
        ]
    )
    funnel = work / "funnel"
    _run(
        [
            sys.executable,
            str(ROOT / "reference-platform/tools/prepare_funnel.py"),
            str(funnel),
            "--image",
            "heartwood-funnel:publish",
        ]
    )


def interpreter(worker: str) -> str:
    """Return the resolved path of the worker image's interpreter."""
    return _output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            _PYTHON,
            worker,
            "-I",
            "-c",
            "from pathlib import Path;import sys;print(Path(sys.executable).resolve())",
        ]
    ).strip()


def _registry_digest(reference: str) -> str | None:
    """Return the digest the registry serves for a reference, or None when it has none."""
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Only an answer that the tag does not exist counts as absent; any other failure,
        # such as a refused credential or an unreachable registry, stops the publication.
        answer = result.stderr.lower()
        if "not found" in answer or "name_unknown" in answer or "manifest_unknown" in answer:
            return None
        raise RecordError(f"the registry could not be read for {reference}: {result.stderr}")
    for line in result.stdout.splitlines():
        field, _, value = line.partition(":")
        if field == "Digest":
            return value.strip()
    raise RecordError(f"the registry named no digest for {reference}")


def _push_image(local: str, reference: str) -> str:
    """Push a local image under a reference and return the digest the push reported."""
    _run(["docker", "tag", local, reference])
    output = _output(["docker", "push", reference])
    print(output, end="")
    pushed = _PUSHED.findall(output)
    if not pushed:
        raise RecordError(f"the push of {reference} reported no digest")
    return str(pushed[-1])


def push(
    registry: str,
    commit: str,
    digest_of: Callable[[str], str | None] = _registry_digest,
    push_image: Callable[[str, str], str] = _push_image,
) -> dict[str, dict[str, str]]:
    """Push each image under its commit tag and return the digest of each push."""
    tag = f"sha-{commit}"
    targets = {name: f"{registry}/heartwood-{name}" for name in IMAGES}
    # The tool writes a commit's tags once. After a partial publication the commit stays
    # unpublished, and the next commit publishes its own images.
    existing = [name for name in IMAGES if digest_of(f"{targets[name]}:{tag}") is not None]
    if existing:
        raise RecordError(f"{tag} already exists for {', '.join(existing)}")
    published = {}
    for name in IMAGES:
        reference = f"{targets[name]}:{tag}"
        digest = push_image(f"heartwood-{name}:publish", reference)
        # A tag that another writer moved meanwhile no longer names this push.
        if not _DIGEST.fullmatch(digest) or digest_of(reference) != digest:
            raise RecordError(f"{reference} does not resolve to the pushed digest {digest}")
        published[name] = {"repository": targets[name], "tag": tag, "digest": digest}
    return published


def build(arguments: argparse.Namespace) -> None:
    """Build the images of the checked-out commit and record what the push needs."""
    commit = arguments.commit
    if not _COMMIT.fullmatch(commit):
        raise RecordError("the commit is a full SHA-1")
    if _output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip() != commit:
        raise RecordError(f"the checkout is not commit {commit}")
    if subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD"]).returncode != 0:
        raise RecordError("the checkout has changes to tracked files")
    with TemporaryDirectory() as work:
        build_images(arguments.wheels, arguments.gateway_artifacts, commit, Path(work))
    built = {"commit": commit, "worker_interpreter": interpreter("heartwood-worker:publish")}
    arguments.output.write_text(json.dumps(built, indent=2) + "\n", encoding="utf-8")


def publish(arguments: argparse.Namespace) -> None:
    """Push the built images and write the record of their digests."""
    built = json.loads(arguments.build.read_text(encoding="utf-8"))
    commit, registry = built["commit"], arguments.registry
    if not _COMMIT.fullmatch(commit) or not _REGISTRY.fullmatch(registry):
        raise RecordError("the commit is a full SHA-1 and the registry a lowercase repository path")
    record = {
        "commit": commit,
        "images": push(registry, commit),
        "worker_interpreter": built["worker_interpreter"],
    }
    arguments.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    read_record(arguments.output)


def read_record(path: Path) -> dict[str, Any]:
    """Read a record and require every image by digest under the commit's tag."""
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or set(record) != {"commit", "images", "worker_interpreter"}:
        raise RecordError("the record has other fields than commit, images and worker_interpreter")
    commit, images = record["commit"], record["images"]
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise RecordError("the record's commit is not a full SHA-1")
    if not isinstance(images, dict) or set(images) != set(IMAGES):
        raise RecordError(f"the record names other images than {', '.join(IMAGES)}")
    for name, image in images.items():
        if (
            not isinstance(image, dict)
            or set(image) != {"repository", "tag", "digest"}
            or not _REGISTRY.fullmatch(str(image["repository"]))
            or not str(image["repository"]).endswith(f"/heartwood-{name}")
            or image["tag"] != f"sha-{commit}"
            or not _DIGEST.fullmatch(str(image["digest"]))
        ):
            raise RecordError(
                f"the record's {name} image is not a repository, commit tag and digest"
            )
    binary = record["worker_interpreter"]
    if not isinstance(binary, str) or not binary.startswith("/") or "\n" in binary:
        raise RecordError("the record's worker_interpreter is not an absolute path")
    return record


def verify_attestation(subject: str, repository: str, commit: str) -> None:
    """Require a provenance attestation of a subject by the main-branch run of a commit."""
    _run(
        [
            "gh",
            "attestation",
            "verify",
            subject,
            "--repo",
            repository,
            "--signer-workflow",
            f"{repository}/{WORKFLOW}",
            "--source-ref",
            "refs/heads/main",
            "--source-digest",
            commit,
        ]
    )


def values(record: dict[str, Any]) -> str:
    """Return chart values that pin every image of a record by digest."""
    pinned = {
        name: f"{image['repository']}@{image['digest']}" for name, image in record["images"].items()
    }
    return _HEADER.format(commit=record["commit"]) + "\n".join(
        [
            "images:",
            f"  platformServices: {pinned['platform-services']}",
            f"  openshellGateway: {pinned['sandbox-gateway']}",
            f"  hostedGateway: {pinned['hosted-gateway']}",
            f"  modelRuntime: {pinned['runtime']}",
            f"  funnel: {pinned['funnel']}",
            "hub:",
            "  worker:",
            f"    image: {pinned['worker']}",
            "  modelRoute:",
            "    binaries:",
            f"      - {json.dumps(record['worker_interpreter'])}",
            "sessions:",
            "  stagingImages:",
            f"    - {pinned['worker']}",
            "",
        ]
    )


def pin(
    arguments: argparse.Namespace,
    verify: Callable[[str, str, str], None] = verify_attestation,
) -> None:
    """Verify a record and each image in it and write the chart values that pin them."""
    record = read_record(arguments.record)
    # Verification is by digest, so the repositories are bound to the attesting owner here.
    owner = arguments.repository.partition("/")[0].lower()
    for name, image in record["images"].items():
        if image["repository"] != f"ghcr.io/{owner}/heartwood-{name}":
            raise RecordError(f"the record's {name} image is not under ghcr.io/{owner}")
    # The record's attestation covers the worker's interpreter, which no image attests.
    verify(str(arguments.record), arguments.repository, record["commit"])
    for image in record["images"].values():
        reference = f"oci://{image['repository']}@{image['digest']}"
        verify(reference, arguments.repository, record["commit"])
    arguments.output.write_text(values(record), encoding="utf-8")


def main() -> int:
    """Run the command named on the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("build", help=build.__doc__)
    built.add_argument("--commit", required=True)
    built.add_argument("--wheels", required=True, type=Path)
    built.add_argument("--gateway-artifacts", required=True, type=Path)
    built.add_argument("--output", required=True, type=Path)
    pushed = commands.add_parser("push", help=publish.__doc__)
    pushed.add_argument("build", type=Path)
    pushed.add_argument("--registry", required=True, help="such as ghcr.io/OWNER")
    pushed.add_argument("--output", required=True, type=Path)
    pinned = commands.add_parser("values", help=pin.__doc__)
    pinned.add_argument("record", type=Path)
    pinned.add_argument("--output", type=Path, default=VALUES)
    pinned.add_argument("--repository", default="SchmiedmayerLab/heartwood")
    arguments = parser.parse_args()
    try:
        {"build": build, "push": publish, "values": pin}[arguments.command](arguments)
    except (RecordError, ValueError, subprocess.CalledProcessError) as error:
        print(f"publish_images: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
