# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the release-owned GPU runtime asset path."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def test_gpu_runtime_asset_is_hashed_and_added_to_release_manifest(tmp_path: Path) -> None:
    content = b"synthetic pinned wheel"
    digest = hashlib.sha256(content).hexdigest()
    filename = "vllm-1.2.3-py3-none-any.whl"
    compatibility = _compatibility(
        tmp_path,
        filename=filename,
        digest=digest,
        size=len(content),
    )
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
        size=len(expected),
    )
    source = tmp_path / filename
    source.write_bytes(b"different whee")
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


def test_gpu_runtime_download_retries_an_interrupted_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module("deploy/package_gpu_runtime.py", "package_gpu_runtime_retry")
    responses: list[object] = [ConnectionResetError("connection reset"), io.BytesIO(b"wheel")]
    delays: list[float] = []

    def urlopen(*_args: object, **_kwargs: object) -> object:
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", delays.append)
    destination = tmp_path / "runtime.whl"

    module._download("https://wheels.vllm.ai/revision/runtime.whl", destination)

    assert destination.read_bytes() == b"wheel"
    assert delays == [2.0]


def test_local_runtime_requirements_use_only_the_verified_release_wheel(
    tmp_path: Path,
) -> None:
    content = _synthetic_wheel()
    digest = hashlib.sha256(content).hexdigest()
    filename = "vllm-1.2.3-py3-none-any.whl"
    compatibility = _compatibility(
        tmp_path,
        filename=filename,
        digest=digest,
        size=len(content),
    )
    wheel = tmp_path / filename
    wheel.write_bytes(content)
    source_url = f"https://wheels.vllm.ai/revision/{filename.replace('+', '%2B')}"
    source = tmp_path / "requirements.txt"
    source.write_text(
        f"vllm @ {source_url}#sha256={digest} \\\n    --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    output = tmp_path / "localized.txt"
    module = _module(
        "images/gpu/localize_runtime_lock.py",
        "localize_runtime_lock",
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

    environment = tmp_path / "environment"
    subprocess.run((sys.executable, "-m", "venv", str(environment)), check=True)
    python = environment / "bin" / "python"
    completed = subprocess.run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--require-hashes",
            "--no-cache",
            "--no-index",
            "--requirements",
            str(output),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert (
        subprocess.run(
            (python, "-c", "import vllm; assert vllm.__version__ == '1.2.3'"),
            check=False,
        ).returncode
        == 0
    )


def test_local_runtime_rejects_wrong_filename_and_corrupted_wheel(tmp_path: Path) -> None:
    content = _synthetic_wheel()
    digest = hashlib.sha256(content).hexdigest()
    filename = "vllm-1.2.3+cu129-cp38-abi3-manylinux_2_28_x86_64.whl"
    compatibility = _compatibility(
        tmp_path,
        filename=filename,
        digest=digest,
        size=len(content),
    )
    source_url = f"https://wheels.vllm.ai/revision/{filename.replace('+', '%2B')}"
    source = tmp_path / "requirements.txt"
    source.write_text(
        f"vllm @ {source_url}#sha256={digest} \\\n    --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    module = _module("images/gpu/localize_runtime_lock.py", "localize_runtime_rejections")

    wrong_name = tmp_path / "renamed.whl"
    wrong_name.write_bytes(content)
    with pytest.raises(module.LocalRuntimeError, match="retain its pinned filename"):
        module.localize_requirements(
            source=source,
            output=tmp_path / "wrong-name.txt",
            wheel=wrong_name,
            compatibility_path=compatibility,
        )

    corrupted = tmp_path / filename
    corrupted.write_bytes(content + b"corrupted")
    with pytest.raises(module.LocalRuntimeError, match="digest differs"):
        module.localize_requirements(
            source=source,
            output=tmp_path / "corrupted.txt",
            wheel=corrupted,
            compatibility_path=compatibility,
        )


def _compatibility(tmp_path: Path, *, filename: str, digest: str, size: int) -> Path:
    path = tmp_path / "compatibility.toml"
    source_url = f"https://wheels.vllm.ai/revision/{filename.replace('+', '%2B')}"
    path.write_text(
        "[runtime]\n"
        f'vllm_wheel_filename = "{filename}"\n'
        f'vllm_wheel_source_url = "{source_url}"\n'
        f"vllm_wheel_size_bytes = {size}\n"
        f'vllm_wheel_sha256 = "{digest}"\n',
        encoding="utf-8",
    )
    return path


def _synthetic_wheel() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        files = {
            "vllm/__init__.py": "__version__ = '1.2.3'\n",
            "vllm-1.2.3.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: vllm\nVersion: 1.2.3\n"
            ),
            "vllm-1.2.3.dist-info/WHEEL": (
                "Wheel-Version: 1.0\nGenerator: heartwood-test\n"
                "Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
        }
        records = []
        for name, value in files.items():
            archive.writestr(name, value)
            records.append(f"{name},,\n")
        records.append("vllm-1.2.3.dist-info/RECORD,,\n")
        archive.writestr("vllm-1.2.3.dist-info/RECORD", "".join(records))
    return stream.getvalue()


def _module(path: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(path))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
