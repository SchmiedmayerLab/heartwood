# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Reject substituted candidate build inputs before they can execute."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
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


type _Tree = dict[str, tuple[int | str, bytes]]

_TREE: _Tree = {
    "LICENSE": (0o644, b"Synthetic license"),
    "bin/tool": (0o755, b"#!/bin/sh\n"),
    "src/module.py": (0o644, b"VALUE = 1\n"),
    "src/alias.py": ("module.py", b""),
}


def _archive(
    tree: _Tree,
    root: str = "Upstream-abc",
    *,
    regenerated: bool = False,
) -> bytes:
    """Build a revision archive; a regenerated one differs only in archive metadata."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as bundle:
        if not regenerated:
            bundle.pax_headers = {"comment": "abc"}
        directories = sorted({str(Path(name).parent) for name in tree} - {"."})
        for name in ["", *directories]:
            entry = tarfile.TarInfo(f"{root}/{name}".rstrip("/"))
            entry.type, entry.mode = tarfile.DIRTYPE, 0o775 if regenerated else 0o755
            bundle.addfile(entry)
        for name, (mode, content) in sorted(tree.items(), reverse=regenerated):
            entry = tarfile.TarInfo(f"{root}/{name}")
            entry.mtime, entry.uname = (1, "regenerated") if regenerated else (0, "")
            if isinstance(mode, str):
                entry.type, entry.linkname = tarfile.SYMTYPE, mode
                bundle.addfile(entry)
            else:
                entry.mode, entry.size = mode, len(content)
                bundle.addfile(entry, io.BytesIO(content))
    return gzip.compress(buffer.getvalue(), compresslevel=1 if regenerated else 9, mtime=0)


def _tree_digest(builder: ModuleType, tree: _Tree, path: Path) -> str:
    archive = path.with_suffix(".tar.gz")
    archive.write_bytes(_archive(tree))
    with pytest.raises(ValueError, match="tree_sha256 is ") as failure:
        builder._source({"url": archive.as_uri(), "tree_sha256": "0" * 64}, path)
    return str(failure.value).rsplit(" ", 1)[1]


def _serve(monkeypatch: pytest.MonkeyPatch, builder: ModuleType, archive: bytes) -> list[str]:
    requests: list[str] = []

    def urlopen(url: str, **_kwargs: object) -> io.BytesIO:
        requests.append(url)
        return io.BytesIO(archive)

    monkeypatch.setattr(builder.urllib.request, "urlopen", urlopen)
    return requests


def test_regenerated_revision_archive_verifies_by_content(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _tree_digest(builder, _TREE, tmp_path / "reviewed")
    regenerated = _archive(_TREE, "renamed-abc", regenerated=True)
    assert regenerated != _archive(_TREE)
    source = {"url": "https://example.invalid/abc", "tree_sha256": expected}
    cache = tmp_path / "cache"
    cache.mkdir()
    requests = _serve(monkeypatch, builder, regenerated)
    checkout = builder._source(source, tmp_path / "first", cache=cache)
    assert checkout.name == "renamed-abc"
    assert (checkout / "src/alias.py").readlink() == Path("module.py")
    assert os.access(checkout / "bin/tool", os.X_OK)
    assert requests == [source["url"]]
    # A cached archive with other bytes but the same content is accepted without a download.
    assert (cache / f"{expected}.tar.gz").read_bytes() == regenerated
    (cache / f"{expected}.tar.gz").write_bytes(_archive(_TREE))
    builder._source(source, tmp_path / "second", cache=cache)
    assert requests == [source["url"]]


@pytest.mark.parametrize(
    "change",
    ["content", "mode", "link", "added", "removed", "renamed", "directory", "second-root"],
)
def test_changed_revision_content_fails_closed(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    expected = _tree_digest(builder, _TREE, tmp_path / "reviewed")
    tree: _Tree = dict(_TREE)
    if change == "content":
        tree["src/module.py"] = (0o644, b"VALUE = 2\n")
    elif change == "mode":
        tree["LICENSE"] = (0o755, tree["LICENSE"][1])
    elif change == "link":
        tree["src/alias.py"] = ("../LICENSE", b"")
    elif change == "added":
        tree["src/added.py"] = (0o644, b"")
    elif change == "removed":
        del tree["bin/tool"]
    elif change == "renamed":
        tree["src/renamed.py"] = tree.pop("src/module.py")
    archive = _archive(tree)
    if change in {"directory", "second-root"}:
        buffer = io.BytesIO(gzip.decompress(archive))
        with tarfile.open(fileobj=buffer, mode="a") as bundle:
            entry = tarfile.TarInfo(
                "Upstream-abc/empty" if change == "directory" else "Other-abc/hook.py"
            )
            if change == "directory":
                entry.type = tarfile.DIRTYPE
                bundle.addfile(entry)
            else:
                bundle.addfile(entry, io.BytesIO(b""))
        archive = gzip.compress(buffer.getvalue())
    cache = tmp_path / "cache"
    cache.mkdir()
    _serve(monkeypatch, builder, archive)
    with pytest.raises(ValueError, match="digest mismatch"):
        builder._source(
            {"url": "https://example.invalid/abc", "tree_sha256": expected},
            tmp_path / "staging",
            cache=cache,
        )
    assert not list(cache.iterdir())


@pytest.mark.parametrize(
    ("name", "link"),
    [
        ("../outside", None),
        ("/heartwood-absolute-entry", None),
        ("Upstream-abc/escape", "../../outside"),
        ("Upstream-abc/escape", "/"),
    ],
)
def test_revision_archive_cannot_write_outside_staging(
    builder: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    link: str | None,
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        entry = tarfile.TarInfo(name)
        if link is None:
            bundle.addfile(entry, io.BytesIO(b""))
        else:
            entry.type, entry.linkname = tarfile.SYMTYPE, link
            bundle.addfile(entry)
            bundle.addfile(tarfile.TarInfo(name + "/written"), io.BytesIO(b""))
    _serve(monkeypatch, builder, gzip.compress(buffer.getvalue()))
    work = tmp_path / "work"
    with pytest.raises(ValueError, match="digest mismatch"):
        builder._source({"url": "https://example.invalid", "tree_sha256": "0" * 64}, work / "a")
    assert [path.name for path in work.iterdir()] == ["a"]
    assert not (tmp_path / "outside").exists()
    assert not Path("/heartwood-absolute-entry").exists()
    assert not Path("/written").exists()


@pytest.mark.parametrize(
    "digests",
    [{}, {"sha256": "a" * 64, "tree_sha256": "a" * 64}, {"tree_sha256": "../outside"}],
)
def test_source_records_exactly_one_digest(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digests: dict[str, str]
) -> None:
    _serve(monkeypatch, builder, b"")
    with pytest.raises(ValueError, match=r"exactly one|hex digest"):
        builder._source(
            {"url": "https://example.invalid", **digests}, tmp_path / "staging", cache=tmp_path
        )


def test_records_fixture_verifies_the_same_tree_digest(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "records_fixture", _ROOT / "deploy/build-records-fixture.py"
    )
    assert spec is not None
    assert spec.loader is not None
    records = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(records)
    expected = _tree_digest(builder, _TREE, tmp_path / "reviewed")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(
        records.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(_archive(_TREE, "renamed", regenerated=True)),
    )
    source = {"url": "https://example.invalid", "tree_sha256": expected}
    checkout, _ = records.source_checkout(source, tmp_path / "first", cache)
    assert records.tree_digest(checkout) == expected
    changed: _Tree = {**_TREE, "LICENSE": (0o644, b"Changed license")}
    (cache / f"{expected}.tar.gz").write_bytes(_archive(changed))
    with pytest.raises(ValueError, match="digest mismatch"):
        records.source_checkout(source, tmp_path / "second", cache)


def test_generated_revision_archives_are_pinned_by_content() -> None:
    candidate = _ROOT / "images/worker/candidate"
    gateway = json.loads((candidate / "gateway.json").read_text())
    sources = [
        gateway["source"],
        *gateway["cmake"].values(),
        *gateway["cli"].values(),
        *json.loads((candidate / "sources.json").read_text())["sources"].values(),
        *json.loads((candidate / "records.json").read_text())["sources"].values(),
    ]
    for source in sources:
        generated = source["url"].startswith("https://codeload.github.com/")
        assert generated == ("tree_sha256" in source) != ("sha256" in source), source["url"]
        if generated:
            assert source["url"].endswith("/tar.gz/" + source["revision"])
            assert "directory" not in source


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
                        "tree_sha256": "a" * 64,
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


def test_gateway_objects_are_not_reused_across_manifests(
    builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "version": "0.1.8+heartwood.1",
        "source": {},
        "patches": [],
        "cmake": {"amd64": {}},
        "cli": {"amd64": {}},
        "images": {"rust": "rust@sha256:" + "a" * 64},
    }

    def extract(_source: object, staging: Path, **_kwargs: object) -> Path:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "Cargo.toml").write_text('[workspace.package]\nversion = "0.0.0"\n')
        (staging / "Cargo.lock").write_text(
            'version = 4\n[[package]]\nname = "openshell-server"\nversion = "0.0.0"\n'
        )
        return staging

    monkeypatch.setattr(builder, "_source", extract)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *_args, **_kwargs: "linux/amd64")
    targets: list[str] = []

    def stop(command: list[str], **_kwargs: object) -> None:
        targets.extend(item for item in command if item.endswith(",dst=/target"))
        raise builder.subprocess.CalledProcessError(101, command)

    monkeypatch.setattr(builder.subprocess, "run", stop)
    # The version stays the same, as when a patch is withdrawn without a new suffix.
    for attempt, content in enumerate((b"reviewed", b"reviewed", b"changed")):
        monkeypatch.setattr(
            builder, "_gateway_configuration", lambda content=content: (manifest, content)
        )
        with pytest.raises(builder.subprocess.CalledProcessError):
            builder.build_gateway(tmp_path / f"output-{attempt}", tmp_path / "cache")
    assert len(targets) == 3
    assert targets[0] == targets[1] != targets[2]


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


def test_candidate_application_install_keeps_bundled_skills_verifiable(tmp_path: Path) -> None:
    """Install the real gateway wheel the way the candidate image does."""
    from heartwood.gateway._skill_settings import pinned_bundled_manifests

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("requires uv")
    application = tmp_path / "application"
    subprocess.run(
        [uv, "build", "--wheel", "packages/gateway", "--out-dir", str(application)],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(application.glob("heartwood_gateway-*.whl"))
    name, version = wheel.name.split("-")[:2]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (application / "application-requirements.txt").write_text(
        f"{name.replace('_', '-')}=={version} --hash=sha256:{digest}\n"
    )
    environment = tmp_path / "environment"
    subprocess.run(
        [uv, "venv", "--no-config", "--quiet", "--python", sys.executable, str(environment)],
        check=True,
    )
    python = environment / "bin" / "python"
    # The candidate image compiles bytecode for every uv installation by default.
    image_environment = {**os.environ, "UV_COMPILE_BYTECODE": "1", "UV_LINK_MODE": "copy"}
    subprocess.run(
        [
            "bash",
            "images/worker/candidate/install-application.sh",
            uv,
            str(python),
            str(application),
        ],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        env=image_environment,
    )
    purelib = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bundled = Path(purelib) / "heartwood/gateway/_bundled/vendor/heartwood-skills/skills"

    assert not list(bundled.rglob("__pycache__"))
    assert {manifest.name for manifest in pinned_bundled_manifests(bundled)} == {
        "aggregate-export",
        "baseline-model",
        "omop-cohort-summary",
    }
