# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Reject substituted candidate build inputs before they can execute."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "worker_candidate", _ROOT / "deploy/build-worker-candidate.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "config"
    config.mkdir()
    constraints = b"synthetic build constraints"
    patch = b"synthetic reviewed patch"
    (config / "build-requirements.txt").write_bytes(constraints)
    (config / "reviewed.patch").write_bytes(patch)
    (config / "sources.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "purpose": "qualification-only",
                "uv": "0.13.7",
                "python": "3.12.15",
                "source_date_epoch": 1,
                "build_requirements_sha256": hashlib.sha256(constraints).hexdigest(),
                "sources": {
                    "synthetic": {
                        "url": "https://example.invalid/source.tar.gz",
                        "sha256": "a" * 64,
                        "patches": [
                            {"path": "reviewed.patch", "sha256": hashlib.sha256(patch).hexdigest()}
                        ],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(module, "_CONFIG", config)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *_args, **_kwargs: "uv 0.13.7")
    return module


@pytest.mark.parametrize("filename", ["reviewed.patch", "build-requirements.txt"])
def test_tampered_build_input_cannot_start_a_download(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    (builder._CONFIG / filename).write_text("substituted input")
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("downloaded")
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        builder.build(tmp_path / "wheels", "uv")
    assert not (tmp_path / "wheels").exists()


def test_wrong_build_tool_is_rejected(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *_args, **_kwargs: "uv 0.13.6")
    with pytest.raises(ValueError, match="requires uv"):
        builder.build(tmp_path / "wheels", "uv")
    assert not (tmp_path / "wheels").exists()


def test_existing_output_is_preserved(builder: ModuleType, tmp_path: Path) -> None:
    output = tmp_path / "wheels"
    output.mkdir()
    (output / "existing.whl").write_bytes(b"existing work")
    with pytest.raises(FileExistsError):
        builder.build(output, "uv")
    assert (output / "existing.whl").read_bytes() == b"existing work"


def test_changed_archive_cannot_execute_build_code(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "untrusted.tar.gz"
    archive.write_bytes(b"substituted archive")
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_args, **_kwargs: archive.open("rb")
    )
    monkeypatch.setattr(
        builder.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed source")
    )
    with pytest.raises(ValueError, match="source digest mismatch"):
        builder.build(tmp_path / "wheels", "uv")
    assert not (tmp_path / "wheels/build-record.json").exists()


@pytest.mark.parametrize("condition", ["valid", "missing-license", "duplicate-identity"])
def test_wheel_identity_requires_preserved_license_and_unambiguous_metadata(
    builder: ModuleType, tmp_path: Path, condition: str
) -> None:
    wheel = tmp_path / "synthetic.whl"
    metadata = "Name: synthetic\nVersion: 1.0+heartwood.1\n"
    if condition == "duplicate-identity":
        metadata += "Name: substituted\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("synthetic.dist-info/METADATA", metadata)
        if condition != "missing-license":
            archive.writestr("synthetic.dist-info/licenses/LICENSE", "Synthetic test license")
    if condition == "valid":
        assert builder._wheel_identity(wheel) == ("synthetic", "1.0+heartwood.1")
    else:
        with pytest.raises(ValueError, match="Candidate wheel"):
            builder._wheel_identity(wheel)
