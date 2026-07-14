# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from huggingface_hub import ModelInfo

from heartwood.gateway import (
    HuggingFaceModelRepository,
    LocalModelChoice,
    ModelArtifact,
    ModelRepositoryError,
    ModelSnapshot,
    load_model_artifact_catalog,
    load_model_snapshot_catalog,
    recommended_model_choices,
)


def test_repository_plan_selects_balanced_gguf_for_cpu() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model-q8_0.gguf", 8 * 1024**3, digest="8" * 64),
        _file("model-q4_k_m.gguf", 4 * 1024**3, digest="4" * 64),
    )

    plan = repository.plan(
        "example/research-model-gguf",
        cpu_available=True,
        gpu_available=False,
    )

    assert plan.model.runtime == "llama-cpp"
    assert plan.model.source_path == "model-q4_k_m.gguf"
    assert plan.model.source_revision == "1" * 40
    assert plan.model.catalog_source == "user-selected"
    assert "CPU cores" in str(plan.model.minimum_resource_envelope)
    assert "balanced single-file GGUF" in plan.selection_reason


def test_repository_plan_prefers_standard_snapshot_when_gpu_runtime_is_available() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 10 * 1024**3, digest="a" * 64),
        _file("model-q4_k_m.gguf", 4 * 1024**3, digest="4" * 64),
        _file(".gitattributes", 0),
    )

    plan = repository.plan(
        "example/research-model",
        cpu_available=True,
        gpu_available=True,
    )

    assert plan.model.runtime == "vllm"
    assert plan.model.source_path is None
    assert "NVIDIA GPU" in str(plan.model.minimum_resource_envelope)
    assert "NVIDIA vLLM runtime" in plan.selection_reason


def test_repository_plan_reports_unsupported_formats_and_runtime_mismatch() -> None:
    standard = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 1024, digest="a" * 64),
    )
    with pytest.raises(ModelRepositoryError, match=r"GPU image.*issues/new/choose"):
        standard.plan(
            "example/gpu-model",
            cpu_available=True,
            gpu_available=False,
        )

    unsupported = _repository(_file("README.md", 100))
    with pytest.raises(ModelRepositoryError, match=r"does not yet support.*issues/new/choose"):
        unsupported.plan(
            "example/unsupported-model",
            cpu_available=True,
            gpu_available=False,
        )

    tokenizer_only = _repository(
        _file("config.json", 100),
        _file("tokenizer.bin", 1024, digest="b" * 64),
    )
    with pytest.raises(ModelRepositoryError, match=r"does not yet support.*issues/new/choose"):
        tokenizer_only.plan(
            "example/not-a-model-snapshot",
            cpu_available=False,
            gpu_available=True,
        )


def test_repository_plan_rejects_snapshots_with_incomplete_size_metadata() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 1024, digest="a" * 64),
        SimpleNamespace(rfilename="tokenizer.json", size=None, lfs=None),
    )

    with pytest.raises(ModelRepositoryError, match=r"does not yet support.*issues/new/choose"):
        repository.plan(
            "example/incomplete-snapshot",
            cpu_available=False,
            gpu_available=True,
        )


def test_repository_plan_reports_deployments_without_a_compatible_runtime() -> None:
    cpu = _repository(_file("model-q4_k_m.gguf", 1024, digest="a" * 64)).inspect(
        "example/cpu-model"
    )
    with pytest.raises(ModelRepositoryError, match=r"portable CPU runtime.*issues/new/choose"):
        cpu.plan(cpu_available=False, gpu_available=False)

    empty = replace(cpu, candidates=())
    with pytest.raises(ModelRepositoryError, match=r"current deployment.*issues/new/choose"):
        empty.plan(cpu_available=False, gpu_available=False)


def test_repository_plan_rejects_ambiguous_gguf_choices_with_issue_link() -> None:
    repository = _repository(
        _file("first-f16.gguf", 1024, digest="1" * 64),
        _file("second-f16.gguf", 2048, digest="2" * 64),
    )

    with pytest.raises(ModelRepositoryError, match=r"could not choose.*issues/new/choose"):
        repository.plan(
            "example/ambiguous-gguf",
            cpu_available=True,
            gpu_available=False,
        )


def test_repository_plan_rejects_models_that_require_custom_code() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 1024, digest="a" * 64),
        tags=("transformers", "custom_code"),
    )

    with pytest.raises(ModelRepositoryError, match=r"require custom code.*issues/new/choose"):
        repository.plan(
            "example/custom-model",
            cpu_available=False,
            gpu_available=True,
        )


def test_repository_inspection_resolves_revision_license_and_network_errors() -> None:
    repository = _repository(_file("model-q4_k_m.gguf", 1024, digest="a" * 64))

    inspection = repository.inspect("example/model", revision="main")

    assert inspection.source_revision == "1" * 40
    assert inspection.license_posture.startswith("Source model card reports apache-2.0")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("offline")

    with pytest.raises(ModelRepositoryError, match=r"unable to inspect.*offline"):
        HuggingFaceModelRepository(model_info=fail).inspect("example/model")


