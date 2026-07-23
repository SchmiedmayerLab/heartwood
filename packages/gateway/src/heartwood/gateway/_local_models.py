# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Normalized local-model choices and Hugging Face repository inspection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from importlib import import_module
from pathlib import PurePosixPath
from typing import Literal, Protocol, cast

from heartwood.gateway._local_model_contract import (
    DEFAULT_LOCAL_CONTEXT_WINDOW,
    MAXIMUM_LOCAL_CONTEXT_WINDOW,
    MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
    MINIMUM_LOCAL_CONTEXT_WINDOW,
)
from heartwood.gateway._model_artifacts import ModelArtifact
from heartwood.gateway._model_identity import (
    is_hugging_face_model_id,
    is_immutable_revision,
    is_resolved_revision,
)
from heartwood.gateway._model_snapshots import (
    ModelQualification,
    ModelSnapshot,
    ModelTier,
    ToolCallParser,
)

type LocalModelRuntime = Literal["llama-cpp", "vllm"]
type LocalModelCatalogSource = Literal["catalog", "user-selected"]

_SPLIT_GGUF = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)
_SAFETENSORS_WEIGHTS = re.compile(
    r"^model(?:-\d{5}-of-\d{5})?\.safetensors(?:\.index\.json)?$",
    re.IGNORECASE,
)
_ISSUE_URL = "https://github.com/SchmiedmayerLab/heartwood/issues/new/choose"
_GGUF_PREFERENCE = ("q4_k_m", "q5_k_m", "q4_k_s", "q5_k_s", "q8_0")
_HERMES_MODEL_TYPES = {"qwen2", "qwen3", "qwen3_moe", "qwen3_next"}
_TEXT_GENERATION_PIPELINES = {"conversational", "text-generation"}
_SNAPSHOT_ALLOW_PATTERNS = (
    "*.json",
    "*.jinja",
    "*.model",
    "*.safetensors",
    "*.tiktoken",
    "LICENSE*",
    "NOTICE*",
    "README*",
)
_SNAPSHOT_IGNORE_PATTERNS = ("*.bin", "*.py", ".git/*", "metal/*", "original/*")
_USER_SELECTED_PURPOSE = (
    "User-selected Hugging Face model; Heartwood has not reviewed its capabilities, "
    "license, or suitability."
)


class ModelRepositoryError(ValueError):
    """Raised when a model repository cannot produce a supported local candidate."""


