# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify the isolated, supported Heartwood vLLM runtime contract."""

from __future__ import annotations

import ast
import tomllib
from importlib import import_module
from importlib.metadata import distributions, version
from pathlib import Path

import torch
import vllm
import vllm.envs as vllm_envs
from packaging.utils import canonicalize_name
from vllm.tool_parsers import ToolParserManager

_DEPENDENCY_VERSIONS = {
    "cuda-bindings": "12.9.7",
    "cuda-python": "12.9.7",
    "flashinfer-cubin": "0.6.13",
    "flashinfer-python": "0.6.13",
    "nvidia-cuda-runtime-cu12": "12.9.79",
}
_REQUIRED_TOOL_PARSERS = ("hermes", "openai", "qwen3_coder")
_FORBIDDEN_CUDA_13_PACKAGES = {
    "cuda-tile",
    "nvidia-cuda-crt",
    "nvidia-cuda-nvcc",
    "nvidia-cuda-runtime",
    "nvidia-cuda-tileiras",
    "nvidia-nvvm",
}


def main() -> None:
    """Reject a mixed or incomplete CUDA runtime before model startup."""
    contract = _runtime_contract()
    expected = {
        **_DEPENDENCY_VERSIONS,
        "torch": contract["pytorch_version"],
        "torchaudio": contract["torchaudio_version"],
        "torchvision": contract["torchvision_version"],
        "vllm": contract["vllm_version"],
    }
    observed = {package: version(package) for package in expected}
    if observed != expected:
        raise RuntimeError(f"unexpected GPU runtime versions: {observed}")
    if torch.version.cuda != contract["cuda_version"]:
        raise RuntimeError(f"unexpected PyTorch CUDA build: {torch.version.cuda}")
    if contract.get("cuda_13_qualified") is not False:
        raise RuntimeError("Heartwood's CUDA 13 runtime is not qualified")

    installed = {
        canonicalize_name(distribution.metadata["Name"])
        for distribution in distributions()
        if distribution.metadata.get("Name")
    }
    cuda_13 = sorted(
        name
        for name in installed
        if name in _FORBIDDEN_CUDA_13_PACKAGES or name.endswith("-cu13") or "-cu13-" in name
    )
    if cuda_13:
        raise RuntimeError(f"unqualified CUDA 13 packages are installed: {', '.join(cuda_13)}")

    if vllm_envs.VLLM_V1_USE_OUTLINES_CACHE:
        raise RuntimeError("the vLLM outlines disk cache must remain disabled")
    torchscript_calls = _torchscript_calls(Path(vllm.__file__).resolve().parent)
    if torchscript_calls:
        raise RuntimeError(
            "the supported vLLM runtime invokes torch.jit.script: " + ", ".join(torchscript_calls)
        )

    import_module("flashinfer")

    available_parsers = set(ToolParserManager.list_registered())
    missing_parsers = sorted(set(_REQUIRED_TOOL_PARSERS) - available_parsers)
    if missing_parsers:
        missing = ", ".join(missing_parsers)
        raise RuntimeError(f"required vLLM tool parsers are unavailable: {missing}")

    print(
        "Heartwood GPU runtime verified: "
        f"vLLM {observed['vllm']}, PyTorch {observed['torch']}, CUDA {torch.version.cuda}; "
        f"tool parsers {', '.join(_REQUIRED_TOOL_PARSERS)}"
    )


def _torchscript_calls(package_root: Path) -> list[str]:
    calls: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError) as error:
            raise RuntimeError(f"unable to inspect packaged vLLM source: {path}") from error
        torch_names = {"torch"}
        jit_names: set[str] = set()
        script_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "torch":
                        torch_names.add(alias.asname or alias.name)
                    elif alias.name == "torch.jit" and alias.asname:
                        jit_names.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.module == "torch":
                jit_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "jit"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "torch.jit":
                script_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "script"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            direct_call = isinstance(function, ast.Name) and function.id in script_names
            imported_jit_call = (
                isinstance(function, ast.Attribute)
                and function.attr == "script"
                and isinstance(function.value, ast.Name)
                and function.value.id in jit_names
            )
            torch_call = (
                isinstance(function, ast.Attribute)
                and function.attr == "script"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "jit"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id in torch_names
            )
            if direct_call or imported_jit_call or torch_call:
                calls.append(f"{path.relative_to(package_root)}:{node.lineno}")
    return calls


def _runtime_contract() -> dict[str, object]:
    path = Path(__file__).with_name("compatibility.toml")
    with path.open("rb") as file:
        payload = tomllib.load(file)
    if payload.get("schema_version") != "heartwood.gpu-compatibility.v2":
        raise RuntimeError("unsupported GPU compatibility contract")
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        raise RuntimeError("GPU runtime compatibility metadata is unavailable")
    return runtime


if __name__ == "__main__":
    main()
