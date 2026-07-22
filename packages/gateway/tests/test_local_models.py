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
    catalog_model_choices,
    load_model_artifact_catalog,
    load_model_snapshot_catalog,
    managed_model_request_body,
    managed_model_token_budgets,
    plan_local_context_window,
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
    assert plan.model.context_window == 32_768
    assert "CPU cores" in str(plan.model.minimum_resource_envelope)
    assert "balanced single-file GGUF" in plan.selection_reason


def test_context_planner_uses_stable_model_and_memory_bounded_tiers() -> None:
    t4 = plan_local_context_window(
        model_limit=131_072,
        model_size_bytes=5 * 1024**3,
        runtime="vllm",
        available_memory_bytes=16 * 1024**3,
    )
    larger_gpu = plan_local_context_window(
        model_limit=131_072,
        model_size_bytes=5 * 1024**3,
        runtime="vllm",
        available_memory_bytes=48 * 1024**3,
    )
    long_context_gpu = plan_local_context_window(
        model_limit=1_048_576,
        model_size_bytes=5 * 1024**3,
        runtime="vllm",
        available_memory_bytes=80 * 1024**3,
    )
    unknown = plan_local_context_window(
        model_limit=131_072,
        model_size_bytes=None,
        runtime="llama-cpp",
        available_memory_bytes=None,
    )

    assert t4.effective_window == 32_768
    assert t4.resource == "GPU memory"
    assert "headroom" in t4.reason
    assert larger_gpu.effective_window == 131_072
    assert "full" in larger_gpu.reason
    assert long_context_gpu.effective_window == 262_144
    assert "headroom" in long_context_gpu.reason
    assert unknown.effective_window == 18_432
    assert "minimum" in unknown.reason


def test_context_planner_never_exceeds_a_non_tier_model_limit() -> None:
    plan = plan_local_context_window(
        model_limit=48_000,
        model_size_bytes=4 * 1024**3,
        runtime="llama-cpp",
        available_memory_bytes=64 * 1024**3,
    )

    assert plan.effective_window == 32_768


def test_managed_model_budgets_reserve_output_inside_the_runtime_window() -> None:
    assert managed_model_token_budgets(18_432) == (16_384, 2_048)
    assert managed_model_token_budgets(32_768) == (28_672, 4_096)

    with pytest.raises(ValueError, match="managed agent context window"):
        managed_model_token_budgets(16_384)


def test_context_planner_rejects_short_models_and_insufficient_memory() -> None:
    with pytest.raises(ValueError, match="at least 18,432 tokens"):
        plan_local_context_window(
            model_limit=4_096,
            model_size_bytes=1024,
            runtime="llama-cpp",
            available_memory_bytes=64 * 1024**3,
        )
    with pytest.raises(ValueError, match="cannot support the minimum 18,432-token"):
        plan_local_context_window(
            model_limit=32_768,
            model_size_bytes=5 * 1024**3,
            runtime="vllm",
            available_memory_bytes=8 * 1024**3,
        )


def test_qwen3_managed_requests_disable_visible_reasoning_by_default() -> None:
    assert managed_model_request_body("qwen3") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert managed_model_request_body("qwen2") == {}


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
    assert plan.model.model_type == "qwen2"
    assert plan.model.source_path is None
    assert "NVIDIA GPU" in str(plan.model.minimum_resource_envelope)
    assert "NVIDIA vLLM runtime" in plan.selection_reason


def test_repository_plan_bounds_context_from_model_metadata() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 1024, digest="a" * 64),
        context_window=2_097_152,
    )

    plan = repository.plan(
        "example/long-context-model",
        cpu_available=False,
        gpu_available=True,
    )

    assert plan.model.context_window == 1_048_576


def test_repository_plan_rejects_models_too_short_for_an_agent_session() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model-q4_k_m.gguf", 1024, digest="a" * 64),
        context_window=4_096,
    )

    with pytest.raises(ModelRepositoryError, match="at least 18,432 tokens"):
        repository.plan(
            "example/short-context-model",
            cpu_available=True,
            gpu_available=False,
        )


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


def test_repository_plan_rejects_snapshots_without_supported_tool_metadata() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 1024, digest="a" * 64),
        model_type="bert",
        pipeline_tag="feature-extraction",
    )

    with pytest.raises(ModelRepositoryError, match=r"tool-capable model.*issues/new/choose"):
        repository.plan(
            "example/embedding-model",
            cpu_available=False,
            gpu_available=True,
        )


def test_repository_plan_accepts_qwen3_snapshots_with_hermes_tool_calls() -> None:
    repository = _repository(
        _file("config.json", 100),
        _file("model.safetensors", 1024, digest="a" * 64),
        model_type="qwen3",
    )

    plan = repository.plan(
        "example/qwen3-model",
        cpu_available=False,
        gpu_available=True,
    )

    assert plan.model.runtime == "vllm"
    assert plan.model.model_type == "qwen3"


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
        config={"max_position_embeddings": 32_768},
    )
    repository = HuggingFaceModelRepository(model_info=lambda *_args, **_kwargs: info)

    plan = repository.plan(
        "example/model-gguf",
        cpu_available=True,
        gpu_available=False,
    )

    assert plan.model.source_path == "model-q4_k_m.gguf"
    assert plan.model.artifact_sha256 == "c" * 64