class ModelInfoProvider(Protocol):
    """Subset of ``huggingface_hub.HfApi.model_info`` used by the gateway."""

    def __call__(
        self,
        repo_id: str,
        *,
        revision: str | None = None,
        files_metadata: bool = False,
        token: str | bool | None = None,
    ) -> object:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LocalModelChoice:
    """One gateway-normalized model that Heartwood can download and launch."""

    model_id: str
    label: str
    purpose: str
    runtime: LocalModelRuntime
    source_repository: str
    source_revision: str
    source_path: str | None
    size_bytes: int
    minimum_free_bytes: int
    license_posture: str
    catalog_source: LocalModelCatalogSource
    model_type: str | None = None
    context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW
    artifact_sha256: str | None = None
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None
    license_id: str = "Unspecified"
    precision: str = "Unspecified"
    tier: ModelTier = "standard"
    qualification: ModelQualification = "unvalidated"
    minimum_gpu_count: int = 0
    minimum_gpu_memory_bytes: int = 0
    recommended_ram_bytes: int = 0
    recommended_disk_bytes: int = 0
    maximum_context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW
    tool_call_parser: ToolCallParser | None = None
    tensor_parallel_size: int = 1
    startup_seconds_min: int = 30
    startup_seconds_max: int = 600
    download_policy: str | None = None
    allow_patterns: tuple[str, ...] = ()
    ignore_patterns: tuple[str, ...] = ()
    validated_platforms: tuple[str, ...] = ()
    qualification_test: str | None = None
    qualification_date: str | None = None
    qualification_evidence: str | None = None
    recommended_cpu_count: int = 8

    def validate(self) -> None:
        """Validate source provenance and runtime-specific integrity metadata."""
        if not self.model_id or "/" in self.model_id or ".." in self.model_id:
            raise ModelRepositoryError("managed model id must be a safe directory name")
        if not self.label.strip() or not self.purpose.strip():
            raise ModelRepositoryError("managed model label and purpose must not be empty")
        if not is_hugging_face_model_id(self.source_repository):
            raise ModelRepositoryError("repository must be a Hugging Face owner/model id")
        revision_is_valid = (
            is_resolved_revision(self.source_revision)
            if self.catalog_source == "user-selected"
            else is_immutable_revision(self.source_revision)
        )
        if not revision_is_valid:
            raise ModelRepositoryError("model revision must resolve to an immutable commit")
        if self.size_bytes <= 0 or self.minimum_free_bytes < self.size_bytes:
            raise ModelRepositoryError("managed model storage metadata is invalid")
        if not self.license_posture.strip():
            raise ModelRepositoryError("managed model license posture must not be empty")
        if not self.license_id.strip() or not self.precision.strip():
            raise ModelRepositoryError("managed model license and precision must not be empty")
        if self.tier not in {"standard", "powerful", "maximum"}:
            raise ModelRepositoryError(f"unsupported managed model tier: {self.tier}")
        if self.qualification not in {"unvalidated", "qualified"}:
            raise ModelRepositoryError(
                f"unsupported managed model qualification: {self.qualification}"
            )
        if self.startup_seconds_min <= 0 or self.startup_seconds_max < self.startup_seconds_min:
            raise ModelRepositoryError("managed model startup estimate is invalid")
        if self.model_type is not None and re.fullmatch(r"[a-z0-9_-]+", self.model_type) is None:
            raise ModelRepositoryError("managed model type must be a normalized identifier")
        if self.context_window < 2048:
            raise ModelRepositoryError("managed model context window must be at least 2048 tokens")
        if self.context_window > MAXIMUM_LOCAL_CONTEXT_WINDOW:
            raise ModelRepositoryError(
                "managed model context window must be at most "
                f"{MAXIMUM_LOCAL_CONTEXT_WINDOW} tokens"
            )
        if not self.context_window <= self.maximum_context_window <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
            raise ModelRepositoryError("managed model maximum context capacity is invalid")
        if self.recommended_ram_bytes <= 0 or self.recommended_disk_bytes < self.minimum_free_bytes:
            raise ModelRepositoryError("managed model RAM or disk recommendation is invalid")
        if self.recommended_cpu_count <= 0:
            raise ModelRepositoryError("managed model CPU recommendation is invalid")
        if self.runtime == "llama-cpp":
            if self.source_path is None or not self.source_path.casefold().endswith(".gguf"):
                raise ModelRepositoryError("CPU models require one GGUF file")
            source_path = PurePosixPath(self.source_path)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise ModelRepositoryError("model file must be a safe repository-relative path")
            if (
                self.artifact_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is None
            ):
                raise ModelRepositoryError("GGUF models require a source SHA-256 digest")
            if self.minimum_gpu_count != 0 or self.minimum_gpu_memory_bytes != 0:
                raise ModelRepositoryError("CPU models must not require GPU resources")
            if self.tool_call_parser is not None or self.download_policy is not None:
                raise ModelRepositoryError("CPU models must not declare vLLM settings")
        else:
            if self.source_path is not None or self.artifact_sha256 is not None:
                raise ModelRepositoryError("GPU snapshots must not select one repository file")
            if self.minimum_gpu_count <= 0 or self.minimum_gpu_memory_bytes <= 0:
                raise ModelRepositoryError("GPU models require a positive GPU resource envelope")
            if self.tensor_parallel_size < self.minimum_gpu_count:
                raise ModelRepositoryError("tensor parallelism must cover the minimum GPU count")
            if self.tool_call_parser not in {"hermes", "openai", "qwen3_coder"}:
                raise ModelRepositoryError("GPU models require a supported tool-call parser")
            if self.download_policy is None or not self.allow_patterns:
                raise ModelRepositoryError("GPU models require a reviewed download policy")

    def safe_dict(self) -> dict[str, object]:
        """Return non-secret model metadata for every interaction surface."""
        self.validate()
        return asdict(self)

    def qualification_for(self, platform_id: str) -> ModelQualification:
        """Return qualification for one exact managed-platform configuration."""
        if platform_id in self.validated_platforms:
            return self.qualification
        return "unvalidated"

    def download_model(self) -> ModelArtifact | ModelSnapshot:
        """Translate the normalized choice to the existing download implementation."""
        self.validate()
        runtime_profile = "llama-cpp-cpu" if self.runtime == "llama-cpp" else "vllm-cuda"
        if self.runtime == "llama-cpp":
            if self.source_path is None or self.artifact_sha256 is None:  # pragma: no cover
                raise ModelRepositoryError("CPU model metadata is incomplete")
            return ModelArtifact(
                artifact_id=self.model_id,
                runtime_profile=runtime_profile,
                purpose=self.purpose,
                source_repository=self.source_repository,
                source_path=self.source_path,
                source_revision=self.source_revision,
                artifact_format="GGUF",
                artifact_size_bytes=self.size_bytes,
                minimum_free_bytes=self.minimum_free_bytes,
                artifact_sha256=self.artifact_sha256,
                license_posture=self.license_posture,
                model_alias=self.label,
                context_window=self.context_window,
                minimum_resource_envelope=self.minimum_resource_envelope,
                recommended_resource_envelope=self.recommended_resource_envelope,
                recommended=False,
            )
        return ModelSnapshot(
            snapshot_id=self.model_id,
            runtime_profile=runtime_profile,
            purpose=self.purpose,
            source_repository=self.source_repository,
            source_revision=self.source_revision,
            expected_size_bytes=self.size_bytes,
            minimum_free_bytes=self.minimum_free_bytes,
            license_id=self.license_id,
            license_posture=self.license_posture,
            model_alias=self.label,
            precision=self.precision,
            tier=self.tier,
            qualification=self.qualification,
            minimum_gpu_count=self.minimum_gpu_count,
            minimum_gpu_memory_bytes=self.minimum_gpu_memory_bytes,
            recommended_ram_bytes=self.recommended_ram_bytes,
            recommended_disk_bytes=self.recommended_disk_bytes,
            maximum_context_window=self.maximum_context_window,
            tool_call_parser=cast(ToolCallParser, self.tool_call_parser),
            tensor_parallel_size=self.tensor_parallel_size,
            startup_seconds_min=self.startup_seconds_min,
            startup_seconds_max=self.startup_seconds_max,
            download_policy=cast(str, self.download_policy),
            allow_patterns=self.allow_patterns,
            ignore_patterns=self.ignore_patterns,
            validated_platforms=self.validated_platforms,
            qualification_test=self.qualification_test,
            qualification_date=self.qualification_date,
            qualification_evidence=self.qualification_evidence,
            context_window=self.context_window,
            minimum_resource_envelope=self.minimum_resource_envelope,
            recommended_resource_envelope=self.recommended_resource_envelope,
            recommended_cpu_count=self.recommended_cpu_count,
            recommended=False,
        )


