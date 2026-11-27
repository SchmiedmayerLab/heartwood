# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests of the image publication record and the chart values that pin it."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
_CHART = Path(__file__).resolve().parents[2] / "chart" / "heartwood-platform"
_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_REGISTRY = "ghcr.io/example-owner"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "publish_images", _TOOLS / "publish_images.py"
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


publish_images = _load()


def _digest(index: int) -> str:
    return "sha256:" + f"{index:x}" * 64


def _record() -> dict[str, Any]:
    return {
        "commit": _COMMIT,
        "images": {
            name: {
                "repository": f"{_REGISTRY}/heartwood-{name}",
                "tag": f"sha-{_COMMIT}",
                "digest": _digest(index + 1),
            }
            for index, name in enumerate(publish_images.IMAGES)
        },
        "worker_interpreter": "/opt/heartwood/python/cpython-3.12.15/bin/python3.12",
    }


def _write(path: Path, record: dict[str, Any]) -> Path:
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_push_records_the_digest_of_each_push() -> None:
    tags: dict[str, str] = {}
    pushed: list[tuple[str, str]] = []

    def push_image(local: str, reference: str) -> str:
        pushed.append((local, reference))
        tags[reference] = _digest(len(tags) + 1)
        return tags[reference]

    published = publish_images.push(_REGISTRY, _COMMIT, tags.get, push_image)

    assert published == _record()["images"]
    assert pushed[0] == ("heartwood-worker:publish", f"{_REGISTRY}/heartwood-worker:sha-{_COMMIT}")


def test_push_never_replaces_an_existing_commit_tag() -> None:
    existing = {f"{_REGISTRY}/heartwood-funnel:sha-{_COMMIT}": _digest(1)}
    pushed: list[str] = []

    def push_image(_local: str, reference: str) -> str:
        pushed.append(reference)
        return _digest(1)

    with pytest.raises(publish_images.RecordError, match="already exists for funnel"):
        publish_images.push(_REGISTRY, _COMMIT, existing.get, push_image)
    assert pushed == []


def test_push_refuses_a_tag_that_names_another_digest() -> None:
    def push_image(_local: str, _reference: str) -> str:
        return _digest(1)

    tags = iter([None] * len(publish_images.IMAGES) + [_digest(2)])
    with pytest.raises(publish_images.RecordError, match="does not resolve to the pushed digest"):
        publish_images.push(_REGISTRY, _COMMIT, lambda _: next(tags), push_image)