def test_repository_inspection_rejects_unknown_context_capacity() -> None:
    info = SimpleNamespace(
        id="example/model-gguf",
        sha="3" * 40,
        siblings=[_file("model-q4_k_m.gguf", 1024, digest="c" * 64)],
        tags=["gguf"],
        card_data=SimpleNamespace(license="apache-2.0"),
        config={},
        pipeline_tag="text-generation",
    )
    repository = HuggingFaceModelRepository(model_info=lambda *_args, **_kwargs: info)

    with pytest.raises(ModelRepositoryError, match=r"cannot verify.*issues/new/choose"):
        repository.plan(
            "example/model-gguf",
            cpu_available=True,
            gpu_available=False,
        )


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
        config={"max_position_embeddings": 32_768},
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
        (lambda choice: replace(choice, context_window=1024), "at least 2048"),
        (lambda choice: replace(choice, context_window=1_048_577), "at most 1048576"),
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
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=16 * 1024**3,
        tool_call_parser="hermes",
        download_policy="synthetic",
        allow_patterns=("*.json", "*.safetensors"),
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

    choices = catalog_model_choices(artifacts.artifacts, snapshots.snapshots)
    downloadable = catalog_model_choices(
        artifacts.artifacts,
        snapshots.snapshots,
        recommended_only=False,
    )

    assert {choice.model_id for choice in choices} == {
        "qwen25-7b-instruct-q4_k_m",
        "qwen3-coder-30b-a3b-instruct-fp8-vllm",
        "qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm",
    }
    assert all(choice.recommended_resource_envelope for choice in choices)
    assert {choice.context_window for choice in choices} == {18_432, 32_768}
    assert "llama-cpp-stories260k-ci" in {choice.model_id for choice in downloadable}
    assert "qwen25-coder-7b-instruct-q4_k_m" in {choice.model_id for choice in downloadable}
    assert {
        "qwen25-coder-7b-instruct-awq-vllm",
        "qwen25-coder-14b-instruct-awq-vllm",
        "qwen25-coder-32b-instruct-awq-vllm",
        "qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm",
        "gpt-oss-20b-vllm",
        "qwen3-coder-30b-a3b-instruct-fp8-vllm",
        "qwen3-coder-next-fp8-vllm",
        "gpt-oss-120b-vllm",
    } <= {choice.model_id for choice in downloadable}
    assert all(choice.catalog_source == "catalog" for choice in downloadable)
    gpu_choices = {choice.model_id: choice for choice in downloadable if choice.runtime == "vllm"}
    assert {
        model_id for model_id, choice in gpu_choices.items() if choice.qualification == "qualified"
    } == {
        "qwen3-coder-30b-a3b-instruct-fp8-vllm",
        "qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm",
    }
    assert all(
        choice.qualification == "candidate"
        for model_id, choice in gpu_choices.items()
        if model_id
        not in {
            "qwen3-coder-30b-a3b-instruct-fp8-vllm",
            "qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm",
        }
    )


def test_catalog_qualification_is_scoped_to_the_validated_platform() -> None:
    root = Path(__file__).resolve().parents[3]
    artifacts = load_model_artifact_catalog(
        root / "images" / "generic" / "local-runtime" / "model-catalog.toml"
    )
    snapshots = load_model_snapshot_catalog(
        root / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )
    cpu = catalog_model_choices(artifacts.artifacts, snapshots.snapshots)[0]
    terra_gpu = next(
        choice
        for choice in catalog_model_choices(
            artifacts.artifacts,
            snapshots.snapshots,
            recommended_only=False,
        )
        if choice.model_id == "qwen25-coder-14b-instruct-awq-vllm"
    )
    qualified_terra_gpu = next(
        choice
        for choice in catalog_model_choices(
            artifacts.artifacts,
            snapshots.snapshots,
            recommended_only=False,
        )
        if choice.model_id == "qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm"
    )

    assert cpu.qualification_for("generic") == "qualified"
    assert cpu.qualification_for("terra") == "qualified"
    assert cpu.qualification_for("carina") == "candidate"
    assert terra_gpu.qualification_for("terra") == "candidate"
    assert terra_gpu.qualification_for("carina") == "candidate"
    assert qualified_terra_gpu.qualification_for("terra") == "qualified"
    assert qualified_terra_gpu.qualification_for("carina") == "candidate"


def _repository(
    *siblings: object,
    tags: tuple[str, ...] = ("text-generation",),
    model_type: str = "qwen2",
    pipeline_tag: str | None = "text-generation",
    context_window: int | None = 32_768,
) -> HuggingFaceModelRepository:
    config: dict[str, object] = {"model_type": model_type}
    if context_window is not None:
        config["max_position_embeddings"] = context_window
    info = SimpleNamespace(
        id="example/model",
        sha="1" * 40,
        siblings=list(siblings),
        card_data=SimpleNamespace(license="apache-2.0"),
        tags=list(tags),
        config=config,
        pipeline_tag=pipeline_tag,
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
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=3072,
    )
