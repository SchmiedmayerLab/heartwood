# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify the isolated, supported Heartwood vLLM runtime contract."""

from __future__ import annotations

import ast
import re
import tomllib
from importlib import import_module
from importlib.metadata import distributions, version
from pathlib import Path

import torch
import vllm
from packaging.utils import canonicalize_name
from vllm.model_executor.models import ModelRegistry
from vllm.reasoning import ReasoningParserManager
from vllm.tool_parsers import ToolParserManager

_DEPENDENCY_VERSIONS = {
    "cuda-bindings": "12.9.7",
    "cuda-python": "12.9.7",
    "flashinfer-python": "0.6.16.post3",
    "nvidia-cuda-runtime-cu12": "12.9.79",
}
_REQUIRED_TOOL_PARSERS = ("hermes", "muse_glimmer", "openai", "qwen3_coder")
_REQUIRED_REASONING_PARSERS = ("muse_glimmer",)
_REQUIRED_MODEL_ARCHITECTURES = (
    "MuseGlimmerForCausalLM",
    "MuseGlimmerForConditionalGeneration",
)
_CUDA_RUNTIME_PATTERN = re.compile(rb"libcudart\.so\.(\d+)")
_FORBIDDEN_CUDA_13_PACKAGES = {
    "cuda-tile",
    "nvidia-cuda-crt",
    "nvidia-cuda-nvdisasm",
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
    native_cuda_versions = _native_cuda_linkages(Path(vllm.__file__).resolve().parent)
    expected_cuda_major = str(contract["cuda_version"]).partition(".")[0]
    if native_cuda_versions != {expected_cuda_major}:
        observed_linkages = ", ".join(sorted(native_cuda_versions)) or "none"
        raise RuntimeError("unexpected vLLM native CUDA runtime linkage: " + observed_linkages)

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
    available_reasoning_parsers = set(ReasoningParserManager.list_registered())
    missing_reasoning_parsers = sorted(
        set(_REQUIRED_REASONING_PARSERS) - available_reasoning_parsers
    )
    if missing_reasoning_parsers:
        missing = ", ".join(missing_reasoning_parsers)
        raise RuntimeError(f"required vLLM reasoning parsers are unavailable: {missing}")
    available_architectures = set(ModelRegistry.get_supported_archs())
    missing_architectures = sorted(set(_REQUIRED_MODEL_ARCHITECTURES) - available_architectures)
    if missing_architectures:
        missing = ", ".join(missing_architectures)
        raise RuntimeError(f"required vLLM model architectures are unavailable: {missing}")

    print(
        "Heartwood GPU runtime verified: "
        f"vLLM {observed['vllm']}, PyTorch {observed['torch']}, CUDA {torch.version.cuda}; "
        f"tool parsers {', '.join(_REQUIRED_TOOL_PARSERS)}; "
        f"reasoning parsers {', '.join(_REQUIRED_REASONING_PARSERS)}; "
        f"model architectures {', '.join(_REQUIRED_MODEL_ARCHITECTURES)}"
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


def _native_cuda_linkages(package_root: Path) -> set[str]:
    versions: set[str] = set()
    overlap = 32
    for path in sorted(package_root.rglob("*.so")):
        tail = b""
        try:
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    payload = tail + chunk
                    versions.update(
                        match.group(1).decode("ascii")
                        for match in _CUDA_RUNTIME_PATTERN.finditer(payload)
                    )
                    tail = payload[-overlap:]
        except OSError as error:
            raise RuntimeError(f"unable to inspect packaged vLLM binary: {path}") from error
    return versions


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