@dataclass(frozen=True, slots=True)
class LocalModelDownloadPlan:
    """One automatic local-model choice for the current deployment."""

    model: LocalModelChoice
    selection_reason: str

    def safe_dict(self) -> dict[str, object]:
        """Return the exact model, runtime, and resource plan shown before download."""
        return {
            "model": self.model.safe_dict(),
            "selection_reason": self.selection_reason,
        }


@dataclass(frozen=True, slots=True)
class ModelRepositoryInspection:
    """Immutable repository provenance and supported download candidates."""

    source_repository: str
    source_revision: str
    license_posture: str
    candidates: tuple[LocalModelChoice, ...]

    def safe_dict(self) -> dict[str, object]:
        """Return inspection data suitable for REST, CLI, notebook, and browser clients."""
        return {
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "license_posture": self.license_posture,
            "candidates": [candidate.safe_dict() for candidate in self.candidates],
        }

    def plan(
        self,
        *,
        cpu_available: bool,
        gpu_available: bool,
    ) -> LocalModelDownloadPlan:
        """Choose the best supported runtime and model file for this deployment."""
        gpu = next(
            (candidate for candidate in self.candidates if candidate.runtime == "vllm"),
            None,
        )
        if gpu_available and gpu is not None:
            return LocalModelDownloadPlan(
                model=gpu,
                selection_reason=(
                    "Selected the repository snapshot because this deployment provides the "
                    "NVIDIA vLLM runtime."
                ),
            )
        cpu = _preferred_gguf(self.candidates) if cpu_available else None
        if cpu is not None:
            return LocalModelDownloadPlan(
                model=cpu,
                selection_reason=(
                    "Selected a balanced single-file GGUF variant for the portable CPU runtime."
                ),
            )
        if gpu is not None:
            raise ModelRepositoryError(
                "This repository requires an NVIDIA vLLM deployment, but the current Heartwood "
                f"runtime is CPU-only. Use a GPU image or report another setup at {_ISSUE_URL}"
            )
        if any(candidate.runtime == "llama-cpp" for candidate in self.candidates):
            raise ModelRepositoryError(
                "This repository provides a CPU model, but the current deployment does not "
                f"include the portable CPU runtime. Report another setup at {_ISSUE_URL}"
            )
        raise ModelRepositoryError(
            "Heartwood cannot prepare this model on the current deployment. "
            f"Report it at {_ISSUE_URL}"
        )


