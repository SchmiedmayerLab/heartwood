# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Behavioral tests for immutable container image tag publication."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACTION_PATH = _REPO_ROOT / ".github/actions/create-immutable-image-tag/action.yml"

_DIGEST_A = f"sha256:{'a' * 64}"
_DIGEST_B = f"sha256:{'b' * 64}"
_AMD64_DIGEST = f"sha256:{'c' * 64}"
_ARM64_DIGEST = f"sha256:{'d' * 64}"
_ATTESTATION_DIGEST = f"sha256:{'e' * 64}"
_SECOND_ATTESTATION_DIGEST = f"sha256:{'f' * 64}"

_FAKE_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_DOCKER_STATE"])
calls_path = Path(os.environ["FAKE_DOCKER_CALLS"])
state = json.loads(state_path.read_text(encoding="utf-8"))
arguments = sys.argv[1:]

with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(arguments) + "\n")

if arguments[:3] == ["buildx", "imagetools", "create"]:
    if "--dry-run" in arguments:
        print(json.dumps(state["dry_run_raw"]))
    else:
        state["created"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")
    raise SystemExit(0)

if arguments[:3] == ["buildx", "imagetools", "inspect"]:
    available = state.get("tag_exists", False) or state.get("created", False)
    if not available:
        print("manifest unknown", file=sys.stderr)
        raise SystemExit(1)
    if "--raw" in arguments:
        key = "existing_raw" if state.get("tag_exists", False) else "published_raw"
        print(json.dumps(state[key]))
    else:
        print(f"Name: {arguments[-1]}")
        print(f"Digest: {state['tag_digest']}")
    raise SystemExit(0)

print(f"unsupported fake docker invocation: {arguments}", file=sys.stderr)
raise SystemExit(2)
"""


@dataclass(frozen=True)
class _ActionResult:
    process: subprocess.CompletedProcess[str]
    calls: tuple[tuple[str, ...], ...]
    output: dict[str, str]
    state: dict[str, Any]


def test_new_docker_v2_tag_is_created_and_flattened(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_DIGEST_A}",
        candidate_digest=_DIGEST_A,
        flatten="true",
        validation_mode="docker-v2",
        state=_state(published_raw=_docker_v2_manifest(), tag_digest=_DIGEST_A),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state["created"] is True
    assert _publish_calls(result.calls) == (
        (
            "buildx",
            "imagetools",
            "create",
            "--prefer-index=false",
            "--tag",
            "registry.test/heartwood:immutable",
            f"registry.test/heartwood@{_DIGEST_A}",
        ),
    )


def test_existing_equivalent_manifest_is_idempotent(tmp_path: Path) -> None:
    proposed = _multi_platform_index()
    existing = _multi_platform_index(reverse=True)
    result = _run_action(
        tmp_path,
        references=(
            f"registry.test/heartwood@{_AMD64_DIGEST} registry.test/heartwood@{_ARM64_DIGEST}"
        ),
        validation_mode="linux-multi-platform-index",
        state=_state(
            dry_run_raw=proposed,
            existing_raw=existing,
            tag_digest=_DIGEST_A,
            tag_exists=True,
        ),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state.get("created") is not True
    assert _publish_calls(result.calls) == ()


def test_existing_immutable_tag_rejects_conflicting_manifest(tmp_path: Path) -> None:
    proposed = _multi_platform_index()
    conflicting = _multi_platform_index(arm64_digest=_DIGEST_B)
    result = _run_action(
        tmp_path,
        references=(
            f"registry.test/heartwood@{_AMD64_DIGEST} registry.test/heartwood@{_ARM64_DIGEST}"
        ),
        validation_mode="linux-multi-platform-index",
        state=_state(
            dry_run_raw=proposed,
            existing_raw=conflicting,
            tag_digest=_DIGEST_A,
            tag_exists=True,
        ),
    )

    assert result.process.returncode == 1
    assert "already exists with a different manifest" in result.process.stderr
    assert result.state.get("created") is not True
    assert _publish_calls(result.calls) == ()


def test_existing_immutable_tag_rejects_conflicting_digest(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_DIGEST_A}",
        candidate_digest=_DIGEST_A,
        validation_mode="docker-v2",
        state=_state(
            existing_raw=_docker_v2_manifest(),
            tag_digest=_DIGEST_B,
            tag_exists=True,
        ),
    )

    assert result.process.returncode == 1
    assert "already exists with a different digest" in result.process.stderr
    assert f"expected digest: {_DIGEST_A}; observed digests: {_DIGEST_B}" in (result.process.stderr)
    assert result.state.get("created") is not True
    assert _publish_calls(result.calls) == ()


def test_new_tag_rejects_published_digest_mismatch(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_DIGEST_A}",
        candidate_digest=_DIGEST_A,
        validation_mode="docker-v2",
        state=_state(published_raw=_docker_v2_manifest(), tag_digest=_DIGEST_B),
    )

    assert result.process.returncode == 1
    assert "newly created test image does not match validated candidate digest" in (
        result.process.stderr
    )
    assert f"expected digest: {_DIGEST_A}; observed digests: {_DIGEST_B}" in (result.process.stderr)
    assert result.state["created"] is True


@pytest.mark.parametrize(
    ("references", "validation_mode", "diagnostic"),
    [
        ("", "docker-v2", "has no candidate references"),
        (
            f"registry.test/a@{_DIGEST_A} registry.test/b@{_DIGEST_B}",
            "docker-v2",
            "requires exactly one candidate reference",
        ),
        (
            f"registry.test/a@{_DIGEST_A}",
            "linux-multi-platform-index",
            "requires exactly two candidate references",
        ),
        (
            (
                f"registry.test/a@{_DIGEST_A} "
                f"registry.test/b@{_DIGEST_B} "
                f"registry.test/c@{_AMD64_DIGEST}"
            ),
            "linux-multi-platform-index",
            "requires exactly two candidate references",
        ),
    ],
)
def test_invalid_source_counts_fail_before_docker(
    tmp_path: Path,
    references: str,
    validation_mode: str,
    diagnostic: str,
) -> None:
    candidate_digest = _DIGEST_A if validation_mode == "docker-v2" else ""
    result = _run_action(
        tmp_path,
        references=references,
        candidate_digest=candidate_digest,
        validation_mode=validation_mode,
        state=_state(),
    )

    assert result.process.returncode == 1
    assert diagnostic in result.process.stderr
    assert result.calls == ()


def test_linux_amd64_index_is_created_and_validated(tmp_path: Path) -> None:
    index = _image_index([_platform_manifest(_AMD64_DIGEST, "amd64")])
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_AMD64_DIGEST}",
        candidate_digest=_AMD64_DIGEST,
        validation_mode="linux-amd64-index",
        state=_state(published_raw=index, tag_digest=_DIGEST_A),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state["created"] is True
    assert "--prefer-index=false" not in _publish_calls(result.calls)[0]


def test_linux_amd64_index_accepts_an_index_candidate_digest(tmp_path: Path) -> None:
    index = _image_index([_platform_manifest(_AMD64_DIGEST, "amd64")])
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_DIGEST_A}",
        candidate_digest=_DIGEST_A,
        validation_mode="linux-amd64-index",
        state=_state(published_raw=index, tag_digest=_DIGEST_A),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state["created"] is True


def test_existing_linux_amd64_index_validates_the_wrapped_manifest_digest(
    tmp_path: Path,
) -> None:
    index = _image_index([_platform_manifest(_AMD64_DIGEST, "amd64")])
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_AMD64_DIGEST}",
        candidate_digest=_AMD64_DIGEST,
        validation_mode="linux-amd64-index",
        state=_state(
            existing_raw=index,
            tag_digest=_DIGEST_A,
            tag_exists=True,
        ),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state.get("created") is not True


def test_linux_amd64_index_rejects_a_different_wrapped_manifest_digest(
    tmp_path: Path,
) -> None:
    index = _image_index([_platform_manifest(_DIGEST_B, "amd64")])
    result = _run_action(
        tmp_path,
        references=f"registry.test/heartwood@{_AMD64_DIGEST}",
        candidate_digest=_AMD64_DIGEST,
        validation_mode="linux-amd64-index",
        state=_state(published_raw=index, tag_digest=_DIGEST_A),
    )

    assert result.process.returncode == 1
    assert "does not match validated candidate digest" in result.process.stderr
    assert f"expected digest: {_AMD64_DIGEST}; observed digests: {_DIGEST_A}, {_DIGEST_B}" in (
        result.process.stderr
    )


def test_linux_amd64_and_arm64_index_is_created_and_validated(
    tmp_path: Path,
) -> None:
    index = _multi_platform_index()
    result = _run_action(
        tmp_path,
        references=(
            f"registry.test/heartwood@{_AMD64_DIGEST} registry.test/heartwood@{_ARM64_DIGEST}"
        ),
        validation_mode="linux-multi-platform-index",
        state=_state(
            dry_run_raw=index,
            published_raw=index,
            tag_digest=_DIGEST_A,
        ),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state["created"] is True
    assert len(_publish_calls(result.calls)) == 1


def test_multi_platform_index_allows_attestation_manifests(tmp_path: Path) -> None:
    index = _image_index(
        [
            _platform_manifest(_AMD64_DIGEST, "amd64"),
            _attestation_manifest(_ATTESTATION_DIGEST),
            _platform_manifest(_ARM64_DIGEST, "arm64"),
            _attestation_manifest(_SECOND_ATTESTATION_DIGEST),
        ]
    )
    result = _run_action(
        tmp_path,
        references=(
            f"registry.test/heartwood@{_AMD64_DIGEST} registry.test/heartwood@{_ARM64_DIGEST}"
        ),
        validation_mode="linux-multi-platform-index",
        state=_state(
            dry_run_raw=index,
            published_raw=index,
            tag_digest=_DIGEST_A,
        ),
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == {"digest": _DIGEST_A}
    assert result.state["created"] is True


def _run_action(
    tmp_path: Path,
    *,
    references: str,
    validation_mode: str,
    state: dict[str, Any],
    candidate_digest: str = "",
    flatten: str = "false",
) -> _ActionResult:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)

    state_path = tmp_path / "docker-state.json"
    calls_path = tmp_path / "docker-calls.jsonl"
    output_path = tmp_path / "github-output"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    output_path.touch()

    environment = os.environ.copy()
    environment.update(
        {
            "CANDIDATE_DIGEST": candidate_digest,
            "CANDIDATE_REFERENCES": references,
            "CANDIDATE_TAG": "registry.test/heartwood:immutable",
            "DIAGNOSTIC_NAME": "test image",
            "FAKE_DOCKER_CALLS": str(calls_path),
            "FAKE_DOCKER_STATE": str(state_path),
            "FLATTEN": flatten,
            "GITHUB_OUTPUT": str(output_path),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "VALIDATION_MODE": validation_mode,
        }
    )
    process = subprocess.run(
        ["bash", "-c", _action_script()],
        cwd=_REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    calls = (
        tuple(
            tuple(json.loads(line)) for line in calls_path.read_text(encoding="utf-8").splitlines()
        )
        if calls_path.exists()
        else ()
    )
    output = dict(
        line.split("=", maxsplit=1) for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    return _ActionResult(
        process=process,
        calls=calls,
        output=output,
        state=final_state,
    )


def _action_script() -> str:
    action = _ACTION_PATH.read_text(encoding="utf-8")
    marker = "      run: |\n"
    assert action.count(marker) == 1
    block = action.split(marker, maxsplit=1)[1]
    lines = block.splitlines()
    assert all(not line or line.startswith("        ") for line in lines)
    return "\n".join(line[8:] if line else "" for line in lines)


def _state(
    *,
    dry_run_raw: dict[str, Any] | None = None,
    existing_raw: dict[str, Any] | None = None,
    published_raw: dict[str, Any] | None = None,
    tag_digest: str = _DIGEST_A,
    tag_exists: bool = False,
) -> dict[str, Any]:
    return {
        "created": False,
        "dry_run_raw": dry_run_raw or _multi_platform_index(),
        "existing_raw": existing_raw or _docker_v2_manifest(),
        "published_raw": published_raw or _docker_v2_manifest(),
        "tag_digest": tag_digest,
        "tag_exists": tag_exists,
    }


def _docker_v2_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "digest": _DIGEST_A,
            "size": 1,
        },
        "layers": [],
    }


def _multi_platform_index(
    *,
    arm64_digest: str = _ARM64_DIGEST,
    reverse: bool = False,
) -> dict[str, Any]:
    manifests = [
        _platform_manifest(_AMD64_DIGEST, "amd64"),
        _platform_manifest(arm64_digest, "arm64"),
    ]
    if reverse:
        manifests.reverse()
    return _image_index(manifests)


def _image_index(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": manifests,
    }


def _platform_manifest(digest: str, architecture: str) -> dict[str, Any]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 1,
        "platform": {"os": "linux", "architecture": architecture},
    }


def _attestation_manifest(digest: str) -> dict[str, Any]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 1,
        "annotations": {
            "vnd.docker.reference.type": "attestation-manifest",
        },
        "platform": {"os": "unknown", "architecture": "unknown"},
    }


def _publish_calls(
    calls: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        call
        for call in calls
        if call[:3] == ("buildx", "imagetools", "create") and "--dry-run" not in call
    )