def test_an_unreadable_registry_stops_the_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(arguments: list[str], **_: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(arguments, 1, "", "ERROR: unauthorized")

    def absent(arguments: list[str], **_: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(arguments, 1, "", "ERROR: ghcr.io/o/r:tag: not found")

    monkeypatch.setattr(publish_images.subprocess, "run", refused)
    with pytest.raises(publish_images.RecordError, match="could not be read"):
        publish_images._registry_digest("ghcr.io/o/r:tag")
    monkeypatch.setattr(publish_images.subprocess, "run", absent)
    assert publish_images._registry_digest("ghcr.io/o/r:tag") is None
    for answer in ("NAME_UNKNOWN: repository name not known", "MANIFEST_UNKNOWN: manifest unknown"):

        def unknown(
            arguments: list[str],
            answer: str = answer,
            **_: Any,  # noqa: ANN401
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 1, "", f"ERROR: {answer}")

        monkeypatch.setattr(publish_images.subprocess, "run", unknown)
        assert publish_images._registry_digest("ghcr.io/o/r:tag") is None


def test_the_gateway_packaging_of_the_qualification_loads() -> None:
    native = publish_images._native()

    assert callable(native.verified_gateway)
    assert callable(native.gateway_image)


@pytest.mark.parametrize(
    "change",
    [
        lambda record: record.pop("worker_interpreter"),
        lambda record: record.update(commit="main"),
        lambda record: record["images"].pop("funnel"),
        lambda record: record["images"]["worker"].update(digest="sha256:short"),
        lambda record: record["images"]["worker"].update(tag="latest"),
        lambda record: record["images"]["worker"].update(
            repository=f"{_REGISTRY}/heartwood-runtime"
        ),
        lambda record: record.update(worker_interpreter="python3"),
        lambda record: record["images"]["worker"].update(
            repository="registry.example.test/example-owner/heartwood-worker"
        ),
    ],
)
def test_values_refuse_an_incomplete_or_malformed_record(
    tmp_path: Path,
    change: Any,  # noqa: ANN401
) -> None:
    record = _record()
    change(record)
    arguments = argparse.Namespace(
        record=_write(tmp_path / "record.json", record),
        output=tmp_path / "values.yaml",
        repository="example-owner/heartwood",
    )

    with pytest.raises(publish_images.RecordError):
        publish_images.pin(arguments, lambda *_: None)
    assert not arguments.output.exists()


def test_values_are_written_only_after_every_attestation_verifies(tmp_path: Path) -> None:
    arguments = argparse.Namespace(
        record=_write(tmp_path / "record.json", _record()),
        output=tmp_path / "values.yaml",
        repository="example-owner/heartwood",
    )
    verified: list[tuple[str, str, str]] = []

    def verify(subject: str, repository: str, commit: str) -> None:
        verified.append((subject, repository, commit))
        if subject.endswith(_digest(6)):
            raise subprocess.CalledProcessError(1, "gh")

    with pytest.raises(subprocess.CalledProcessError):
        publish_images.pin(arguments, verify)
    assert not arguments.output.exists()
    assert verified[:2] == [
        (str(arguments.record), "example-owner/heartwood", _COMMIT),
        (f"oci://{_REGISTRY}/heartwood-worker@{_digest(1)}", "example-owner/heartwood", _COMMIT),
    ]


def test_verification_binds_the_workflow_branch_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(publish_images, "_run", commands.append)

    publish_images.verify_attestation(
        "oci://ghcr.io/o/heartwood-worker@" + _digest(1), "o/heartwood", _COMMIT
    )

    assert commands == [
        [
            "gh",
            "attestation",
            "verify",
            "oci://ghcr.io/o/heartwood-worker@" + _digest(1),
            "--repo",
            "o/heartwood",
            "--signer-workflow",
            "o/heartwood/.github/workflows/native-qualification.yml",
            "--source-ref",
            "refs/heads/main",
            "--source-digest",
            _COMMIT,
        ]
    ]


def test_the_chart_runs_the_pinned_images(tmp_path: Path) -> None:
    arguments = argparse.Namespace(
        record=_write(tmp_path / "record.json", _record()),
        output=tmp_path / "values-published.yaml",
        repository="example-owner/heartwood",
    )
    publish_images.pin(arguments, lambda *_: None)
    pinned = yaml.safe_load(arguments.output.read_text(encoding="utf-8"))
    worker = f"{_REGISTRY}/heartwood-worker@{_digest(1)}"

    assert pinned["hub"]["worker"]["image"] == worker
    assert pinned["sessions"]["stagingImages"] == [worker]
    assert pinned["hub"]["modelRoute"]["binaries"] == [_record()["worker_interpreter"]]
    models = (
        'inference.models=[{"name":"synthetic-coder","maxInputTokens":32768,'
        '"maxOutputTokens":4096,"litellmParams":{"model":"openai/synthetic-coder",'
        '"api_base":"https://models.example.test/v1"}}]'
    )
    output = subprocess.run(
        [
            "helm",
            "template",
            "heartwood-platform",
            str(_CHART),
            "--namespace",
            "heartwood-platform",
            "--values",
            str(_CHART / "values-kind.yaml"),
            "--values",
            str(arguments.output),
            "--set-json",
            models,
            "--set-json",
            'sessions.apiServer=[{"cidr":"10.96.0.1/32","port":443}]',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rendered = {
        container["image"]
        for item in yaml.safe_load_all(output)
        if item is not None and item["kind"] in {"Deployment", "StatefulSet", "Job"}
        for container in item["spec"]["template"]["spec"]["containers"]
    }
    for name in ("platform-services", "funnel"):
        image = _record()["images"][name]
        assert f"{image['repository']}@{image['digest']}" in rendered, name
    # The Hub starts the hosted gateway, the worker and the sandbox gateway from its settings.
    for name in ("hosted-gateway", "sandbox-gateway", "worker"):
        image = _record()["images"][name]
        assert f"{image['repository']}@{image['digest']}" in output, name