class HuggingFaceModelRepository:
    """Inspect public or process-authenticated Hugging Face model repositories."""

    def __init__(
        self,
        *,
        token: str | None = None,
        model_info: ModelInfoProvider | None = None,
    ) -> None:
        self.token = token
        if model_info is None:
            api = import_module("huggingface_hub").HfApi()
            model_info = cast(ModelInfoProvider, api.model_info)
        self.model_info = model_info

    def inspect(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelRepositoryInspection:
        """Resolve a repository revision and enumerate supported CPU and GPU candidates."""
        repository = repository.strip()
        revision = revision.strip() if revision is not None else None
        if not is_hugging_face_model_id(repository):
            raise ModelRepositoryError("repository must use the owner/model format")
        if revision == "":
            revision = None
        try:
            info = self.model_info(
                repository,
                revision=revision,
                files_metadata=True,
                token=self.token,
            )
        except ModelRepositoryError:
            raise
        except Exception as error:
            raise ModelRepositoryError(
                f"unable to inspect Hugging Face model {repository}: {error}"
            ) from error

        resolved_revision = getattr(info, "sha", None)
        if not isinstance(resolved_revision, str) or not is_resolved_revision(resolved_revision):
            raise ModelRepositoryError("Hugging Face did not return an immutable model revision")
        source_repository = getattr(info, "id", repository)
        if not isinstance(source_repository, str) or not is_hugging_face_model_id(
            source_repository
        ):
            source_repository = repository
        if _requires_custom_code(info):
            raise ModelRepositoryError(
                "Heartwood does not yet support model repositories that require custom code. "
                f"Report the model at {_ISSUE_URL}"
            )
        siblings = getattr(info, "siblings", None)
        if not isinstance(siblings, list) or not siblings:
            raise ModelRepositoryError("the model repository does not expose downloadable files")

        inspected_files: list[_RepositoryFile] = []
        metadata_complete = True
        for sibling in siblings:
            file = _repository_file(sibling)
            if file is not None:
                inspected_files.append(file)
            else:
                metadata_complete = False
        files = tuple(inspected_files)
        license_id, license_posture = _license_metadata(getattr(info, "card_data", None))
        context_window = _context_window(info)
        if context_window < MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:
            raise ModelRepositoryError(
                "Heartwood agent sessions require a model context window of at least "
                f"{MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:,} tokens; this repository reports "
                f"{context_window:,}. Report a compatibility problem at {_ISSUE_URL}"
            )
        model_type = _model_type(info)
        candidates = [
            candidate
            for item in files
            if (
                candidate := _gguf_candidate(
                    source_repository,
                    resolved_revision,
                    item,
                    license_posture,
                    context_window=context_window,
                    model_type=model_type,
                    license_id=license_id,
                )
            )
            is not None
        ]
        snapshot = _snapshot_candidate(
            source_repository,
            resolved_revision,
            files,
            license_posture,
            metadata_complete=metadata_complete,
            tool_call_parser=_tool_call_parser(source_repository, info),
            context_window=context_window,
            model_type=model_type,
            license_id=license_id,
        )
        if snapshot is not None:
            candidates.append(snapshot)
        if not candidates:
            raise ModelRepositoryError(
                "Heartwood does not yet support this model repository. Use a standard "
                "tool-capable model supported by the NVIDIA vLLM runtime or a single-file "
                "GGUF repository for CPU "
                f"inference, or report the model at {_ISSUE_URL}"
            )
        return ModelRepositoryInspection(
            source_repository=source_repository,
            source_revision=resolved_revision,
            license_posture=license_posture,
            candidates=tuple(candidates),
        )

    def plan(
        self,
        repository: str,
        *,
        cpu_available: bool,
        gpu_available: bool,
        revision: str | None = None,
    ) -> LocalModelDownloadPlan:
        """Inspect and automatically choose one supported local-model configuration."""
        return self.inspect(repository, revision=revision).plan(
            cpu_available=cpu_available,
            gpu_available=gpu_available,
        )


@dataclass(frozen=True, slots=True)
class _RepositoryFile:
    path: str
    size: int
    sha256: str | None


def catalog_model_choices(
    artifacts: tuple[ModelArtifact, ...],
    snapshots: tuple[ModelSnapshot, ...],
    *,
    recommended_only: bool = True,
) -> tuple[LocalModelChoice, ...]:
    """Normalize the centrally configured model catalogs into one ordered list."""
    choices = [
        LocalModelChoice(
            model_id=artifact.artifact_id,
            label=artifact.model_alias,
            purpose=artifact.purpose,
            runtime="llama-cpp",
            source_repository=artifact.source_repository,
            source_revision=artifact.source_revision,
            source_path=artifact.source_path,
            size_bytes=artifact.artifact_size_bytes,
            minimum_free_bytes=artifact.minimum_free_bytes,
            license_posture=artifact.license_posture,
            catalog_source="catalog",
            model_type=infer_model_type(artifact.source_repository),
            context_window=artifact.context_window,
            artifact_sha256=artifact.artifact_sha256,
            minimum_resource_envelope=artifact.minimum_resource_envelope,
            recommended_resource_envelope=artifact.recommended_resource_envelope,
            license_id=_license_id_from_posture(artifact.license_posture),
            precision=_gguf_precision(artifact.source_path),
            tier="standard",
            qualification=artifact.qualification,
            recommended_ram_bytes=max(16 * 1024**3, artifact.artifact_size_bytes * 4),
            recommended_disk_bytes=max(
                artifact.minimum_free_bytes,
                artifact.artifact_size_bytes * 3,
            ),
            maximum_context_window=artifact.context_window,
            validated_platforms=artifact.validated_platforms,
            qualification_test=artifact.qualification_test,
            qualification_date=artifact.qualification_date,
            qualification_evidence=artifact.qualification_evidence,
        )
        for artifact in artifacts
        if artifact.recommended or not recommended_only
    ]
    choices.extend(
        LocalModelChoice(
            model_id=snapshot.snapshot_id,
            label=snapshot.model_alias,
            purpose=snapshot.purpose,
            runtime="vllm",
            source_repository=snapshot.source_repository,
            source_revision=snapshot.source_revision,
            source_path=None,
            size_bytes=snapshot.expected_size_bytes,
            minimum_free_bytes=snapshot.minimum_free_bytes,
            license_posture=snapshot.license_posture,
            catalog_source="catalog",
            model_type=infer_model_type(snapshot.source_repository),
            context_window=snapshot.context_window,
            minimum_resource_envelope=snapshot.minimum_resource_envelope,
            recommended_resource_envelope=snapshot.recommended_resource_envelope,
            license_id=snapshot.license_id,
            precision=snapshot.precision,
            tier=snapshot.tier,
            qualification=snapshot.qualification,
            minimum_gpu_count=snapshot.minimum_gpu_count,
            minimum_gpu_memory_bytes=snapshot.minimum_gpu_memory_bytes,
            recommended_ram_bytes=snapshot.recommended_ram_bytes,
            recommended_disk_bytes=snapshot.recommended_disk_bytes,
            maximum_context_window=snapshot.maximum_context_window,
            tool_call_parser=snapshot.tool_call_parser,
            tensor_parallel_size=snapshot.tensor_parallel_size,
            startup_seconds_min=snapshot.startup_seconds_min,
            startup_seconds_max=snapshot.startup_seconds_max,
            download_policy=snapshot.download_policy,
            allow_patterns=snapshot.allow_patterns,
            ignore_patterns=snapshot.ignore_patterns,
            validated_platforms=snapshot.validated_platforms,
            qualification_test=snapshot.qualification_test,
            qualification_date=snapshot.qualification_date,
            qualification_evidence=snapshot.qualification_evidence,
            recommended_cpu_count=snapshot.recommended_cpu_count,
        )
        for snapshot in snapshots
        if snapshot.recommended or not recommended_only
    )
    for choice in choices:
        choice.validate()
    return tuple(choices)


def _repository_file(value: object) -> _RepositoryFile | None:
    path = getattr(value, "rfilename", None)
    size = getattr(value, "size", None)
    if not isinstance(path, str) or not path or not isinstance(size, int) or size < 0:
        return None
    lfs = getattr(value, "lfs", None)
    digest = getattr(lfs, "sha256", None)
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        digest = None
    return _RepositoryFile(path=path, size=size, sha256=digest)


def _gguf_candidate(
    repository: str,
    revision: str,
    file: _RepositoryFile,
    license_posture: str,
    *,
    context_window: int,
    model_type: str | None,
    license_id: str,
) -> LocalModelChoice | None:
    if (
        not file.path.casefold().endswith(".gguf")
        or file.size <= 0
        or _SPLIT_GGUF.search(file.path) is not None
        or file.sha256 is None
    ):
        return None
    filename = PurePosixPath(file.path).name
    label = filename.removesuffix(".gguf").removesuffix(".GGUF")
    return LocalModelChoice(
        model_id=_model_id(repository, revision, "llama-cpp", file.path),
        label=label,
        purpose=_USER_SELECTED_PURPOSE,
        runtime="llama-cpp",
        source_repository=repository,
        source_revision=revision,
        source_path=file.path,
        size_bytes=file.size,
        minimum_free_bytes=(file.size * 3 + 1) // 2,
        license_posture=license_posture,
        catalog_source="user-selected",
        model_type=model_type,
        context_window=context_window,
        artifact_sha256=file.sha256,
        minimum_resource_envelope=_cpu_resources(file.size, recommended=False),
        recommended_resource_envelope=_cpu_resources(file.size, recommended=True),
        license_id=license_id,
        precision=_gguf_precision(file.path),
        recommended_ram_bytes=max(16 * 1024**3, file.size * 4),
        recommended_disk_bytes=max((file.size * 3 + 1) // 2, file.size * 3),
        maximum_context_window=context_window,
    )


def _snapshot_candidate(
    repository: str,
    revision: str,
    files: tuple[_RepositoryFile, ...],
    license_posture: str,
    *,
    metadata_complete: bool,
    tool_call_parser: ToolCallParser | None,
    context_window: int,
    model_type: str | None,
    license_id: str,
) -> LocalModelChoice | None:
    if not metadata_complete or tool_call_parser is None:
        return None
    included_files = tuple(file for file in files if _included_snapshot_file(file.path))
    paths = {file.path for file in included_files}
    has_weights = any(_SAFETENSORS_WEIGHTS.fullmatch(path) is not None for path in paths)
    if "config.json" not in paths or not has_weights or len(included_files) == 0:
        return None
    size = sum(file.size for file in included_files)
    if size <= 0:
        return None
    label = repository.rsplit("/", maxsplit=1)[-1]
    return LocalModelChoice(
        model_id=_model_id(repository, revision, "vllm", None),
        label=label,
        purpose=_USER_SELECTED_PURPOSE,
        runtime="vllm",
        source_repository=repository,
        source_revision=revision,
        source_path=None,
        size_bytes=size,
        minimum_free_bytes=(size * 3 + 1) // 2,
        license_posture=license_posture,
        catalog_source="user-selected",
        model_type=model_type,
        context_window=context_window,
        minimum_resource_envelope=_gpu_resources(size, recommended=False),
        recommended_resource_envelope=_gpu_resources(size, recommended=True),
        license_id=license_id,
        precision="Repository-defined safetensors",
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=max(16_000_000_000, int(size * 1.25)),
        recommended_ram_bytes=max(32 * 1024**3, size * 2),
        recommended_disk_bytes=max((size * 3 + 1) // 2, size * 2),
        maximum_context_window=context_window,
        tool_call_parser=tool_call_parser,
        tensor_parallel_size=1,
        download_policy="transformers-safetensors",
        allow_patterns=_SNAPSHOT_ALLOW_PATTERNS,
        ignore_patterns=_SNAPSHOT_IGNORE_PATTERNS,
    )


def _model_id(
    repository: str,
    revision: str,
    runtime: LocalModelRuntime,
    source_path: str | None,
) -> str:
    name = repository.rsplit("/", maxsplit=1)[-1].casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:32] or "model"
    identity = "\n".join((repository, revision, runtime, source_path or ""))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"hf-{slug}-{digest}"


def _license_metadata(card_data: object) -> tuple[str, str]:
    license_id = getattr(card_data, "license", None)
    if not isinstance(license_id, str) and card_data is not None:
        to_dict = getattr(card_data, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if isinstance(data, dict):
                value = data.get("license")
                if isinstance(value, str):
                    license_id = value
    if isinstance(license_id, str) and license_id.strip():
        normalized = license_id.strip()
        return (
            normalized,
            f"Source model card reports {normalized}; review its terms before use.",
        )
    return (
        "Unspecified",
        "No machine-readable license was reported; review the source repository before use.",
    )


def _license_id_from_posture(posture: str) -> str:
    for license_id in ("Apache-2.0", "MIT", "BSD-3-Clause", "BSD-2-Clause"):
        if license_id.casefold() in posture.casefold():
            return license_id
    return "Unspecified"


def _gguf_precision(path: str) -> str:
    filename = PurePosixPath(path).stem.upper()
    match = re.search(r"(?:^|[-_.])(Q\d+(?:_[A-Z0-9]+)+)(?:$|[-_.])", filename)
    return f"GGUF {match.group(1)}" if match is not None else "GGUF quantized"


def _included_snapshot_file(path: str) -> bool:
    if any(fnmatchcase(path, pattern) for pattern in _SNAPSHOT_IGNORE_PATTERNS):
        return False
    return any(fnmatchcase(path, pattern) for pattern in _SNAPSHOT_ALLOW_PATTERNS)


def _requires_custom_code(info: object) -> bool:
    tags = getattr(info, "tags", None)
    if isinstance(tags, list | tuple) and any(
        isinstance(tag, str) and tag.casefold() == "custom_code" for tag in tags
    ):
        return True
    config = getattr(info, "config", None)
    return isinstance(config, dict) and bool(config.get("auto_map"))


def _tool_call_parser(repository: str, info: object) -> ToolCallParser | None:
    model_type = _model_type(info)
    pipeline_tag = getattr(info, "pipeline_tag", None)
    if isinstance(pipeline_tag, str) and pipeline_tag not in _TEXT_GENERATION_PIPELINES:
        return None
    return infer_tool_call_parser(repository, model_type)


def infer_tool_call_parser(
    repository: str,
    model_type: str | None,
) -> ToolCallParser | None:
    """Choose a supported vLLM parser from reviewed model-family metadata."""
    normalized_repository = repository.casefold().replace("_", "-")
    if "qwen3-coder" in normalized_repository:
        return "qwen3_coder"
    if model_type == "gpt_oss" or normalized_repository.startswith("openai/gpt-oss-"):
        return "openai"
    return "hermes" if model_type in _HERMES_MODEL_TYPES else None


def safe_snapshot_download_policy() -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return the shared no-custom-code safetensors download policy."""
    return (
        "transformers-safetensors",
        _SNAPSHOT_ALLOW_PATTERNS,
        _SNAPSHOT_IGNORE_PATTERNS,
    )


def _model_type(info: object) -> str | None:
    config = getattr(info, "config", None)
    if not isinstance(config, dict):
        return None
    model_type = config.get("model_type")
    if not isinstance(model_type, str):
        return None
    normalized = model_type.strip().casefold()
    return normalized if re.fullmatch(r"[a-z0-9_-]+", normalized) is not None else None


def infer_model_type(repository: str, declared: object = None) -> str | None:
    """Normalize a declared family, falling back to a conservative repository hint."""
    if isinstance(declared, str):
        normalized_declared = declared.strip().casefold()
        if re.fullmatch(r"[a-z0-9_-]+", normalized_declared) is not None:
            return normalized_declared
    normalized = repository.casefold().replace(".", "").replace("-", "")
    if "qwen3" in normalized:
        return "qwen3"
    if "qwen25" in normalized:
        return "qwen2"
    return None


def _context_window(info: object) -> int:
    """Choose a bounded local-runtime window from source model metadata."""
    config = getattr(info, "config", None)
    if isinstance(config, dict):
        for key in (
            "max_position_embeddings",
            "model_max_length",
            "n_positions",
            "seq_length",
        ):
            value = config.get(key)
            if isinstance(value, int) and value >= MINIMUM_LOCAL_CONTEXT_WINDOW:
                return min(value, MAXIMUM_LOCAL_CONTEXT_WINDOW)
    raise ModelRepositoryError(
        "Heartwood cannot verify this repository's model context capacity. "
        f"Report the model at {_ISSUE_URL} or import a reviewed artifact with an explicit "
        "context window."
    )


def _preferred_gguf(candidates: tuple[LocalModelChoice, ...]) -> LocalModelChoice | None:
    gguf = [candidate for candidate in candidates if candidate.runtime == "llama-cpp"]
    if len(gguf) == 1:
        return gguf[0]
    for marker in _GGUF_PREFERENCE:
        matching = [
            candidate for candidate in gguf if marker in (candidate.source_path or "").casefold()
        ]
        if len(matching) == 1:
            return matching[0]
    if gguf:
        raise ModelRepositoryError(
            "Heartwood found several GGUF files but could not choose a safe default. "
            f"Report this model at {_ISSUE_URL}"
        )
    return None


def _cpu_resources(size: int, *, recommended: bool) -> str:
    model_gib = _round_up_gib(size)
    memory_gib = model_gib * 2 + (8 if recommended else 4)
    disk_gib = model_gib * 2
    cores = 8 if recommended else 4
    prefix = "Recommended" if recommended else "Estimated minimum"
    return f"{prefix}: {cores} CPU cores, {memory_gib} GB RAM, and {disk_gib} GB free storage."


def _gpu_resources(size: int, *, recommended: bool) -> str:
    model_gib = _round_up_gib(size)
    vram_gib = model_gib + (8 if recommended else 4)
    memory_gib = max(32, model_gib * 2 + (16 if recommended else 8))
    disk_gib = model_gib * 2
    prefix = "Recommended" if recommended else "Estimated minimum"
    return (
        f"{prefix}: one NVIDIA GPU with {vram_gib} GB VRAM, {memory_gib} GB RAM, "
        f"and {disk_gib} GB free storage."
    )


def _round_up_gib(size: int) -> int:
    return max(1, (size + 1024**3 - 1) // 1024**3)