def test_repository_inspection_supports_hugging_face_model_info_contract() -> None:
    info = ModelInfo(  # type: ignore[no-untyped-call]
        id="example/model-gguf",
        sha="3" * 40,
        siblings=[
            {
                "rfilename": "model-q4_k_m.gguf",
                "size": 1024,
                "lfs": {
                    "size": 1024,
                    "sha256": "c" * 64,
                    "pointerSize": 128,
                },
            }
        ],
        tags=["gguf"],
        cardData={"license": "apache-2.0"},
        config={},
    )
    repository = HuggingFaceModelRepository(model_info=lambda *_args, **_kwargs: info)

    plan = repository.plan(
        "example/model-gguf",
        cpu_available=True,
        gpu_available=False,
    )

    assert plan.model.source_path == "model-q4_k_m.gguf"
    assert plan.model.artifact_sha256 == "c" * 64


def test_repository_inspection_normalizes_metadata_and_detects_configured_custom_code() -> None:
    class CardData:
        def to_dict(self) -> dict[str, str]:
            return {"license": "mit"}

    info = SimpleNamespace(
        id=object(),
        sha="2" * 40,
        siblings=[object(), _file("model-q4_k_m.gguf", 1024, digest="b" * 64)],
        card_data=CardData(),
        tags=[],
        config={},
    )
    repository = HuggingFaceModelRepository(model_info=lambda *_args, **_kwargs: info)

    inspection = repository.inspect(" example/model ", revision=" ")

    assert inspection.source_repository == "example/model"
    assert inspection.license_posture.startswith("Source model card reports mit")
    assert inspection.safe_dict()["candidates"]

    info.config = {"auto_map": {"AutoModel": "model.CustomModel"}}
    with pytest.raises(ModelRepositoryError, match=r"require custom code.*issues/new/choose"):
        repository.inspect("example/model")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda choice: replace(choice, model_id="../model"), "safe directory name"),
        (lambda choice: replace(choice, label=" "), "label and purpose"),
        (lambda choice: replace(choice, source_repository="invalid"), "owner/model"),
        (lambda choice: replace(choice, source_revision="main"), "immutable commit"),
        (lambda choice: replace(choice, size_bytes=0), "storage metadata"),
        (lambda choice: replace(choice, minimum_free_bytes=1), "storage metadata"),
        (lambda choice: replace(choice, license_posture=" "), "license posture"),
        (lambda choice: replace(choice, source_path="model.bin"), "one GGUF file"),
        (
            lambda choice: replace(choice, source_path="../model.gguf"),
            "repository-relative path",
        ),
        (lambda choice: replace(choice, artifact_sha256=None), "SHA-256 digest"),
        (lambda choice: replace(choice, runtime="vllm"), "must not select one repository file"),
    ],
)
def test_local_model_choice_rejects_invalid_metadata(
    mutation: Callable[[LocalModelChoice], LocalModelChoice],
    message: str,
) -> None:
    with pytest.raises(ModelRepositoryError, match=message):
        mutation(_cpu_choice()).validate()


def test_local_model_choice_reuses_existing_download_contracts() -> None:
    cpu = _cpu_choice()
    cpu_payload = cpu.safe_dict()
    cpu_download = cpu.download_model()
    gpu = replace(
        cpu,
        model_id="hf-research-model-vllm",
        runtime="vllm",
        source_path=None,
        artifact_sha256=None,
    )
    gpu_download = gpu.download_model()

    assert cpu_payload["catalog_source"] == "user-selected"
    assert isinstance(cpu_download, ModelArtifact)
    assert cpu_download.source_path == "model-q4_k_m.gguf"
    assert isinstance(gpu_download, ModelSnapshot)
    assert gpu_download.minimum_free_bytes == cpu.minimum_free_bytes


def test_central_catalog_exposes_only_recommended_models() -> None:
    root = Path(__file__).resolve().parents[3]
    artifacts = load_model_artifact_catalog(
        root / "images" / "generic" / "local-runtime" / "model-catalog.toml"
    )
    snapshots = load_model_snapshot_catalog(
        root / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )

    choices = recommended_model_choices(artifacts.artifacts, snapshots.snapshots)
    downloadable = recommended_model_choices(
        artifacts.artifacts,
        snapshots.snapshots,
        recommended_only=False,
    )

    assert {choice.model_id for choice in choices} == {
        "qwen25-7b-instruct-q4_k_m",
        "qwen25-coder-7b-instruct-q4_k_m",
        "qwen25-7b-instruct-vllm",
    }
    assert all(choice.recommended_resource_envelope for choice in choices)
    assert "llama-cpp-stories260k-ci" in {choice.model_id for choice in downloadable}


def _repository(
    *siblings: object,
    tags: tuple[str, ...] = (),
) -> HuggingFaceModelRepository:
    info = SimpleNamespace(
        id="example/model",
        sha="1" * 40,
        siblings=list(siblings),
        card_data=SimpleNamespace(license="apache-2.0"),
        tags=list(tags),
    )
    return HuggingFaceModelRepository(model_info=lambda *_args, **_kwargs: info)


def _file(path: str, size: int, *, digest: str | None = None) -> object:
    return SimpleNamespace(
        rfilename=path,
        size=size,
        lfs=None if digest is None else SimpleNamespace(sha256=digest),
    )


def _cpu_choice() -> LocalModelChoice:
    return LocalModelChoice(
        model_id="hf-research-model-cpu",
        label="Research Model Q4_K_M",
        purpose="User-selected model",
        runtime="llama-cpp",
        source_repository="example/research-model",
        source_revision="1" * 40,
        source_path="model-q4_k_m.gguf",
        size_bytes=1024,
        minimum_free_bytes=2048,
        license_posture="Source model card reports apache-2.0.",
        catalog_source="user-selected",
        artifact_sha256="a" * 64,
    )
