# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Reject substituted candidate build inputs before they can execute."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]


def test_gateway_stamp_preserves_registry_dependencies(builder: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[workspace.package]\nversion = "0.0.0"\n')
    (tmp_path / "Cargo.lock").write_text(
        'version = 4\n[[package]]\nname = "openshell-core"\nversion = "0.0.0"\n'
        '[[package]]\nname = "synthetic"\nversion = "10.0.0"\n'
        'source = "registry+https://example.invalid/index"\nchecksum = "unchanged"\n'
    )
    builder._gateway_version(tmp_path, "0.1.8+heartwood.1")
    packages = tomllib.loads((tmp_path / "Cargo.lock").read_text())["package"]
    assert packages[0]["version"] == "0.1.8+heartwood.1"
    assert packages[1] == {
        "name": "synthetic",
        "version": "10.0.0",
        "source": "registry+https://example.invalid/index",
        "checksum": "unchanged",
    }


def test_gateway_stamp_rejects_registry_zero_version(builder: ModuleType, tmp_path: Path) -> None:
    metadata = '[workspace.package]\nversion = "0.0.0"\n'
    (tmp_path / "Cargo.toml").write_text(metadata)
    lock = (
        'version = 4\n[[package]]\nname = "openshell-core"\nversion = "0.0.0"\n'
        'source = "registry+https://example.invalid/index"\n'
    )
    (tmp_path / "Cargo.lock").write_text(lock)
    with pytest.raises(ValueError, match="workspace versions"):
        builder._gateway_version(tmp_path, "0.1.8+heartwood.1")
    assert (tmp_path / "Cargo.toml").read_text() == metadata
    assert (tmp_path / "Cargo.lock").read_text() == lock


@pytest.mark.parametrize("invalid", ["image", "patch"])
def test_gateway_rejects_changed_inputs_before_docker(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    patch = builder._CONFIG / "reviewed.patch"
    images = {"rust": "rust@sha256:" + "1" * 64}
    manifest = {
        "schema_version": 1,
        "purpose": "qualification-only",
        "images": images,
        "patches": [{"path": patch.name, "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}],
    }
    if invalid == "image":
        images["rust"] = "rust:latest"
    else:
        patch.write_text("substituted patch")
    (builder._CONFIG / "gateway.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(
        builder.subprocess,
        "check_output",
        lambda *_args, **_kwargs: pytest.fail("contacted Docker"),
    )
    with pytest.raises(ValueError, match=r"immutable digests|digest mismatch"):
        builder.build_gateway(tmp_path / "output", tmp_path / "cache")
    assert not (tmp_path / "output").exists()


def test_source_cache_is_reverified_before_extraction(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    digest = "a" * 64
    (cache / (digest + ".tar.gz")).write_bytes(b"substituted cache content")
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("downloaded")
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        builder._source({"sha256": digest}, tmp_path / "staging", cache=cache)
    assert not (tmp_path / "staging").exists()


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
                "purpose": "maintained-dependencies",
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


@pytest.mark.parametrize("changed", [None, "wheel", "record", "extra"])
def test_bootstrap_verifies_cached_wheels_without_rebuilding(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    output = tmp_path / "runtime-wheels"
    output.mkdir()
    wheel = output / "synthetic.whl"
    wheel.write_bytes(b"reviewed wheel")
    manifest_path = builder._CONFIG / "sources.json"
    manifest = json.loads(manifest_path.read_text())
    expected = {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()}
    manifest["wheels_sha256"] = expected
    manifest_path.write_text(json.dumps(manifest))
    record = {
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "wheels": expected,
    }
    if changed == "wheel":
        wheel.write_bytes(b"substituted wheel")
    elif changed == "record":
        record["manifest_sha256"] = "substituted"
    elif changed == "extra":
        (output / "unreviewed.whl").write_bytes(b"extra dependency")
    (output / "build-record.json").write_text(json.dumps(record))
    monkeypatch.setattr(builder, "build", lambda *_args: pytest.fail("rebuilt cached dependencies"))
    if changed is None:
        builder.bootstrap(output, "uv")
    else:
        with pytest.raises(ValueError, match="differ"):
            builder.bootstrap(output, "uv")


def test_bootstrap_does_not_follow_a_dependency_directory_symlink(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "outside"
    destination.mkdir()
    output = tmp_path / "runtime-wheels"
    output.symlink_to(destination, target_is_directory=True)
    monkeypatch.setattr(builder, "build", lambda *_args: pytest.fail("followed dependency symlink"))
    with pytest.raises(ValueError, match="symlink"):
        builder.bootstrap(output, "uv")
    assert not list(destination.iterdir())


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


def test_application_metadata_binds_the_candidate_dependency_versions(
    builder: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "application"
    project.mkdir()
    metadata = project / "pyproject.toml"
    metadata.write_text(
        '[project]\nname = "heartwood-synthetic"\nversion = "0.4.0"\n'
        'dependencies = ["filelock==3.32.2", "unchanged>=1"]\n'
    )
    settings = {
        "version_suffix": "+heartwood.1",
        "dependency_replacements": {"filelock==3.32.2": "filelock==4.2.10"},
    }
    assert builder._application_metadata(project, settings) == (
        "heartwood-synthetic",
        "0.4.0+heartwood.1",
    )
    content = metadata.read_text()
    assert '"filelock==4.2.10"' in content
    assert '"unchanged>=1"' in content


@pytest.mark.parametrize("dependency", ["filelock==3.32.1", "filelock>=3", "filelock<4"])
def test_application_metadata_rejects_unreviewed_dependency_changes(
    builder: ModuleType, tmp_path: Path, dependency: str
) -> None:
    project = tmp_path / "application"
    project.mkdir()
    metadata = project / "pyproject.toml"
    original = (
        '[project]\nname = "heartwood-synthetic"\nversion = "0.4.0"\n'
        f'dependencies = ["{dependency}"]\n'
    )
    metadata.write_text(original)
    with pytest.raises(ValueError, match="differs from the qualification manifest"):
        builder._application_metadata(
            project,
            {
                "version_suffix": "+heartwood.1",
                "dependency_replacements": {"filelock==3.32.2": "filelock==4.2.10"},
            },
        )
    assert metadata.read_text() == original


@pytest.mark.parametrize("result", ["passed", "empty", "missing"])
def test_gateway_tests_require_positive_results_and_preserve_build_limits(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    container = ["docker", "run", "--memory", "4g", "--memory-swap", "5g"]
    original = list(container)
    observed: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> None:
        observed.append(command)
        log = kwargs["stdout"]
        assert hasattr(log, "write")
        log.write(
            "test result: ok. 4 passed; 0 failed; 0 ignored;\n"
            if result == "passed"
            else "test result: ok. 0 passed; 0 failed; 0 ignored;\n"
            if result == "empty"
            else "no test results\n"
        )
        assert kwargs["check"] is True
        assert kwargs["stderr"] == builder.subprocess.STDOUT

    monkeypatch.setattr(builder.subprocess, "run", run)
    if result == "passed":
        record = builder._gateway_tests(container, "rust@sha256:" + "a" * 64, tmp_path)
        assert record["passed"] == 4
        assert (
            record["log_sha256"]
            == hashlib.sha256((tmp_path / "gateway-tests.log").read_bytes()).hexdigest()
        )
    else:
        with pytest.raises(ValueError, match="nonempty passing suite"):
            builder._gateway_tests(container, "rust@sha256:" + "a" * 64, tmp_path)
    assert container == original
    command = observed[0]
    assert command[command.index("--memory") + 1] == "12g"
    assert command[command.index("--memory-swap") + 1] == "12g"
    assert command[command.index("--jobs") + 1] == "1"
    assert command[command.index("--features") + 1] == "telemetry,vendored-z3"
    assert command[command.index("--config") + 1] == (
        "profile.release.package.openshell-server.opt-level=0"
    )
    assert command[-3:] == ["delete", "--", "--test-threads=1"]
    assert json.loads((tmp_path / "gateway-test-command.json").read_text()) == command


def test_gateway_regression_failure_prevents_binary_build(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "Cargo.toml").write_text('[workspace.package]\nversion = "0.0.0"\n')
    (source / "Cargo.lock").write_text(
        'version = 4\n[[package]]\nname = "openshell-server"\nversion = "0.0.0"\n'
    )
    manifest = {
        "version": "0.1.8+heartwood.1",
        "source": {},
        "patches": [],
        "cmake": {"amd64": {}},
        "cli": {"amd64": {}},
        "images": {"rust": "rust@sha256:" + "a" * 64},
    }
    monkeypatch.setattr(builder, "_gateway_configuration", lambda: (manifest, b"manifest"))
    monkeypatch.setattr(builder, "_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *_args, **_kwargs: "linux/amd64")
    observed: list[list[str]] = []

    def fail(command: list[str], **kwargs: object) -> None:
        observed.append(command)
        assert "test" in command
        log = kwargs["stdout"]
        assert hasattr(log, "write")
        log.write("synthetic upstream test failure\n")
        raise builder.subprocess.CalledProcessError(101, command)

    monkeypatch.setattr(builder.subprocess, "run", fail)
    output = tmp_path / "output"
    with pytest.raises(builder.subprocess.CalledProcessError):
        builder.build_gateway(output, tmp_path / "cache", gateway_tests=True)
    assert len(observed) == 1
    assert (output / "gateway-tests.log").read_text() == "synthetic upstream test failure\n"
    assert not (output / "bin").exists()
    assert not (output / "gateway-build-record.json").exists()


@pytest.mark.parametrize("gateway", [False, True])
def test_gateway_tests_cli_requires_gateway(
    builder: ModuleType, monkeypatch: pytest.MonkeyPatch, gateway: bool
) -> None:
    import sys

    arguments = ["builder", "--gateway-tests", "--output", "output", "--cache", "cache"]
    if gateway:
        arguments.append("--gateway")
    monkeypatch.setattr(sys, "argv", arguments)
    calls = []
    monkeypatch.setattr(
        builder, "build_gateway", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    if gateway:
        builder.main()
        assert calls == [((Path("output"), Path("cache")), {"gateway_tests": True})]
    else:
        with pytest.raises(SystemExit) as failure:
            builder.main()
        assert failure.value.code == 2
        assert not calls
