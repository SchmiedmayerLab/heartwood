# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Run the CUDA 11.8 vLLM build with its reviewed Transformers compatibility boundary."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path

_VULNERABLE_CONFIG_TYPE = "Llama_Nemotron_Nano_VL"
_COMPATIBILITY_MARKER = "_heartwood_vllm_transformers_compatibility"
_REMOVED_CONFIG_MARKER = "_heartwood_vllm_removed_vulnerable_config"


def _apply_transformers_compatibility() -> None:
    from transformers.configuration_utils import PreTrainedConfig

    if getattr(PreTrainedConfig, _COMPATIBILITY_MARKER, False):
        return
    original = PreTrainedConfig.__init_subclass__.__func__

    def compatible_init_subclass(
        cls: type[PreTrainedConfig], *args: object, **kwargs: object
    ) -> None:
        if cls.__module__.startswith("vllm."):
            super(PreTrainedConfig, cls).__init_subclass__(*args, **kwargs)
            return
        original(cls, *args, **kwargs)

    PreTrainedConfig.__init_subclass__ = classmethod(compatible_init_subclass)
    setattr(PreTrainedConfig, _COMPATIBILITY_MARKER, True)


def _apply_tokenizer_compatibility() -> None:
    from transformers.tokenization_utils_base import PreTrainedTokenizerBase

    if hasattr(PreTrainedTokenizerBase, "all_special_tokens_extended"):
        return

    @property
    def all_special_tokens_extended(self: PreTrainedTokenizerBase) -> list[str]:
        return self.all_special_tokens

    PreTrainedTokenizerBase.all_special_tokens_extended = (  # type: ignore[attr-defined]
        all_special_tokens_extended
    )


def _apply_vllm_security_backport() -> type[object]:
    from vllm.transformers_utils import config as config_module

    removed = getattr(config_module, _REMOVED_CONFIG_MARKER, None)
    if removed is None:
        removed = config_module._CONFIG_REGISTRY.pop(_VULNERABLE_CONFIG_TYPE, None)
        setattr(config_module, _REMOVED_CONFIG_MARKER, removed)
    if removed is None or removed.__name__ != "Nemotron_Nano_VL_Config":
        raise RuntimeError("the reviewed vLLM security backport no longer matches the runtime")
    return removed


def activate_runtime_boundary() -> type[object]:
    """Apply the reviewed compatibility and security boundary idempotently."""
    _apply_transformers_compatibility()
    _apply_tokenizer_compatibility()
    return _apply_vllm_security_backport()


def _configure_child_bootstrap() -> None:
    runtime_bin = Path(__file__).resolve().parent
    bootstrap = runtime_bin / "sitecustomize.py"
    if not bootstrap.is_file():
        raise RuntimeError(f"vLLM child bootstrap is unavailable: {bootstrap}")
    os.environ["PYTHONPATH"] = str(runtime_bin)


def _verify_runtime(removed_config: type[object]) -> None:
    import idna  # noqa: F401
    import transformers
    import xgrammar  # noqa: F401
    from vllm.transformers_utils import config as config_module
    from vllm.transformers_utils.tokenizer import get_tokenizer  # noqa: F401
    from vllm.v1.structured_output import backend_xgrammar  # noqa: F401

    if _VULNERABLE_CONFIG_TYPE in config_module._CONFIG_REGISTRY:
        raise RuntimeError("the vulnerable vLLM configuration remains registered")
    if version("xgrammar") != "0.1.32":
        raise RuntimeError("the reviewed xgrammar security override is unavailable")
    if version("idna") != "3.18":
        raise RuntimeError("the reviewed idna security override is unavailable")

    vulnerable_module = importlib.import_module(removed_config.__module__)
    dynamic_loader_called = False

    def reject_dynamic_loader(*_args: object, **_kwargs: object) -> type[object]:
        nonlocal dynamic_loader_called
        dynamic_loader_called = True
        raise RuntimeError("vulnerable dynamic configuration loader reached")

    vulnerable_module.get_class_from_dynamic_module = reject_dynamic_loader

    with tempfile.TemporaryDirectory() as directory:
        model = Path(directory)
        (model / "config.json").write_text(
            json.dumps(
                {
                    "architectures": ["Qwen2ForCausalLM"],
                    "model_type": "qwen2",
                    "num_attention_heads": 2,
                    "num_hidden_layers": 2,
                    "num_key_value_heads": 2,
                    "hidden_size": 128,
                    "intermediate_size": 256,
                    "vocab_size": 256,
                }
            ),
            encoding="utf-8",
        )
        config = config_module.get_config(model, trust_remote_code=False)
        if config.model_type != "qwen2":
            raise RuntimeError(f"unexpected synthetic model type: {config.model_type}")

        (model / "config.json").write_text(
            json.dumps(
                {
                    "model_type": _VULNERABLE_CONFIG_TYPE,
                    "vision_config": {
                        "auto_map": {"AutoConfig": "untrusted/repository--configuration.Payload"}
                    },
                }
            ),
            encoding="utf-8",
        )
        with suppress(KeyError, OSError, RuntimeError, ValueError):
            config_module.get_config(model, trust_remote_code=False)
        if dynamic_loader_called:
            raise RuntimeError("the vulnerable vLLM dynamic configuration loader was called")

    child = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "from transformers.tokenization_utils_base import PreTrainedTokenizerBase; "
                "assert hasattr(PreTrainedTokenizerBase, 'all_special_tokens_extended'); "
                "from vllm.model_executor.models.registry import ModelRegistry; "
                "print(ModelRegistry)"
            ),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    if child.returncode != 0:
        detail = child.stderr.strip().splitlines()
        summary = detail[-1] if detail else f"exit status {child.returncode}"
        raise RuntimeError(f"vLLM child architecture import failed: {summary}")

    print(
        f"Transformers {transformers.__version__} integration and "
        "vLLM GHSA-8fr4-5q9j-m8gm, xgrammar GHSA-7rgv-gqhr-fxg3, and "
        "idna GHSA-65pc-fj4g-8rjx fixes verified"
    )


def main() -> None:
    """Validate or start the vLLM command-line interface."""
    _configure_child_bootstrap()
    removed_config = activate_runtime_boundary()
    if sys.argv[1:] == ["__heartwood_verify_runtime__"]:
        _verify_runtime(removed_config)
        return

    from vllm.entrypoints.cli.main import main as vllm_main

    vllm_main()


if __name__ == "__main__":
    main()
