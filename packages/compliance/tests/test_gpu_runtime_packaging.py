# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the release-owned GPU runtime asset path."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def test_gpu_runtime_asset_is_hashed_and_added_to_release_manifest(tmp_path: Path) -> None:
    content = b"synthetic pinned wheel"
    digest = hashlib.sha256(content).hexdigest()
    filename = "vllm-1.2.3+cu129-cp38-abi3-manylinux_2_28_x86_64.whl"
    compatibility = _compatibility(tmp_path, filename=filename, digest=digest)
    source = tmp_path / filename
    source.write_bytes(content)
    output = tmp_path / "dist"
    output.mkdir()
    (output / "SHA256SUMS").write_text(
        f"{'0' * 64}  heartwood-native.tar.gz\n",
        encoding="utf-8",
    )

    module = _module("deploy/package_gpu_runtime.py", "package_gpu_runtime")
    destination = module.package_runtime_asset(
        output_dir=output,
        compatibility_path=compatibility,
        source_file=source,
    )

    assert destination.read_bytes() == content
    assert (output / "SHA256SUMS").read_text(encoding="utf-8") == (
        f"{'0' * 64}  heartwood-native.tar.gz\n{digest}  {filename}\n"
    )


def test_gpu_runtime_asset_rejects_a_different_wheel(tmp_path: Path) -> None:
    expected = b"expected wheel"
    filename = "vllm-1.2.3+cu129-cp38-abi3-manylinux_2_28_x86_64.whl"
    compatibility = _compatibility(
        tmp_path,
        filename=filename,
        digest=hashlib.sha256(expected).hexdigest(),
    )
    source = tmp_path / filename
    source.write_bytes(b"different wheel")
    output = tmp_path / "dist"
    output.mkdir()
    (output / "SHA256SUMS").write_text(
        f"{'0' * 64}  heartwood-native.tar.gz\n",
        encoding="utf-8",
    )
    module = _module("deploy/package_gpu_runtime.py", "package_gpu_runtime_mismatch")

    with pytest.raises(module.RuntimeAssetError, match="digest differs"):
        module.package_runtime_asset(
            output_dir=output,
            compatibility_path=compatibility,
            source_file=source,
        )

    assert not (output / filename).exists()


def test_local_runtime_requirements_use_only_the_verified_release_wheel(
    tmp_path: Path,
) -> None:
    content = b"synthetic pinned wheel"
    digest = hashlib.sha256(content).hexdigest()
    filename = "vllm-1.2.3+cu129-cp38-abi3-manylinux_2_28_x86_64.whl"
    compatibility = _compatibility(tmp_path, filename=filename, digest=digest)
    wheel = tmp_path / filename
    wheel.write_bytes(content)
    source_url = f"https://wheels.vllm.ai/revision/{filename.replace('+', '%2B')}"
    source = tmp_path / "requirements.txt"
    source.write_text(
        f"vllm @ {source_url}#sha256={digest} \\\n"
        f"    --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    output = tmp_path / "localized.txt"
    module = _module(
        "images/gpu/localize_runtime_requirements.py",
        "localize_runtime_requirements",
    )

    module.localize_requirements(
        source=source,
        output=output,
        wheel=wheel,
        compatibility_path=compatibility,
    )

    localized = output.read_text(encoding="utf-8")
    assert wheel.resolve().as_uri() in localized
    assert source_url not in localized
    assert digest in localized


def _compatibility(tmp_path: Path, *, filename: str, digest: str) -> Path:
    path = tmp_path / "compatibility.toml"
    source_url = f"https://wheels.vllm.ai/revision/{filename.replace('+', '%2B')}"
    path.write_text(
        "[runtime]\n"
        f'vllm_wheel_filename = "{filename}"\n'
        f'vllm_wheel_source_url = "{source_url}"\n'
        f'vllm_wheel_sha256 = "{digest}"\n',
        encoding="utf-8",
    )
    return path


def _module(path: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(path))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
